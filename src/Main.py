import json
import os
import subprocess
import sys
import threading
import ctypes
import time
from pathlib import Path
from datetime import datetime

try:
    from ctypes import wintypes

    _HAS_WINTYPES = True
except Exception:
    wintypes = None
    _HAS_WINTYPES = False
from typing import Callable, Optional, Tuple

import customtkinter as ctk
from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD
from tkinter_icons import FluentRegularIcon
from CTkFileDialog import askopenfilename, askdirectory

from CTkMessagebox import CTkMessagebox

from core.renderer import AdobeSpritemapRenderer

_INVALID_CHARS = '<>:"/\\|?*'
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

ICON_COLOR = "#DCE4EE"
ICON_COLOR_MUTED = "#9AA4AF"
ICON_SIZE = 18
ICON_SIZE_SMALL = 16
ICON_SIZE_LARGE = 20

APP_NAME = "SpritemapToGIF"
CONFIG_VERSION = 1

_LOG_MAX_BYTES = 2 * 1024 * 1024


def _icon(name: str, size: int = ICON_SIZE, color: str = ICON_COLOR):
    try:
        tk_img = FluentRegularIcon(name, size=size, color=color).image
        pil_img = ImageTk.getimage(tk_img).convert("RGBA")
        return ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(size, size))
    except Exception as exc:
        print(
            f"[tkinter-icons] WARNING: could not load '{name}': {exc}", file=sys.stderr
        )
        return None


def resource_path(relative_path):
    try:
        base_path = Path(sys._MEIPASS)
    except Exception:
        base_path = Path.cwd()
    return str(base_path / relative_path)


def determine_icon():
    if sys.platform == "win32":
        return resource_path("assets/icon.ico")
    else:
        return resource_path("assets/icon.png")


def sanitize_filename(name: str) -> str:
    cleaned = "".join("_" if ch in _INVALID_CHARS else ch for ch in name)
    cleaned = cleaned.strip().rstrip(". ")
    if not cleaned:
        return "unnamed"
    if cleaned.upper().split(".")[0] in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


class SettingsStore:
    UI_KEYS = (
        "duration",
        "loop",
        "disposal",
        "use_source_fps",
        "filter_single_frame",
        "filter_unused_symbols",
        "root_animation_only",
        "animate_movie_clips",
        "appearance_mode",
        "window_geometry",
    )
    PATH_KEYS = (
        "animation_path",
        "spritemap_path",
        "atlas_path",
        "output_dir",
    )
    ALWAYS_SAVED_KEYS = ("window_geometry",)

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (_appdata_dir() / "config.json")
        self._data: dict = {}
        self.remember_paths: bool = False
        self.remember_settings: bool = False
        self._load()

    def _load(self):
        try:
            if self.path.is_file():
                with self.path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self._data = raw
        except Exception:
            self._data = {}
        self.remember_paths = bool(self._data.get("remember_paths", False))
        self.remember_settings = bool(self._data.get("remember_settings", False))

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": CONFIG_VERSION,
                "remember_paths": self.remember_paths,
                "remember_settings": self.remember_settings,
            }
            if self.remember_settings:
                for k in self.UI_KEYS:
                    if k in self._data:
                        payload[k] = self._data[k]

            for k in self.ALWAYS_SAVED_KEYS:
                if k in self._data:
                    payload[k] = self._data[k]

            if self.remember_paths:
                for k in self.PATH_KEYS:
                    if k in self._data:
                        payload[k] = self._data[k]

            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)

        except Exception as exc:
            print(f"[settings] WARNING: could not save config: {exc}", file=sys.stderr)


