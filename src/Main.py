import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtGui import QIcon, QScreen

import qtawesome as qta

from core.renderer import AdobeSpritemapRenderer

import core.resources


class DropLineEdit(QLineEdit):
    def __init__(self, file_mode: str = "file", extensions: tuple = (), parent=None):
        super().__init__(parent)
        self.file_mode = file_mode
        self.extensions = extensions
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and self._is_valid_path(urls[0].toLocalFile()):
                event.acceptProposedAction()
                self._set_drag_style(True)
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drag_style(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_drag_style(False)
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1:
                path = urls[0].toLocalFile()
                if self._is_valid_path(path):
                    self.setText(path)
                    event.acceptProposedAction()
                    return
        event.ignore()

    def _is_valid_path(self, path: str) -> bool:
        if not path:
            return False
        p = Path(path)
        if self.file_mode == "folder":
            return p.is_dir()
        if not p.is_file():
            return False
        if self.extensions and p.suffix.lower() not in self.extensions:
            return False
        return True

    def _set_drag_style(self, active: bool):
        if active:
            self.setStyleSheet(
                "QLineEdit { border: 2px dashed #4FC3F7; background-color: #2A3A44; }"
            )
        else:
            self.setStyleSheet("")


class ExportWorker(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished_export = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        animation_path: str,
        spritemap_json_path: str,
        atlas_image_path: str,
        output_dir: str,
        duration: int,
        loop: int,
        disposal: int,
        filter_single_frame: bool,
        filter_unused_symbols: bool,
        root_animation_only: bool,
    ):
        super().__init__()
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

    def run(self):
        renderer = None
        try:
            self.log.emit("Loading animation document...")
            renderer = AdobeSpritemapRenderer(
                animation_path=self.animation_path,
                spritemap_json_path=self.spritemap_json_path,
                atlas_image_path=self.atlas_image_path,
                filter_single_frame=self.filter_single_frame,
                filter_unused_symbols=self.filter_unused_symbols,
                root_animation_only=self.root_animation_only,
            )

            root_name = renderer.get_root_animation_name() or "spritemap"
            output_path = Path(self.output_dir) / self._sanitize_name(root_name)
            output_path.mkdir(parents=True, exist_ok=True)
            self.log.emit(f"Created output folder: {output_path}")

            animations = list(renderer.iter_animations())
            total = len(animations)
            if total == 0:
                self.error.emit("No animations found to export.")
                return

            exported = 0
            for index, (name, frame_iterator) in enumerate(animations, start=1):
                safe_name = self._sanitize_name(name)
                self.progress.emit(index, total, safe_name)
                self.log.emit(f"Rendering '{safe_name}'...")

                frames = [frame_image for _, frame_image, _ in frame_iterator]
                if not frames:
                    self.log.emit(f"  Skipping '{safe_name}' (no frames).")
                    continue

                gif_path = output_path / f"{safe_name}.gif"
                self._save_gif(frames, gif_path)
                for frame in frames:
                    frame.close()

                exported += 1
                self.log.emit(f"  Saved: {gif_path.name}")

            self.finished_export.emit(
                f"Export complete. {exported} GIF(s) saved to:\n{output_path}"
            )
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if renderer is not None:
                renderer.close()

    def _save_gif(self, frames: list, gif_path: Path):
        first, *rest = frames
        first.save(
            gif_path,
            save_all=True,
            append_images=rest,
            duration=self.duration,
            loop=self.loop,
            disposal=self.disposal,
            comment="GIF Generated by: Spritemap to GIF",
        )

    @staticmethod
    def _sanitize_name(name: str) -> str:
        invalid = '<>:"/\\|?*'
        cleaned = "".join("_" if ch in invalid else ch for ch in name).strip()
        return cleaned or "unnamed"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Spritemap to GIF")
        self.setWindowIcon(QIcon(":ui/icon.ico"))
        self.setFixedSize(750, 770)
        self.worker = None

        self._build_ui()
        self._apply_styles()
        self._center_window()

    def _center_window(self):
        screen = QScreen.availableGeometry(QApplication.primaryScreen())
        window_frame = self.frameGeometry()
        window_frame.moveCenter(screen.center())
        self.move(window_frame.topLeft())

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        files_group = QGroupBox("Input Files")
        files_layout = QFormLayout(files_group)
        files_layout.setLabelAlignment(Qt.AlignRight)

        self.animation_spritemap_edit = DropLineEdit(
            file_mode="file", extensions=(".json",)
        )
        self.animation_spritemap_edit.setPlaceholderText("animation.json (drag & drop)")
        files_layout.addRow(
            self._icon_label("fa5s.file-code", "Animation JSON:"),
            self._file_row(self.animation_spritemap_edit, self._pick_animation),
        )

        self.spritemap_code_edit = DropLineEdit(file_mode="file", extensions=(".json",))
        self.spritemap_code_edit.setPlaceholderText("spritemap.json (drag & drop)")
        files_layout.addRow(
            self._icon_label("fa5s.file-code", "Spritemap JSON:"),
            self._file_row(self.spritemap_code_edit, self._pick_spritemap),
        )

        self.spritemap_image_edit = DropLineEdit(file_mode="file", extensions=(".png",))
        self.spritemap_image_edit.setPlaceholderText("spritemap.png (drag & drop)")
        files_layout.addRow(
            self._icon_label("fa5s.image", "Spritemap PNG:"),
            self._file_row(self.spritemap_image_edit, self._pick_atlas),
        )

        main_layout.addWidget(files_group)

        output_group = QGroupBox("Output")
        output_layout = QFormLayout(output_group)
        output_layout.setLabelAlignment(Qt.AlignRight)

        self.output_edit = DropLineEdit(file_mode="folder")
        self.output_edit.setPlaceholderText("Drop folder here or choose...")
        output_layout.addRow(
            self._icon_label("fa5s.folder", "Output Folder:"),
            self._file_row(self.output_edit, self._pick_output),
        )
        main_layout.addWidget(output_group)

        settings_group = QGroupBox("GIF Settings")
        settings_layout = QFormLayout(settings_group)
        settings_layout.setLabelAlignment(Qt.AlignRight)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(10, 10000)
        self.duration_spin.setValue(42)
        self.duration_spin.setSuffix(" ms")
        settings_layout.addRow(
            self._icon_label("fa5s.clock", "Frame Duration:"), self.duration_spin
        )

        self.loop_spin = QSpinBox()
        self.loop_spin.setRange(0, 100)
        self.loop_spin.setValue(0)
        self.loop_spin.setToolTip("0 = infinite loop")
        settings_layout.addRow(
            self._icon_label("fa5s.redo", "Loop Count:"), self.loop_spin
        )

        self.disposal_combo = QComboBox()
        self.disposal_combo.addItem("2 - Restore to background", 2)
        self.disposal_combo.addItem("1 - Do not dispose", 1)
        self.disposal_combo.addItem("3 - Restore to previous", 3)
        settings_layout.addRow(
            self._icon_label("fa5s.trash-alt", "Disposal:"), self.disposal_combo
        )

        main_layout.addWidget(settings_group)

        options_group = QGroupBox("Export Options")
        options_layout = QVBoxLayout(options_group)

        self.filter_single_check = QCheckBox("Filter single-frame animations")
        self.filter_single_check.setChecked(True)
        options_layout.addWidget(self.filter_single_check)

        self.filter_unused_check = QCheckBox("Filter unused symbols")
        self.filter_unused_check.setChecked(False)
        options_layout.addWidget(self.filter_unused_check)

        self.root_only_check = QCheckBox("Root animation only")
        self.root_only_check.setChecked(False)
        options_layout.addWidget(self.root_only_check)

        main_layout.addWidget(options_group)

        self.export_button = QPushButton(
            qta.icon("fa5s.play", color="#FFFFFF"), "  Export GIFs"
        )
        self.export_button.setObjectName("exportButton")
        self.export_button.setMinimumHeight(40)
        self.export_button.clicked.connect(self._start_export)
        main_layout.addWidget(self.export_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        main_layout.addWidget(self.progress_bar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Export log will appear here...")
        main_layout.addWidget(self.log_view, stretch=1)

    def _icon_label(self, icon_name: str, text: str) -> QLabel:
        label = QLabel(text)
        label.setPixmap(qta.icon(icon_name, color="#90CAF9").pixmap(16, 16))
        return label

    def _file_row(self, line_edit: QLineEdit, handler) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(line_edit)

        button = QPushButton(qta.icon("fa5s.folder-open", color="#E0E0E0"), "")
        button.setFixedWidth(36)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(handler)
        layout.addWidget(button)
        return container

    def _pick_animation(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Animation JSON", "", "JSON Files (*.json);;All Files (*)"
        )
        if path:
            self.animation_spritemap_edit.setText(path)

    def _pick_spritemap(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Spritemap JSON", "", "JSON Files (*.json);;All Files (*)"
        )
        if path:
            self.spritemap_code_edit.setText(path)

    def _pick_atlas(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Spritemap Image",
            "",
            "Images (*.png);;All Files (*)",
        )
        if path:
            self.spritemap_image_edit.setText(path)

    def _pick_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if path:
            self.output_edit.setText(path)

    def _start_export(self):
        animation_path = Path(self.animation_spritemap_edit.text().strip())
        spritemap_path = Path(self.spritemap_code_edit.text().strip())
        atlas_path = Path(self.spritemap_image_edit.text().strip())
        output_dir = Path(self.output_edit.text().strip())

        if not all(
            (
                animation_path.parent != Path(".") or animation_path.name,
                spritemap_path.parent != Path(".") or spritemap_path.name,
                atlas_path.parent != Path(".") or atlas_path.name,
                output_dir.parent != Path(".") or output_dir.name,
            )
        ):
            QMessageBox.warning(
                self, "Missing Input", "Please fill in all file and folder paths."
            )
            return

        for label, path in (
            ("Animation JSON", animation_path),
            ("Spritemap JSON", spritemap_path),
            ("Spritemap PNG", atlas_path),
        ):
            if not path.is_file():
                QMessageBox.warning(
                    self, "File Not Found", f"{label} not found:\n{path}"
                )
                return

        if not output_dir.is_dir():
            QMessageBox.warning(
                self, "Folder Not Found", f"Output folder not found:\n{output_dir}"
            )
            return

        self._set_ui_enabled(False)
        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(1)

        self.worker = ExportWorker(
            animation_path=str(animation_path),
            spritemap_json_path=str(spritemap_path),
            atlas_image_path=str(atlas_path),
            output_dir=str(output_dir),
            duration=self.duration_spin.value(),
            loop=self.loop_spin.value(),
            disposal=self.disposal_combo.currentData(),
            filter_single_frame=self.filter_single_check.isChecked(),
            filter_unused_symbols=self.filter_unused_check.isChecked(),
            root_animation_only=self.root_only_check.isChecked(),
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._on_log)
        self.worker.finished_export.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def _on_progress(self, current: int, total: int, name: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"Exporting {current}/{total}: {name}")

    def _on_log(self, message: str):
        self.log_view.append(message)

    def _on_finished(self, message: str):
        self._set_ui_enabled(True)
        self.progress_bar.setFormat("Done")
        QMessageBox.information(self, "Export Complete", message)

    def _on_error(self, message: str):
        self._set_ui_enabled(True)
        self.progress_bar.setFormat("Error")
        QMessageBox.critical(self, "Export Failed", message)

    def _set_ui_enabled(self, enabled: bool):
        self.export_button.setEnabled(enabled)
        self.animation_spritemap_edit.setEnabled(enabled)
        self.spritemap_code_edit.setEnabled(enabled)
        self.spritemap_image_edit.setEnabled(enabled)
        self.output_edit.setEnabled(enabled)

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1E1E1E;
                color: #E0E0E0;
                font-family: "Segoe UI", "Roboto", sans-serif;
                font-size: 10pt;
            }

            QLabel {
                background-color: transparent;
            }

            QGroupBox {
                border: 1px solid #333333;
                border-radius: 8px;
                margin-top: 14px;
                padding: 12px;
                background-color: #252526;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 6px;
                color: #4FC3F7;
                font-weight: bold;
                background-color: #252526;
            }

            QLineEdit, QSpinBox, QComboBox, QTextEdit {
                background-color: #2D2D30;
                border: 1px solid #3E3E42;
                border-radius: 6px;
                padding: 6px 8px;
                selection-background-color: #4FC3F7;
                selection-color: #1E1E1E;
                color: #E0E0E0;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {
                border: 1px solid #4FC3F7;
            }
            QLineEdit:disabled, QSpinBox:disabled,
            QComboBox:disabled, QTextEdit:disabled {
                background-color: #262628;
                color: #6D6D6D;
                border: 1px solid #2F2F31;
            }

            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 22px;
                border-left: 1px solid #3E3E42;
                background-color: #333337;
                border-top-right-radius: 6px;
                border-bottom-right-radius: 6px;
            }
            QComboBox::drop-down:hover {
                background-color: #3E3E42;
            }
            QComboBox::down-arrow {
                image: url(:ui/down_arrow.png);
                width: 18px;
                height: 18px;
            }
            QComboBox QAbstractItemView {
                background-color: #2D2D30;
                border: 1px solid #4FC3F7;
                selection-background-color: #4FC3F7;
                selection-color: #1E1E1E;
                outline: 0;
            }

            QSpinBox::up-button {
                image: url(:ui/up_arrow.png);
                width: 18px;
            }
            QSpinBox::down-button {
                image: url(:ui/down_arrow.png);
                width: 18px;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background-color: #3E3E42;
            }
            QSpinBox::up-arrow {
                image: url(:ui/up_arrow.png);
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                width: 0; height: 0;
            }
            QSpinBox::down-arrow {
                image: url(:ui/down_arrow.png);
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                width: 0; height: 0;
            }

            QPushButton {
                background-color: #333337;
                border: 1px solid #3E3E42;
                border-radius: 6px;
                padding: 6px 12px;
                color: #E0E0E0;
            }
            QPushButton:hover {
                background-color: #3E3E42;
                border: 1px solid #4FC3F7;
            }
            QPushButton:pressed {
                background-color: #2D2D30;
            }
            QPushButton:disabled {
                background-color: #2A2A2C;
                color: #6D6D6D;
                border: 1px solid #333333;
            }
            QPushButton#exportButton {
                background-color: #007ACC;
                border: none;
                border-radius: 8px;
                font-weight: bold;
                font-size: 11pt;
                color: #FFFFFF;
            }
            QPushButton#exportButton:hover {
                background-color: #1C97EA;
            }
            QPushButton#exportButton:pressed {
                background-color: #005A9E;
            }
            QPushButton#exportButton:disabled {
                background-color: #2A4A5A;
                color: #7A7A7A;
            }

            QCheckBox {
                spacing: 8px;
                color: #E0E0E0;
                background-color: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #3E3E42;
                background-color: #282828;
            }
            QCheckBox::indicator:hover {
                border: 1px solid #4FC3F7;
            }
            QCheckBox::indicator:checked {
                background-color: #4FC3F7;
                border: 1px solid #4FC3F7;
                image: url(:ui/checkmark.png);
            }
            QCheckBox::indicator:disabled {
                background-color: #262628;
                border: 1px solid #2F2F31;
            }

            QProgressBar {
                border: 1px solid #3E3E42;
                border-radius: 6px;
                background-color: #2D2D30;
                text-align: center;
                height: 22px;
                color: #E0E0E0;
            }
            QProgressBar::chunk {
                background-color: #4FC3F7;
                border-radius: 5px;
            }

            QScrollBar:vertical {
                background: #252526;
                width: 10px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #3E3E42;
                border-radius: 5px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #4FC3F7;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QScrollBar:horizontal {
                background: #252526;
                height: 10px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #3E3E42;
                border-radius: 5px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #4FC3F7;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
            }

            QToolTip {
                background-color: #2D2D30;
                color: #E0E0E0;
                border: 1px solid #4FC3F7;
                padding: 4px 6px;
                border-radius: 4px;
            }

            QMessageBox {
                background-color: #252526;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
            }
        """)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(2000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