class GifWriter:
    @staticmethod
    def save(frames, gif_path, duration, loop, disposal):
        if not frames:
            return
        target_size = frames[0].size
        normalized = []
        for frame in frames:
            if frame.size != target_size:
                frame = frame.resize(target_size, Image.LANCZOS)
            normalized.append(frame)
        first, *rest = normalized

        gif_path = Path(gif_path)
        tmp_path = gif_path.with_name(gif_path.name + ".tmp")
        try:
            first.save(
                tmp_path,
                format="GIF",
                save_all=True,
                append_images=rest,
                duration=duration,
                loop=loop,
                disposal=disposal,
                comment=b"GIF Generated by: Spritemap to GIF",
            )
            os.replace(tmp_path, gif_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    @staticmethod
    def save_stream(
        frame_iterator,
        gif_path,
        duration,
        loop,
        disposal,
        on_frame: Optional[Callable[[int], bool]] = None,
    ) -> int:
        gif_path = Path(gif_path)
        tmp_path = gif_path.with_name(gif_path.name + ".tmp")

        first_frame: Optional[Image.Image] = None
        saved_frames = 0
        aborted = False
        target_size: Optional[Tuple[int, int]] = None

        try:
            append_images: list = []
            for index, frame in enumerate(frame_iterator):
                if on_frame is not None and not on_frame(index):
                    aborted = True
                    frame.close()
                    break
                if first_frame is None:
                    target_size = frame.size
                    first_frame = frame
                    continue
                if target_size is not None and frame.size != target_size:
                    frame = frame.resize(target_size, Image.LANCZOS)
                append_images.append(frame)
                saved_frames = index + 1

            if first_frame is None or aborted:
                return 0

            if target_size is not None and first_frame.size != target_size:
                first_frame = first_frame.resize(target_size, Image.LANCZOS)

            first_frame.save(
                tmp_path,
                format="GIF",
                save_all=True,
                append_images=append_images,
                duration=duration,
                loop=loop,
                disposal=disposal,
                comment=b"GIF Generated by: Spritemap to GIF",
            )
            os.replace(tmp_path, gif_path)
            return saved_frames
        finally:
            for img in locals().get("append_images", []) or []:
                try:
                    img.close()
                except Exception:
                    pass
            if first_frame is not None:
                try:
                    first_frame.close()
                except Exception:
                    pass
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass


def validate_animation_json(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object at the top level.")


def classify_json_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            if "ATLAS" in data:
                return "spritemap"
            if "TIMELINE" in data:
                return "animation"
    except Exception:
        pass

    name = path.name.lower()
    if "spritemap" in name:
        return "spritemap"
    if "animation" in name or "anims" in name:
        return "animation"
    return None


class DropEntry(ctk.CTkEntry):
    def __init__(self, master, file_mode="file", extensions=(), **kwargs):
        super().__init__(master, **kwargs)
        self.file_mode = file_mode
        self.extensions = tuple(e.lower() for e in extensions)
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _is_valid_path(self, path: str) -> bool:
        if not path:
            return False
        try:
            p = Path(path)
            if self.file_mode == "folder":
                return p.is_dir()
            if not p.is_file():
                return False
            if self.extensions and p.suffix.lower() not in self.extensions:
                return False
            return True
        except Exception:
            return False

    def _on_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            return getattr(event, "action", None)
        if len(paths) == 1 and self._is_valid_path(paths[0]):
            self.delete(0, "end")
            self.insert(0, paths[0])
            return "break"
        return getattr(event, "action", None)


class ExportWorker(threading.Thread):
    def __init__(
        self,
        animation_path,
        spritemap_json_path,
        atlas_image_path,
        output_dir,
        duration,
        loop,
        disposal,
        filter_single_frame,
        filter_unused_symbols,
        root_animation_only,
        use_source_fps,
        animate_movie_clips,
        on_progress: Callable,
        on_log: Callable,
        on_finished: Callable,
        on_error: Callable,
        log_file: Optional[Path] = None,
    ):
        super().__init__(daemon=True)
        self.animation_path = animation_path
        self.spritemap_json_path = spritemap_json_path
        self.atlas_image_path = atlas_image_path
        self.output_dir = output_dir
        self.duration = duration
        self.loop = loop
        self.disposal = disposal
        self.filter_single_frame = filter_single_frame
        self.filter_unused_symbols = filter_unused_symbols
        self.root_animation_only = root_animation_only
        self.use_source_fps = use_source_fps
        self.animate_movie_clips = animate_movie_clips
        self.on_progress = on_progress
        self.on_log = on_log
        self.on_finished = on_finished
        self.on_error = on_error
        self.log_file = log_file
        self._cancel = False
        self._done = threading.Event()

    def cancel(self):
        self._cancel = True

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def _write_log(self, message: str):
        if self.log_file is None:
            return
        try:
            try:
                if self.log_file.stat().st_size > _LOG_MAX_BYTES:
                    rotated = self.log_file.with_name(self.log_file.name + ".1")
                    os.replace(self.log_file, rotated)
            except OSError:
                pass
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        except Exception:
            pass

    def run(self):
        renderer = None
        try:
            self._write_log(f"=== Export started: {self.animation_path} ===")
            self.on_log("Loading animation document...")
            try:
                renderer = AdobeSpritemapRenderer(
                    animation_path=self.animation_path,
                    spritemap_json_path=self.spritemap_json_path,
                    atlas_image_path=self.atlas_image_path,
                    filter_single_frame=self.filter_single_frame,
                    filter_unused_symbols=self.filter_unused_symbols,
                    root_animation_only=self.root_animation_only,
                    animate_movie_clips=self.animate_movie_clips,
                )
            except json.JSONDecodeError as exc:
                self.on_error(f"Invalid JSON: {exc}")
                self._write_log(f"ERROR: Invalid JSON: {exc}")
                return
            except ValueError as exc:
                self.on_error(f"Invalid spritemap/animation: {exc}")
                self._write_log(f"ERROR: Invalid spritemap/animation: {exc}")
                return

            duration = self.duration
            if self.use_source_fps:
                fps = getattr(renderer, "frame_rate", 0) or 0
                if fps > 0:
                    duration = max(10, round(1000 / fps))
                    self.on_log(f"Using source framerate: {fps:g} fps -> {duration} ms")
                else:
                    self.on_log(
                        f"Source framerate not available; falling back to {duration} ms"
                    )

            if self.root_animation_only:
                output_path = Path(self.output_dir)
                self.on_log(f"Using output folder: {output_path}")
            else:
                root_name = renderer.get_root_animation_name() or "spritemap"
                output_path = Path(self.output_dir) / sanitize_filename(root_name)
                output_path.mkdir(parents=True, exist_ok=True)
                self.on_log(f"Created output folder: {output_path}")

            animations = list(renderer.iter_animations())
            total = len(animations)
            if total == 0:
                self.on_error("No animations found to export.")
                self._write_log("ERROR: No animations found to export.")
                return

            exported = 0
            failures = 0
            seen_names = {}
            for index, (name, frame_iterator) in enumerate(animations, start=1):
                if self._cancel:
                    self.on_log("Export cancelled by user.")
                    self.on_finished(
                        f"Export cancelled. {exported} GIF(s) saved before cancellation.",
                        str(output_path),
                    )
                    self._write_log("CANCELLED by user.")
                    return

                base_name = sanitize_filename(name)
                count = seen_names.get(base_name, 0)
                seen_names[base_name] = count + 1
                safe_name = base_name if count == 0 else f"{base_name}_{count}"

                self.on_progress(index, total, safe_name, 0, 0)
                self.on_log(f"Rendering '{safe_name}'...")

                try:

                    frame_counter = 0

                    def _on_frame(idx: int) -> bool:
                        nonlocal frame_counter
                        frame_counter += 1
                        if frame_counter % 5 == 0:
                            time.sleep(0.001)
                        if self._cancel:
                            return False
                        self.on_progress(index, total, safe_name, idx + 1, 0)
                        return True

                    gif_path = output_path / f"{safe_name}.gif"
                    saved = GifWriter.save_stream(
                        (img for (_name, img, _meta) in frame_iterator),
                        gif_path,
                        duration=duration,
                        loop=self.loop,
                        disposal=self.disposal,
                        on_frame=_on_frame,
                    )

                    if self._cancel and saved == 0:
                        self.on_log("Export cancelled by user.")
                        self.on_finished(
                            f"Export cancelled. {exported} GIF(s) saved before cancellation.",
                            str(output_path),
                        )
                        self._write_log("CANCELLED by user.")
                        return

                    if saved == 0:
                        self.on_log(f"  Skipping '{safe_name}' (no frames).")
                        continue

                    exported += 1
                    self.on_log(f"  Saved: {gif_path.name}")
                    self._write_log(f"OK: {gif_path}")
                except Exception as exc:
                    failures += 1
                    self.on_log(f"  ERROR on '{safe_name}': {exc}")
                    self._write_log(f"ERROR on '{safe_name}': {exc}")

            summary = f"Export complete. {exported} GIF(s) saved to:\n{output_path}"
            if failures:
                summary += f"\n\n{failures} animation(s) failed — see log."
            self._write_log(
                f"=== Export finished: {exported} ok, {failures} failed ==="
            )
            self.on_finished(summary, str(output_path))
        except Exception as exc:
            self.on_error(str(exc))
            self._write_log(f"FATAL: {exc}")
        finally:
            if renderer is not None:
                renderer.close()
            self._done.set()


class MainWindow(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self):
        super().__init__()
        self.DnD_Tkroot = self
        self.TkdndVersion = TkinterDnD._require(self)

        self.title("Spritemap to GIF")

        try:
            if sys.platform == "win32":
                self.wm_iconbitmap(determine_icon())
            else:
                icon_png = ImageTk.PhotoImage(Image.open(determine_icon()))
                self.iconphoto(False, icon_png)
        except Exception as exc:
            print(f"[icon] WARNING: could not set window icon: {exc}", file=sys.stderr)

        self._preferred_size = (750, 770)
        self._min_size = (620, 480)

        self.worker: Optional[ExportWorker] = None
        self._last_output_dir: Optional[Path] = None
        self._closing = False

        self.settings = SettingsStore()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._icons = {
            "input": _icon("ic-fluent-document-24-regular", ICON_SIZE, ICON_COLOR),
            "output": _icon("ic-fluent-folder-24-regular", ICON_SIZE, ICON_COLOR),
            "settings": _icon("ic-fluent-settings-24-regular", ICON_SIZE, ICON_COLOR),
            "options": _icon("ic-fluent-options-24-regular", ICON_SIZE, ICON_COLOR),
            "browse": _icon(
                "ic-fluent-folder-open-24-regular", ICON_SIZE_SMALL, ICON_COLOR_MUTED
            ),
            "export": _icon(
                "ic-fluent-arrow-export-up-24-regular", ICON_SIZE_LARGE, "#FFFFFF"
            ),
            "cancel": _icon(
                "ic-fluent-record-stop-24-regular", ICON_SIZE_LARGE, "#FFFFFF"
            ),
            "open": _icon(
                "ic-fluent-folder-open-24-regular", ICON_SIZE_LARGE, "#FFFFFF"
            ),
        }

        self._build_ui()
        self._fit_to_screen()
        self._apply_settings()
        self._update_export_button_state()

    def _get_usable_screen_area(self):
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()

        if _HAS_WINTYPES:
            try:
                rect = wintypes.RECT()
                SPI_GETWORKAREA = 0x0030

                if ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
                ):
                    return (
                        rect.left,
                        rect.top,
                        rect.right - rect.left,
                        rect.bottom - rect.top,
                    )
            except Exception:
                pass

        return (0, 0, sw, sh)

    def _fit_to_screen(self):
        self.update_idletasks()

        work_x, work_y, work_w, work_h = self._get_usable_screen_area()

        margin = 24
        max_w = max(200, work_w - margin * 2)
        max_h = max(200, work_h - margin * 2)

        pref_w, pref_h = self._preferred_size
        width = min(pref_w, max_w)
        height = min(pref_h, max_h)

        min_w = min(self._min_size[0], max_w)
        min_h = min(self._min_size[1], max_h)
        self.minsize(min_w, min_h)

        saved = self.settings.get("window_geometry")
        if isinstance(saved, str) and "x" in saved:
            try:
                self.geometry(saved)
                return
            except Exception:
                pass

        x = work_x + max(0, (work_w - width) // 2)
        y = work_y + max(0, (work_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _enable_mousewheel_scrolling(self):
        canvas = getattr(self.scroll_frame, "_parent_canvas", None)
        if canvas is None:
            return

        canvas.configure(yscrollincrement=20)

        if getattr(self, "_mousewheel_bound_ids", None) is None:
            self._mousewheel_bound_ids = set()

        def _on_mousewheel(event):
            if getattr(event, "num", None) == 4:
                steps = -1
            elif getattr(event, "num", None) == 5:
                steps = 1
            elif sys.platform == "darwin":
                steps = -int(event.delta)
            else:
                steps = -int(event.delta / 120)
            if event.state & 0x0004:
                steps *= 3
            canvas.yview_scroll(steps, "units")
            return "break"

        def _bind(widget):
            try:
                widget_id = str(widget)
            except Exception:
                widget_id = None
            if widget_id is not None and widget_id in self._mousewheel_bound_ids:
                return
            if widget_id is not None:
                self._mousewheel_bound_ids.add(widget_id)
            if sys.platform in ("win32", "darwin"):
                widget.bind("<MouseWheel>", _on_mousewheel, add="+")
            else:
                widget.bind("<Button-4>", _on_mousewheel, add="+")
                widget.bind("<Button-5>", _on_mousewheel, add="+")
            try:
                for child in widget.winfo_children():
                    _bind(child)
            except Exception:
                pass

        self._rebind_mousewheel = _bind
        _bind(self.scroll_frame)

    def _make_droppable(self, widget, handler):
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler)
        except Exception:
            pass
        for attr in ("_canvas", "_canvas_2"):
            inner = getattr(widget, attr, None)
            if inner is not None:
                try:
                    inner.drop_target_register(DND_FILES)
                    inner.dnd_bind("<<Drop>>", handler)
                except Exception:
                    pass

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll_frame.grid(row=0, column=0, sticky="nsew")
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        files_frame = ctk.CTkFrame(self.scroll_frame)
        files_frame.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        files_frame.grid_columnconfigure(1, weight=1)

        self._section_label(files_frame, "Input Files", "input").grid(
            row=0, column=0, columnspan=3, padx=10, pady=(10, 6), sticky="w"
        )

        self.animation_entry = self._add_file_row(
            files_frame, 2, "Animation JSON:", "animation.json (drag & drop)", ".json"
        )
        self.spritemap_entry = self._add_file_row(
            files_frame, 3, "Spritemap JSON:", "spritemap.json (drag & drop)", ".json"
        )
        self.atlas_entry = self._add_file_row(
            files_frame, 4, "Spritemap PNG:", "spritemap.png (drag & drop)", ".png"
        )

        self._make_droppable(files_frame, self._on_files_drop)
        for child in files_frame.winfo_children():
            self._make_droppable(child, self._on_files_drop)

        output_frame = ctk.CTkFrame(self.scroll_frame)
        output_frame.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        output_frame.grid_columnconfigure(1, weight=1)

        self._section_label(output_frame, "Output", "output").grid(
            row=0, column=0, columnspan=3, padx=10, pady=(10, 6), sticky="w"
        )

        ctk.CTkLabel(output_frame, text="Output Folder:", anchor="e", width=120).grid(
            row=1, column=0, padx=(10, 6), pady=6, sticky="e"
        )
        self.output_entry = DropEntry(
            output_frame,
            file_mode="folder",
            placeholder_text="Drop folder here or choose...",
        )
        self.output_entry.grid(row=1, column=1, padx=6, pady=6, sticky="ew")
        self._browse_button(output_frame, self._pick_output).grid(
            row=1, column=2, padx=(6, 10), pady=6
        )

        settings_frame = ctk.CTkFrame(self.scroll_frame)
        settings_frame.grid(row=2, column=0, padx=16, pady=8, sticky="ew")
        settings_frame.grid_columnconfigure(1, weight=1)

        self._section_label(settings_frame, "GIF Settings", "settings").grid(
            row=0, column=0, columnspan=2, padx=10, pady=(10, 6), sticky="w"
        )

        ctk.CTkLabel(
            settings_frame, text="Frame Duration:", anchor="e", width=120
        ).grid(row=1, column=0, padx=(10, 6), pady=6, sticky="e")
        duration_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        duration_row.grid(row=1, column=1, padx=6, pady=6, sticky="ew")
        self.duration_var = ctk.StringVar(value="42")
        self.duration_entry = ctk.CTkEntry(
            duration_row, textvariable=self.duration_var, width=100
        )
        self.duration_entry.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(duration_row, text="ms").grid(
            row=0, column=1, padx=(6, 0), sticky="w"
        )

        ctk.CTkLabel(settings_frame, text="Timing:", anchor="e", width=120).grid(
            row=2, column=0, padx=(10, 6), pady=6, sticky="e"
        )
        self.use_source_fps_var = ctk.BooleanVar(value=False)
        self.use_source_fps_check = ctk.CTkCheckBox(
            settings_frame,
            text="Use source framerate",
            variable=self.use_source_fps_var,
            command=self._toggle_duration,
        )
        self.use_source_fps_check.grid(row=2, column=1, padx=6, pady=6, sticky="w")

        ctk.CTkLabel(settings_frame, text="Loop Count:", anchor="e", width=120).grid(
            row=3, column=0, padx=(10, 6), pady=6, sticky="e"
        )
        loop_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        loop_row.grid(row=3, column=1, padx=6, pady=6, sticky="ew")
        self.loop_var = ctk.StringVar(value="0")
        self.loop_entry = ctk.CTkEntry(loop_row, textvariable=self.loop_var, width=100)
        self.loop_entry.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(loop_row, text="(0 = infinite)").grid(
            row=0, column=1, padx=(6, 0), sticky="w"
        )

        ctk.CTkLabel(settings_frame, text="Disposal:", anchor="e", width=120).grid(
            row=4, column=0, padx=(10, 6), pady=6, sticky="e"
        )
        self.disposal_var = ctk.StringVar(value="2 - Restore to background")
        self.disposal_combo = ctk.CTkComboBox(
            settings_frame,
            values=[
                "2 - Restore to background",
                "1 - Do not dispose",
                "3 - Restore to previous",
            ],
            variable=self.disposal_var,
            width=260,
        )
        self.disposal_combo.grid(row=4, column=1, padx=6, pady=6, sticky="w")

        options_frame = ctk.CTkFrame(self.scroll_frame)
        options_frame.grid(row=3, column=0, padx=16, pady=8, sticky="ew")
        options_frame.grid_columnconfigure(0, weight=1)

        self._section_label(options_frame, "Export Options", "options").grid(
            row=0, column=0, padx=10, pady=(10, 6), sticky="w"
        )

        render_opts = ctk.CTkFrame(options_frame, fg_color="transparent")
        render_opts.grid(row=1, column=0, padx=10, pady=(0, 6), sticky="ew")
        render_opts.grid_columnconfigure(0, weight=1)

        self.filter_single_var = ctk.BooleanVar(value=True)
        self.filter_single_check = ctk.CTkCheckBox(
            render_opts,
            text="Filter single-frame animations",
            variable=self.filter_single_var,
        )
        self.filter_single_check.grid(row=0, column=0, pady=4, sticky="w")

        self.filter_unused_var = ctk.BooleanVar(value=False)
        self.filter_unused_check = ctk.CTkCheckBox(
            render_opts, text="Filter unused symbols", variable=self.filter_unused_var
        )
        self.filter_unused_check.grid(row=1, column=0, pady=4, sticky="w")

        self.root_only_var = ctk.BooleanVar(value=False)
        self.root_only_check = ctk.CTkCheckBox(
            render_opts, text="Root animation only", variable=self.root_only_var
        )
        self.root_only_check.grid(row=2, column=0, pady=4, sticky="w")

        self.animate_mc_var = ctk.BooleanVar(value=True)
        self.animate_mc_check = ctk.CTkCheckBox(
            render_opts,
            text="Animate movie clips",
            variable=self.animate_mc_var,
        )
        self.animate_mc_check.grid(row=3, column=0, pady=4, sticky="w")

        separator = ctk.CTkFrame(
            options_frame, height=1, fg_color=("#3a3a3a", "#3a3a3a")
        )
        separator.grid(row=2, column=0, padx=10, pady=(4, 8), sticky="ew")

        persist_opts = ctk.CTkFrame(options_frame, fg_color="transparent")
        persist_opts.grid(row=3, column=0, padx=10, pady=(0, 10), sticky="ew")
        persist_opts.grid_columnconfigure(0, weight=1)

        self.remember_paths_var = ctk.BooleanVar(value=self.settings.remember_paths)
        self.remember_paths_check = ctk.CTkCheckBox(
            persist_opts,
            text="Remember last used file/folder paths",
            variable=self.remember_paths_var,
            command=self._on_remember_paths_toggled,
        )
        self.remember_paths_check.grid(row=0, column=0, pady=4, sticky="w")

        self.remember_settings_var = ctk.BooleanVar(
            value=self.settings.remember_settings
        )
        self.remember_settings_check = ctk.CTkCheckBox(
            persist_opts,
            text="Remember settings",
            variable=self.remember_settings_var,
            command=self._on_remember_settings_toggled,
        )
        self.remember_settings_check.grid(row=1, column=0, pady=4, sticky="w")

        button_frame = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
        button_frame.grid(row=4, column=0, padx=16, pady=8, sticky="ew")
        button_frame.grid_columnconfigure(0, weight=2)
        button_frame.grid_columnconfigure(1, weight=1)
        button_frame.grid_columnconfigure(2, weight=1)

        self.export_button = ctk.CTkButton(
            button_frame,
            text="  Export GIFs",
            image=self._icons.get("export"),
            compound="left",
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_export,
            state="disabled",
        )
        self.export_button.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.cancel_button = ctk.CTkButton(
            button_frame,
            text="  Cancel",
            image=self._icons.get("cancel"),
            compound="left",
            height=40,
            state="disabled",
            fg_color="#B71C1C",
            hover_color="#D32F2F",
            command=self._cancel_export,
        )
        self.cancel_button.grid(row=0, column=1, padx=6, sticky="ew")

        self.open_folder_button = ctk.CTkButton(
            button_frame,
            text="  Open Output",
            image=self._icons.get("open"),
            compound="left",
            height=40,
            state="disabled",
            fg_color="#37474F",
            hover_color="#455A64",
            command=self._open_output_folder,
        )
        self.open_folder_button.grid(row=0, column=2, padx=(6, 0), sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(self.scroll_frame)
        self.progress_bar.grid(row=5, column=0, padx=16, pady=(8, 4), sticky="ew")
        self.progress_bar.set(0)
        self.progress_label = ctk.CTkLabel(self.scroll_frame, text="Ready", anchor="w")
        self.progress_label.grid(row=6, column=0, padx=16, pady=(0, 4), sticky="ew")

        self.frame_label = ctk.CTkLabel(
            self.scroll_frame, text="", anchor="w", text_color=ICON_COLOR_MUTED
        )
        self.frame_label.grid(row=7, column=0, padx=16, pady=(0, 8), sticky="ew")

        self.log_toggle = ctk.CTkButton(
            self.scroll_frame,
            text="Show Log",
            width=120,
            height=28,
            fg_color="#37474F",
            hover_color="#455A64",
            command=self._toggle_log_panel,
        )
        self.log_toggle.grid(row=8, column=0, padx=16, pady=(0, 8), sticky="w")

        self.log_panel = ctk.CTkTextbox(
            self.scroll_frame, height=180, wrap="word", state="disabled"
        )
        self.log_panel.grid(row=9, column=0, padx=16, pady=(0, 16), sticky="nsew")
        self.log_panel.grid_remove()

        for var in (
            self.duration_var,
            self.loop_var,
            self.disposal_var,
        ):
            var.trace_add("write", lambda *_: self._update_export_button_state())
        for entry in (
            self.animation_entry,
            self.spritemap_entry,
            self.atlas_entry,
            self.output_entry,
        ):
            entry.bind(
                "<KeyRelease>",
                lambda *_: self._update_export_button_state(),
                add="+",
            )
            orig_insert = entry.insert

            def _wrapped_insert(index, string, _orig=orig_insert):
                _orig(index, string)
                self._update_export_button_state()

            entry.insert = _wrapped_insert

        self.after(100, self._enable_mousewheel_scrolling)

    def _section_label(self, parent, text: str, icon_key: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=f"  {text}",
            image=self._icons.get(icon_key),
            compound="left",
            font=ctk.CTkFont(size=14, weight="bold"),
        )

    def _browse_button(self, parent, command) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text="",
            image=self._icons.get("browse"),
            width=40,
            command=command,
        )

    def _add_file_row(self, parent, row, label, placeholder, ext):
        ctk.CTkLabel(parent, text=label, anchor="e", width=120).grid(
            row=row, column=0, padx=(10, 6), pady=6, sticky="e"
        )
        entry = DropEntry(
            parent, file_mode="file", extensions=(ext,), placeholder_text=placeholder
        )
        entry.grid(row=row, column=1, padx=6, pady=6, sticky="ew")
        self._browse_button(parent, lambda: self._pick_file(entry, ext)).grid(
            row=row, column=2, padx=(6, 10), pady=6
        )
        return entry

    def _classify_dropped_file(self, path: str) -> Optional[str]:
        try:
            p = Path(path)
        except Exception:
            return None
        if not p.is_file():
            return None

        suffix = p.suffix.lower()

        if suffix == ".png":
            return "atlas"

        if suffix == ".json":
            key = classify_json_file(p)
            if key is not None:
                return key
            if not self.spritemap_entry.get().strip():
                return "spritemap"
            if not self.animation_entry.get().strip():
                return "animation"
            return "animation"

        return None

    def _on_files_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            return getattr(event, "action", None)

        entries = {
            "animation": self.animation_entry,
            "spritemap": self.spritemap_entry,
            "atlas": self.atlas_entry,
        }

        leftovers = []
        for path in paths:
            key = self._classify_dropped_file(path)
            if key is None:
                continue
            entry = entries[key]
            existing = entry.get().strip()
            if existing and Path(existing) != Path(path):
                leftovers.append((key, path))
                continue
            entry.delete(0, "end")
            entry.insert(0, path)

        for key, path in leftovers:
            entry = entries[key]
            if entry.get().strip():
                self._append_log_panel(
                    f"Ignored dropped file (a different {key} is already "
                    f"selected): {path}"
                )
                continue
            entry.delete(0, "end")
            entry.insert(0, path)

        self._update_export_button_state()
        return getattr(event, "action", None)

    def _toggle_duration(self):
        if self.use_source_fps_var.get():
            self.duration_entry.configure(state="disabled")
        else:
            self.duration_entry.configure(state="normal")

    def _on_remember_paths_toggled(self):
        enabled = self.remember_paths_var.get()
        self.settings.remember_paths = enabled

        if enabled:
            self.settings.set("animation_path", self.animation_entry.get().strip())
            self.settings.set("spritemap_path", self.spritemap_entry.get().strip())
            self.settings.set("atlas_path", self.atlas_entry.get().strip())
            self.settings.set("output_dir", self.output_entry.get().strip())
            self.settings.save()
        else:
            for key in SettingsStore.PATH_KEYS:
                self.settings._data.pop(key, None)
            self.settings.save()

    def _on_remember_settings_toggled(self):
        enabled = self.remember_settings_var.get()
        self.settings.remember_settings = enabled

        if enabled:
            self._collect_settings_into_store()
        else:
            for key in SettingsStore.UI_KEYS:
                self.settings._data.pop(key, None)
        self.settings.save()

    def _toggle_log_panel(self):
        if self.log_panel.winfo_ismapped():
            self.log_panel.grid_remove()
            self.log_toggle.configure(text="Show Log")
        else:
            self.log_panel.grid()
            self.log_toggle.configure(text="Hide Log")
            rebind = getattr(self, "_rebind_mousewheel", None)
            if rebind is not None:
                self.after_idle(lambda: rebind(self.scroll_frame))

    def _append_log_panel(self, message: str):
        try:
            self.log_panel.configure(state="normal")
            self.log_panel.insert("end", message + "\n")
            self.log_panel.see("end")
            self.log_panel.configure(state="disabled")
        except Exception:
            pass

    def _pick_file(self, entry: DropEntry, ext: str):
        if ext == ".json":
            filetypes = [("JSON Files", "*.json"), ("All Files", "*.*")]
            title = "Select JSON File"
        else:
            filetypes = [("PNG Images", "*.png"), ("All Files", "*.*")]
            title = "Select Spritemap Image"
        path = askopenfilename(
            title=title,
            filetypes=filetypes,
            autocomplete=True,
            preview_img=(ext == ".png"),
            icon=determine_icon(),
        )
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _pick_output(self):
        path = askdirectory(
            title="Select Output Folder",
            autocomplete=True,
            icon=determine_icon(),
        )
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    def _ui(self, func, *args):
        if getattr(self, "_closing", False):
            return
        try:
            self.after(0, func, *args)
        except Exception:
            pass

    def _log(self, message: str):
        print(f"[AdobeSpritemapRenderer] {message}")
        self._append_log_panel(message)

    def _on_progress(self, current, total, name, frame_i=0, frame_n=0):
        def update():
            self.progress_bar.set(current / total if total else 0)
            self.progress_label.configure(text=f"Exporting {current}/{total}: {name}")
            if frame_n > 0:
                self.frame_label.configure(text=f"Frame {frame_i}/{frame_n}")
            else:
                self.frame_label.configure(text="")

        self._ui(update)

    def _on_log(self, message):
        self._ui(self._log, message)

    def _on_finished(self, message, output_path):
        def update():
            self._set_exporting(False)
            self.progress_bar.set(1.0)
            self.progress_label.configure(text="Done")
            self.frame_label.configure(text="")
            if output_path:
                p = Path(output_path)
                if p.is_dir():
                    self._last_output_dir = p
                    self.open_folder_button.configure(state="normal")
            title = "Export Complete"
            icon = "check"
            if "cancelled" in message.lower():
                title = "Export Cancelled"
                icon = "warning"
            CTkMessagebox(
                master=self,
                title=title,
                message=message,
                icon=icon,
                option_1="OK",
                sound=True,
            )

        self._ui(update)

    def _on_error(self, message):
        def update():
            self._set_exporting(False)
            self.progress_label.configure(text="Error")
            self.frame_label.configure(text="")
            fallback = self.output_entry.get().strip()
            if fallback:
                p = Path(fallback)
                if p.is_dir():
                    self._last_output_dir = p
                    self.open_folder_button.configure(state="normal")
            CTkMessagebox(
                master=self,
                title="Export Failed",
                message=message,
                icon="cancel",
                option_1="OK",
                sound=True,
            )

        self._ui(update)

    def _update_export_button_state(self):
        try:
            required = (
                self.animation_entry.get().strip(),
                self.spritemap_entry.get().strip(),
                self.atlas_entry.get().strip(),
                self.output_entry.get().strip(),
            )
            worker_busy = self.worker is not None and self.worker.is_alive()
            self.export_button.configure(
                state="normal" if all(required) and not worker_busy else "disabled"
            )
        except Exception:
            pass

    def _start_export(self):
        if self.worker is not None:
            if not self.worker.wait(timeout=2.0):
                CTkMessagebox(
                    master=self,
                    title="Export In Progress",
                    message="The previous export is still running. Please wait for it "
                    "to finish or cancel it before starting a new one.",
                    icon="warning",
                    option_1="OK",
                    sound=True,
                )
                return
            self.worker = None
        animation_path = Path(self.animation_entry.get().strip())
        spritemap_path = Path(self.spritemap_entry.get().strip())
        atlas_path = Path(self.atlas_entry.get().strip())
        output_dir = Path(self.output_entry.get().strip())

        if not all(
            (
                self.animation_entry.get().strip(),
                self.spritemap_entry.get().strip(),
                self.atlas_entry.get().strip(),
                self.output_entry.get().strip(),
            )
        ):
            CTkMessagebox(
                master=self,
                title="Missing Input",
                message="Please fill in all file and folder paths.",
                icon="warning",
                option_1="OK",
                sound=True,
            )
            return

        for label, path in (
            ("Animation JSON", animation_path),
            ("Spritemap JSON", spritemap_path),
            ("Spritemap PNG", atlas_path),
        ):
            if not path.is_file():
                CTkMessagebox(
                    master=self,
                    title="File Not Found",
                    message=f"{label} not found:\n{path}",
                    icon="warning",
                    option_1="OK",
                    sound=True,
                )
                return

        if not output_dir.is_dir():
            CTkMessagebox(
                master=self,
                title="Folder Not Found",
                message=f"Output folder not found:\n{output_dir}",
                icon="warning",
                option_1="OK",
                sound=True,
            )
            return

        probe_path = output_dir / ".spritemap_write_probe"
        try:
            with probe_path.open("wb") as probe:
                probe.write(b"")
            probe_path.unlink()
        except OSError as exc:
            CTkMessagebox(
                master=self,
                title="Output Not Writable",
                message=f"Cannot write to output folder:\n{output_dir}\n\n{exc}",
                icon="cancel",
                option_1="OK",
                sound=True,
            )
            return

        try:
            validate_animation_json(animation_path)
            validate_animation_json(spritemap_path)
        except ValueError as exc:
            CTkMessagebox(
                master=self,
                title="Invalid Input",
                message=str(exc),
                icon="cancel",
                option_1="OK",
                sound=True,
            )
            return

        try:
            duration = int(self.duration_var.get())
            loop = int(self.loop_var.get())
        except ValueError:
            CTkMessagebox(
                master=self,
                title="Invalid Input",
                message="Duration and Loop must be numbers.",
                icon="cancel",
                option_1="OK",
                sound=True,
            )
            return
        if duration <= 0:
            CTkMessagebox(
                master=self,
                title="Invalid Input",
                message="Duration must be a positive integer.",
                icon="cancel",
                option_1="OK",
                sound=True,
            )
            return
        if loop < 0:
            CTkMessagebox(
                master=self,
                title="Invalid Input",
                message="Loop must be 0 (infinite) or a positive integer.",
                icon="cancel",
                option_1="OK",
                sound=True,
            )
            return

        disposal = 2
        if self.disposal_var.get().startswith("1"):
            disposal = 1
        elif self.disposal_var.get().startswith("3"):
            disposal = 3

        log_path = output_dir / "export_log.txt"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n=== New export {datetime.now().isoformat(timespec='seconds')} ===\n"
                )
        except Exception:
            log_path = None

        self._set_exporting(True)
        self.progress_bar.set(0)
        self.progress_label.configure(text="Starting...")
        self.frame_label.configure(text="")
        self.open_folder_button.configure(state="disabled")
        self._last_output_dir = None

        self.worker = ExportWorker(
            animation_path=str(animation_path),
            spritemap_json_path=str(spritemap_path),
            atlas_image_path=str(atlas_path),
            output_dir=str(output_dir),
            duration=duration,
            loop=loop,
            disposal=disposal,
            filter_single_frame=self.filter_single_var.get(),
            filter_unused_symbols=self.filter_unused_var.get(),
            root_animation_only=self.root_only_var.get(),
            use_source_fps=self.use_source_fps_var.get(),
            animate_movie_clips=self.animate_mc_var.get(),
            on_progress=self._on_progress,
            on_log=self._on_log,
            on_finished=self._on_finished,
            on_error=self._on_error,
            log_file=log_path,
        )
        self.worker.start()

    def _cancel_export(self):
        if self.worker is not None and self.worker.is_alive():
            self.cancel_button.configure(state="disabled")
            self.progress_label.configure(text="Cancelling after current frame...")
            self._log("Cancel requested, finishing current frame…")
            self.worker.cancel()

    def _open_output_folder(self):
        if self._last_output_dir is not None and self._last_output_dir.is_dir():
            path = str(self._last_output_dir)
            try:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
            except Exception as exc:
                CTkMessagebox(
                    master=self,
                    title="Open Folder",
                    message=f"Could not open folder:\n{exc}",
                    icon="cancel",
                    option_1="OK",
                    sound=True,
                )

    def _set_exporting(self, exporting: bool):
        state = "disabled" if exporting else "normal"
        for widget in (
            self.animation_entry,
            self.spritemap_entry,
            self.atlas_entry,
            self.output_entry,
            self.duration_entry,
            self.loop_entry,
            self.disposal_combo,
            self.use_source_fps_check,
        ):
            try:
                widget.configure(state=state)
            except Exception:
                pass

        for widget in (
            self.filter_single_check,
            self.filter_unused_check,
            self.root_only_check,
            self.animate_mc_check,
            self.remember_paths_check,
            self.remember_settings_check,
        ):
            try:
                widget.configure(state=state)
            except Exception:
                pass

        self.cancel_button.configure(state="normal" if exporting else "disabled")

        if not exporting and self.use_source_fps_var.get():
            self.duration_entry.configure(state="disabled")

        self._update_export_button_state()

    def _apply_settings(self):
        s = self.settings
        self.remember_paths_var.set(bool(s.remember_paths))
        self.remember_settings_var.set(bool(s.remember_settings))

        if s.remember_settings:
            self.duration_var.set(str(s.get("duration", self.duration_var.get())))
            self.loop_var.set(str(s.get("loop", self.loop_var.get())))
            self.disposal_var.set(str(s.get("disposal", self.disposal_var.get())))
            self.use_source_fps_var.set(bool(s.get("use_source_fps", False)))
            self.filter_single_var.set(bool(s.get("filter_single_frame", True)))
            self.filter_unused_var.set(bool(s.get("filter_unused_symbols", False)))
            self.root_only_var.set(bool(s.get("root_animation_only", False)))
            self.animate_mc_var.set(bool(s.get("animate_movie_clips", True)))
        self._toggle_duration()

        if s.remember_paths:
            self.animation_entry.insert(0, s.get("animation_path", "") or "")
            self.spritemap_entry.insert(0, s.get("spritemap_path", "") or "")
            self.atlas_entry.insert(0, s.get("atlas_path", "") or "")
            self.output_entry.insert(0, s.get("output_dir", "") or "")

        self._update_export_button_state()

    def _collect_settings_into_store(self):
        s = self.settings
        s.remember_paths = self.remember_paths_var.get()
        s.remember_settings = self.remember_settings_var.get()

        if s.remember_settings:
            s.set("duration", self.duration_var.get())
            s.set("loop", self.loop_var.get())
            s.set("disposal", self.disposal_var.get())
            s.set("use_source_fps", self.use_source_fps_var.get())
            s.set("filter_single_frame", self.filter_single_var.get())
            s.set("filter_unused_symbols", self.filter_unused_var.get())
            s.set("root_animation_only", self.root_only_var.get())
            s.set("animate_movie_clips", self.animate_mc_var.get())

        try:
            s.set("window_geometry", self.geometry())
        except Exception:
            pass

        if s.remember_paths:
            s.set("animation_path", self.animation_entry.get().strip())
            s.set("spritemap_path", self.spritemap_entry.get().strip())
            s.set("atlas_path", self.atlas_entry.get().strip())
            s.set("output_dir", self.output_entry.get().strip())

    def on_close(self):
        self._closing = True
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel()
        try:
            self._collect_settings_into_store()
            self.settings.save()
        except Exception:
            pass
        self.destroy()


def main():
    app = MainWindow()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
