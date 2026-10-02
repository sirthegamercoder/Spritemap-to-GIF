import json
import math
import warnings
from typing import Any, Dict, Generator, Iterator, List, Optional, Set, Tuple

import numpy as np
from PIL import Image, ImageChops, ImageColor

IDENTITY_M3D: Tuple[float, ...] = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)

LOOP_MAP = {
    "loop": "LP",
    "play once": "PO",
    "single frame": "SF",
    "singleframe": "SF",
}

SYMBOL_TYPE_MAP = {
    "graphic": "G",
    "graphics": "G",
    "movie clip": "MC",
    "movieclip": "MC",
    "button": "BTN",
}

FrameTuple = Tuple[str, Image.Image, Tuple[int, int, int, int, int, int]]


def strip_trailing_digits(name: str) -> str:
    stripped = name.rstrip("0123456789")
    return stripped or name


def _safe_float(value: Optional[Any], default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Optional[Any], default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return bool(value)


def _get_first(container: Optional[Dict[str, Any]], *keys: str) -> Any:
    if not isinstance(container, dict):
        return None
    for key in keys:
        if key in container:
            return container[key]
    lowercase_map = None
    for key in keys:
        if not isinstance(key, str):
            continue
        if lowercase_map is None:
            lowercase_map = {
                existing_key.lower(): existing_key
                for existing_key in container.keys()
                if isinstance(existing_key, str)
            }
        match = lowercase_map.get(key.lower())
        if match is not None:
            return container[match]
    return None


def _mx_to_m3d(mx: List[float]) -> List[float]:
    a, b, c, d, tx, ty = mx
    return [
        float(a),
        float(b),
        0.0,
        0.0,
        float(c),
        float(d),
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        float(tx),
        float(ty),
        0.0,
        1.0,
    ]


def _convert_mx_to_m3d_recursive(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if key == "MX" and isinstance(value, list) and len(value) == 6:
                result["M3D"] = _mx_to_m3d(value)
            elif key == "M3D":
                result[key] = value
            else:
                result[key] = _convert_mx_to_m3d_recursive(value)
        return result
    if isinstance(data, list):
        return [_convert_mx_to_m3d_recursive(item) for item in data]
    return data


def normalize_animation_document(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return data

    if "AN" in data and "SD" in data:
        return _convert_mx_to_m3d_recursive(data)

    verbose_key_candidates = {
        key.lower(): key for key in data.keys() if isinstance(key, str)
    }
    has_verbose_keys = (
        "animation" in verbose_key_candidates
        or "symbol_dictionary" in verbose_key_candidates
    )
    if not has_verbose_keys:
        return _convert_mx_to_m3d_recursive(data)

    normalized = dict(data)

    animation_section = _normalize_animation_section(
        _get_first(data, "ANIMATION", "animation")
    )
    if animation_section:
        normalized["AN"] = animation_section

    symbol_dictionary = _normalize_symbol_dictionary(
        _get_first(data, "SYMBOL_DICTIONARY", "symbolDictionary")
    )
    if symbol_dictionary:
        normalized["SD"] = symbol_dictionary

    metadata_section = _normalize_metadata(_get_first(data, "metadata", "MD"))
    if metadata_section:
        merged = dict(normalized.get("MD", {}))
        merged.update(metadata_section)
        normalized["MD"] = merged

    return normalized


def _normalize_animation_section(section: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(section, dict):
        return {}

    timeline = _normalize_timeline(_get_first(section, "TIMELINE", "timeline"))
    normalized: Dict[str, Any] = {}
    name = _get_first(section, "name", "NAME")
    if name:
        normalized["N"] = name
    symbol_name = _get_first(
        section, "SYMBOL_name", "symbol_name", "SYMBOLName", "symbolName"
    )
    if symbol_name:
        normalized["SN"] = symbol_name
    if timeline:
        normalized["TL"] = timeline
    return normalized


def _normalize_symbol_dictionary(section: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(section, dict):
        return {}

    symbols: List[Dict[str, Any]] = []
    symbols_source = _get_first(section, "Symbols", "symbols") or []
    for symbol in symbols_source:
        if not isinstance(symbol, dict):
            continue
        normalized_symbol: Dict[str, Any] = {}
        symbol_name = _get_first(
            symbol, "SYMBOL_name", "symbol_name", "SYMBOLName", "symbolName"
        )
        if symbol_name:
            normalized_symbol["SN"] = symbol_name
        timeline = _normalize_timeline(_get_first(symbol, "TIMELINE", "timeline"))
        if timeline:
            normalized_symbol["TL"] = timeline
        if normalized_symbol:
            symbols.append(normalized_symbol)
    if symbols:
        return {"S": symbols}
    return {}


def _normalize_timeline(section: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(section, dict):
        return {}

    layers_source = _get_first(section, "LAYERS", "layers") or []
    layers = [
        layer for layer in (_normalize_layer(entry) for entry in layers_source) if layer
    ]
    if layers:
        return {"L": layers}
    return {}


def _normalize_layer(layer: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(layer, dict):
        return {}

    frames_source = _get_first(layer, "Frames", "frames", "FRAMES") or []
    frames = [
        frame for frame in (_normalize_frame(entry) for entry in frames_source) if frame
    ]
    normalized: Dict[str, Any] = {}
    name = _get_first(layer, "Layer_name", "layerName", "name")
    if name:
        normalized["LN"] = name
    if frames:
        normalized["FR"] = frames
    layer_type = (_get_first(layer, "layerType", "Layer_type") or "").lower()
    if _truthy(layer.get("isClippingLayer")) or layer_type == "clipping":
        normalized["LT"] = "Clp"
    if _truthy(layer.get("hasClippingMask")):
        normalized["Clpb"] = True
    return normalized


def _normalize_frame(frame: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(frame, dict):
        return {}

    elements_source = _get_first(frame, "E", "elements", "Elements") or []
    elements = [
        element
        for element in (_normalize_element(entry) for entry in elements_source)
        if element
    ]
    normalized: Dict[str, Any] = {
        "I": _safe_int(_get_first(frame, "I", "index"), default=0),
        "DU": max(1, _safe_int(_get_first(frame, "DU", "duration"), default=1)),
        "E": elements,
    }
    label = _get_first(frame, "N", "name", "label")
    if label:
        normalized["N"] = label
    return normalized


def _normalize_element(element: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(element, dict):
        return {}

    if "ASI" in element:
        return element
    if "SI" in element:
        bitmap_instance = _normalize_symbol_bitmap(element.get("SI"))
        if bitmap_instance:
            return {"ASI": bitmap_instance}
        return element

    symbol_instance = _get_first(
        element, "SI", "SYMBOL_Instance", "symbol_instance", "symbolInstance"
    )
    if symbol_instance is not None:
        bitmap_instance = _normalize_symbol_bitmap(symbol_instance)
        if bitmap_instance:
            return {"ASI": bitmap_instance}
        symbol = _normalize_symbol_instance(symbol_instance)
        if symbol:
            return {"SI": symbol}
    atlas_instance = _get_first(
        element,
        "ASI",
        "ATLAS_SPRITE_instance",
        "atlas_sprite_instance",
        "atlasSpriteInstance",
    )
    if atlas_instance is not None:
        atlas = _normalize_atlas_instance(atlas_instance)
        if atlas:
            return {"ASI": atlas}
    return {}


def _normalize_symbol_instance(instance: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(instance, dict):
        return {}

    normalized: Dict[str, Any] = {}
    symbol_name = _get_first(
        instance, "SN", "SYMBOL_name", "symbol_name", "SYMBOLName", "symbolName"
    )
    if symbol_name:
        normalized["SN"] = symbol_name
    instance_name = _get_first(instance, "IN", "Instance_Name", "instanceName")
    if instance_name is not None:
        normalized["IN"] = instance_name
    symbol_type = (
        _get_first(instance, "ST", "symbolType", "symbol_type") or ""
    ).lower()
    normalized["ST"] = SYMBOL_TYPE_MAP.get(symbol_type, symbol_type.upper() or "G")
    normalized["FF"] = _safe_int(_get_first(instance, "FF", "firstFrame"), default=0)
    loop_mode = _get_first(instance, "LP", "loop", "Loop")
    if isinstance(loop_mode, str):
        normalized["LP"] = LOOP_MAP.get(loop_mode.lower(), loop_mode)
    elif loop_mode is not None:
        normalized["LP"] = loop_mode
    transform_point = _get_first(
        instance, "TRP", "transformationPoint", "transformPoint"
    )
    if isinstance(transform_point, dict):
        normalized["TRP"] = {
            "x": float(transform_point.get("x", 0.0)),
            "y": float(transform_point.get("y", 0.0)),
        }
    matrix_source = _get_first(instance, "M3D", "Matrix3D")
    if matrix_source is None:
        matrix_source = _get_first(instance, "MX")
    if matrix_source is None:
        matrix_source = _matrix_from_decomposed(instance)
    normalized["M3D"] = _normalize_matrix(matrix_source)
    color_effect = _get_first(instance, "C", "colorEffect", "colourEffect")
    if color_effect is not None:
        normalized["C"] = color_effect
    return normalized


def _normalize_symbol_bitmap(instance: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(instance, dict):
        return {}
    bitmap = instance.get("bitmap")
    if not isinstance(bitmap, dict):
        return {}
    name = bitmap.get("name")
    if not name:
        return {}
    normalized: Dict[str, Any] = {"N": name}
    matrix_source = _get_first(instance, "M3D", "Matrix3D")
    if matrix_source is None:
        matrix_source = _get_first(instance, "MX")
    if matrix_source is None:
        matrix_source = _matrix_from_decomposed(instance)
    normalized["M3D"] = _normalize_matrix(matrix_source)
    return normalized


def _normalize_atlas_instance(instance: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(instance, dict):
        return {}

    normalized: Dict[str, Any] = {}
    name = _get_first(instance, "N", "name", "Name")
    if name:
        normalized["N"] = name
    matrix_source = _get_first(instance, "M3D", "Matrix3D", "matrix3D")
    if matrix_source is None:
        matrix_source = _get_first(instance, "MX")
    if matrix_source is None:
        matrix_source = _matrix_from_decomposed(instance)
    normalized["M3D"] = _normalize_matrix(matrix_source)
    return normalized


def _matrix_from_decomposed(
    instance: Optional[Dict[str, Any]],
) -> Optional[List[float]]:
    if not isinstance(instance, dict):
        return None

    containers = []
    decomposed = _get_first(instance, "DecomposedMatrix", "decomposedMatrix")
    if isinstance(decomposed, dict):
        containers.append(decomposed)
    containers.append(instance)

    position = rotation = scaling = None
    for container in containers:
        if position is None:
            position = _get_first(container, "Position", "position")
        if rotation is None:
            rotation = _get_first(container, "Rotation", "rotation")
        if scaling is None:
            scaling = _get_first(container, "Scaling", "scaling", "Scale", "scale")

    if position is None and rotation is None and scaling is None:
        return None

    translate_x = _safe_float(position.get("x"), default=0.0) if position else 0.0
    translate_y = _safe_float(position.get("y"), default=0.0) if position else 0.0

    angle_z = 0.0
    if rotation:
        angle_z = math.radians(_safe_float(rotation.get("z"), default=0.0))
    scale_x = _safe_float(scaling.get("x"), default=1.0) if scaling else 1.0
    scale_y = _safe_float(scaling.get("y"), default=1.0) if scaling else 1.0

    cos_z = math.cos(angle_z)
    sin_z = math.sin(angle_z)

    matrix = list(IDENTITY_M3D)
    matrix[0] = scale_x * cos_z
    matrix[4] = -scale_y * sin_z
    matrix[1] = scale_x * sin_z
    matrix[5] = scale_y * cos_z
    matrix[12] = translate_x
    matrix[13] = translate_y
    return matrix


def _normalize_matrix(matrix: Optional[Any]) -> List[float]:
    if isinstance(matrix, list) and len(matrix) == 16:
        return list(matrix)
    if isinstance(matrix, list) and len(matrix) == 6:
        return _mx_to_m3d(matrix)
    if isinstance(matrix, dict):
        return [
            _get_first(matrix, "m00", "M00") or 1.0,
            _get_first(matrix, "m01", "M01") or 0.0,
            _get_first(matrix, "m02", "M02") or 0.0,
            _get_first(matrix, "m03", "M03") or 0.0,
            _get_first(matrix, "m10", "M10") or 0.0,
            _get_first(matrix, "m11", "M11") or 1.0,
            _get_first(matrix, "m12", "M12") or 0.0,
            _get_first(matrix, "m13", "M13") or 0.0,
            _get_first(matrix, "m20", "M20") or 0.0,
            _get_first(matrix, "m21", "M21") or 0.0,
            _get_first(matrix, "m22", "M22") or 1.0,
            _get_first(matrix, "m23", "M23") or 0.0,
            _get_first(matrix, "m30", "M30") or 0.0,
            _get_first(matrix, "m31", "M31") or 0.0,
            _get_first(matrix, "m32", "M32") or 0.0,
            _get_first(matrix, "m33", "M33") or 1.0,
        ]
    return list(IDENTITY_M3D)


def _normalize_metadata(section: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(section, dict):
        return {}
    framerate = section.get("framerate") or section.get("frameRate")
    if framerate is None:
        return {}
    try:
        framerate_value = float(framerate)
    except (TypeError, ValueError):
        return {}
    return {"FRT": framerate_value}


def _collect_symbol_refs_from_layers(layers: Optional[List[dict]]) -> Set[str]:
    refs: Set[str] = set()
    if not layers:
        return refs
    for layer in layers:
        for frame in layer.get("FR", []):
            for element in frame.get("E", []):
                si = element.get("SI")
                if si:
                    name = si.get("SN")
                    if name:
                        refs.add(name)
    return refs


def collect_referenced_symbols(animation_json: dict) -> Set[str]:
    root_layers = animation_json.get("AN", {}).get("TL", {}).get("L", [])
    symbols_by_name: Dict[str, List[dict]] = {}
    for symbol in animation_json.get("SD", {}).get("S", []):
        name = symbol.get("SN")
        if name:
            symbols_by_name[name] = symbol.get("TL", {}).get("L", [])

    referenced: Set[str] = set()
    stack = list(_collect_symbol_refs_from_layers(root_layers))
    while stack:
        name = stack.pop()
        if name in referenced:
            continue
        referenced.add(name)
        child_layers = symbols_by_name.get(name)
        if child_layers:
            for child in _collect_symbol_refs_from_layers(child_layers):
                if child not in referenced:
                    stack.append(child)
    return referenced


def collect_direct_child_symbols(animation_json: dict) -> Set[str]:
    root_layers = animation_json.get("AN", {}).get("TL", {}).get("L", [])
    return _collect_symbol_refs_from_layers(root_layers)


def _frame_duration(frame: dict) -> int:
    duration = frame.get("DU", 1)
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 1
    return max(1, duration)


def compute_layers_length(layers: Optional[List[dict]]) -> int:
    if not layers:
        return 0

    total = 0
    for layer in layers:
        frames = layer.get("FR", []) or []
        if not frames:
            continue
        last_frame = frames[-1]
        start_index = int(last_frame.get("I", 0))
        total = max(total, start_index + _frame_duration(last_frame))
    return total


def compute_symbol_lengths(animation_json: dict) -> Dict[str, int]:
    lengths: Dict[str, int] = {}
    for symbol in animation_json.get("SD", {}).get("S", []):
        name = symbol.get("SN")
        if not name:
            continue
        layers = symbol.get("TL", {}).get("L", [])
        lengths[name] = compute_layers_length(layers)
    return lengths


def _extract_label_ranges_from_layers(
    layers: Optional[List[dict]],
) -> List[Dict[str, int]]:
    labels: List[Dict[str, int]] = []
    if not layers:
        return labels

    for layer in layers:
        frames = layer.get("FR", [])
        for frame in frames:
            label_name = frame.get("N")
            if not label_name:
                continue
            start = int(frame.get("I", 0))
            duration = _frame_duration(frame)
            labels.append({"name": label_name, "start": start, "end": start + duration})
        if labels:
            break

    labels.sort(key=lambda item: item["start"])
    unique: List[Dict[str, int]] = []
    seen = set()
    for entry in labels:
        if entry["name"] in seen:
            continue
        seen.add(entry["name"])
        unique.append(entry)
    return unique


def extract_label_ranges(
    animation_json: dict, symbol_name: Optional[str] = None
) -> List[Dict[str, int]]:
    if symbol_name is None:
        layers = animation_json.get("AN", {}).get("TL", {}).get("L", [])
    else:
        for symbol in animation_json.get("SD", {}).get("S", []):
            if symbol.get("SN") == symbol_name:
                layers = symbol.get("TL", {}).get("L", [])
                break
        else:
            layers = []
    return _extract_label_ranges_from_layers(layers)


def extract_label_ranges_from_layers(
    layers: Optional[List[dict]],
) -> List[Dict[str, int]]:
    return _extract_label_ranges_from_layers(layers)


class TransformMatrix:
    def __init__(self, m=None, a=1, b=0, c=0, d=0, e=1, f=0):
        if m is None:
            self.m = np.array([[a, b, c], [d, e, f], [0, 0, 1]], dtype=float)
        else:
            self.m = m

    @classmethod
    def parse(cls, matrix_values):
        if not matrix_values or len(matrix_values) < 16:
            return cls()
        return cls(
            a=matrix_values[0],
            b=matrix_values[4],
            c=matrix_values[12],
            d=matrix_values[1],
            e=matrix_values[5],
            f=matrix_values[13],
        )

    def data(self):
        return np.linalg.inv(self.m).reshape(-1)[:6]

    def __matmul__(self, other):
        if not isinstance(other, TransformMatrix):
            raise TypeError(f"expected TransformMatrix, got {type(other)!r}")
        return TransformMatrix(m=self.m @ other.m)

    def __repr__(self):
        a, b, c, d, e, f = self.m.reshape(-1)[:6]
        return f"TransformMatrix(a={a}, b={b}, c={c}, d={d}, e={e}, f={f})"

    def __eq__(self, other):
        return (
            isinstance(other, TransformMatrix) and self.m.tobytes() == other.m.tobytes()
        )

    def __hash__(self):
        return hash(self.m.tobytes())


class ColorEffect:
    def __init__(self, effect=None):
        self.effect = effect

    @classmethod
    def parse(cls, effect):
        mode = effect.get("M")
        if mode == "AD":
            multiplier = np.array(
                [effect["RM"], effect["GM"], effect["BM"], effect["AM"]]
            )
            offset = np.array([effect["RO"], effect["GO"], effect["BO"], effect["AO"]])
        elif mode == "CA":
            multiplier = np.array([1, 1, 1, effect["AM"]])
            offset = np.zeros(4)
        elif mode == "CBRT":
            brightness = max(-1.0, min(1.0, _safe_float(effect["BRT"], 0.0)))
            if brightness < 0:
                multiplier = np.array(
                    [1 + brightness, 1 + brightness, 1 + brightness, 1]
                )
                offset = np.zeros(4)
            else:
                multiplier = np.array(
                    [1 - brightness, 1 - brightness, 1 - brightness, 1]
                )
                offset = brightness * np.array([255, 255, 255, 0])
        elif mode == "T":
            tint_color = ImageColor.getrgb(effect["TC"])
            tint_multiplier = effect["TM"]
            multiplier = np.array(
                [1 - tint_multiplier, 1 - tint_multiplier, 1 - tint_multiplier, 1]
            )
            offset = tint_multiplier * np.array([*tint_color, 0])
        else:
            warnings.warn(f"Unsupported color effect: {effect}")
            return cls()

        return cls((multiplier, offset))

    def __call__(self, image):
        if self.effect is None:
            return image

        mode = image.mode
        image = image.convert("RGBA")
        multiplier, offset = self.effect
        arr = (np.array(image) * multiplier + offset).clip(0, 255).astype("uint8")
        try:
            image = Image.fromarray(arr, mode="RGBA")
        except TypeError:
            h, w = arr.shape[:2]
            image = Image.frombytes("RGBA", (w, h), arr.tobytes())
        return image.convert(mode)

    def __eq__(self, other):
        if not isinstance(other, ColorEffect):
            return False
        if self.effect is None or other.effect is None:
            return self.effect is other.effect
        multiplier_self, offset_self = self.effect
        multiplier_other, offset_other = other.effect
        return (
            multiplier_self.tobytes() == multiplier_other.tobytes()
            and offset_self.tobytes() == offset_other.tobytes()
        )

    def __hash__(self):
        if self.effect is None:
            return hash(None)
        multiplier, offset = self.effect
        return hash((multiplier.tobytes(), offset.tobytes()))

    def __matmul__(self, other):
        if not isinstance(other, ColorEffect):
            raise TypeError(f"expected ColorEffect, got {type(other)!r}")
        if self.effect is None:
            return other
        if other.effect is None:
            return self
        multiplier_self, offset_self = self.effect
        multiplier_other, offset_other = other.effect
        return ColorEffect(
            (
                multiplier_self * multiplier_other,
                multiplier_self * offset_other + offset_self,
            )
        )

    def __repr__(self):
        return f"ColorEffect({self.effect!r})"


class SpriteAtlas:
    def __init__(self, spritemap_json, atlas_image, canvas_size, resample):
        if atlas_image.mode == "P":
            atlas_image = atlas_image.convert("RGBA")
        self.img = atlas_image.convert("RGBa")
        self.canvas_width, self.canvas_height = canvas_size
        self.resample = resample
        self.sprite_info = {}
        self.sprites = {}
        self._composed_cache = {}
        self._composed_cache_limit = 512

        actual_w, actual_h = atlas_image.size
        meta_size = spritemap_json.get("meta", {}).get("size", {})
        declared_w = meta_size.get("w", actual_w)
        declared_h = meta_size.get("h", actual_h)
        self._scale_x = actual_w / declared_w if declared_w > 0 else 1.0
        self._scale_y = actual_h / declared_h if declared_h > 0 else 1.0
        self._needs_rescale = not (
            0.99 < self._scale_x < 1.01 and 0.99 < self._scale_y < 1.01
        )

        for sprite in spritemap_json.get("ATLAS", {}).get("SPRITES", []):
            data = sprite["SPRITE"] if "SPRITE" in sprite else sprite
            x = data.get("x", 0)
            y = data.get("y", 0)
            w = data.get("w", 0)
            h = data.get("h", 0)
            self.sprite_info[data["name"]] = {
                "box": (x, y, x + w, y + h),
                "rotated": data.get("rotated", False),
            }

    def get_sprite(self, name, matrix: TransformMatrix, color: ColorEffect):
        cache_key = None
        if matrix is not None and color is not None:
            cache_key = (
                name,
                matrix.m.tobytes(),
                hash(color),
            )
            cached = self._composed_cache.get(cache_key)
            if cached is not None:
                return cached

        if name not in self.sprites:
            sprite_info = self.sprite_info.get(name)
            if sprite_info is None:
                return None, None
            box = sprite_info["box"]
            if self._needs_rescale:
                scaled_box = (
                    round(box[0] * self._scale_x),
                    round(box[1] * self._scale_y),
                    round(box[2] * self._scale_x),
                    round(box[3] * self._scale_y),
                )
                sprite = self.img.crop(scaled_box)
                orig_w = box[2] - box[0]
                orig_h = box[3] - box[1]
                if sprite.size != (orig_w, orig_h):
                    sprite = sprite.resize((orig_w, orig_h), self.resample)
            else:
                sprite = self.img.crop(box)
            if sprite_info.get("rotated"):
                sprite = sprite.transpose(Image.ROTATE_90)
            self.sprites[name] = sprite
        else:
            sprite = self.sprites[name]

        width, height = sprite.size
        corners = matrix.m @ np.array(
            [[0, width, 0, width], [0, 0, height, height], [1, 1, 1, 1]]
        )

        min_x = math.floor(min(corners[0]))
        max_x = math.ceil(max(corners[0]))
        min_y = math.floor(min(corners[1]))
        max_y = math.ceil(max(corners[1]))

        if (
            max_x < 0
            or self.canvas_width <= min_x
            or max_y < 0
            or self.canvas_height <= min_y
        ):
            warnings.warn(
                f"Sprite `{name}` is out of bounds, increase canvas size: "
                f"({min_x:.2f}, {min_y:.2f}) x ({max_x:.2f}, {max_y:.2f})"
            )
            return None, None

        min_x = max(0, min_x)
        max_x = min(self.canvas_width - 1, max_x)
        min_y = max(0, min_y)
        max_y = min(self.canvas_height - 1, max_y)

        transform_size = (max_x - min_x + 1, max_y - min_y + 1)
        matrix = TransformMatrix(c=-min_x, f=-min_y) @ matrix
        sprite = color(sprite)
        sprite = sprite.transform(
            transform_size, Image.AFFINE, data=matrix.data(), resample=self.resample
        )
        sprite = sprite.convert("RGBA")
        result = (sprite, (min_x, min_y))

        if cache_key is not None:
            if len(self._composed_cache) >= self._composed_cache_limit:
                self._composed_cache.pop(next(iter(self._composed_cache)), None)
            self._composed_cache[cache_key] = result

        return result

    def close(self) -> None:
        if getattr(self, "sprites", None):
            for sprite in list(self.sprites.values()):
                try:
                    if sprite is not None:
                        sprite.close()
                except Exception:
                    pass
            self.sprites.clear()

        if getattr(self, "_composed_cache", None):
            for sprite, _ in self._composed_cache.values():
                try:
                    if sprite is not None:
                        sprite.close()
                except Exception:
                    pass
            self._composed_cache.clear()

        if getattr(self, "img", None) is not None:
            try:
                self.img.close()
            except Exception:
                pass
            finally:
                self.img = None


def _union_bounds(
    a: Optional[Tuple[int, int, int, int]],
    b: Optional[Tuple[int, int, int, int]],
) -> Optional[Tuple[int, int, int, int]]:
    if a is None:
        return b
    if b is None:
        return a
    return (
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    )


class Symbols:
    def __init__(self, animation_json, sprite_atlas, canvas_size):
        self.background_color = (0, 0, 0, 0)
        self.canvas_size = canvas_size
        self.sprite_atlas = sprite_atlas
        self.timelines = {}

        for symbol in animation_json.get("SD", {}).get("S", []):
            name = symbol.get("SN")
            if name in self.timelines:
                raise ValueError(f"Symbol `{name}` is not unique")
            self.timelines[name] = symbol.get("TL", {}).get("L", [])

        self.timelines[None] = animation_json.get("AN", {}).get("TL", {}).get("L", [])
        self.label_map: Dict[Optional[str], List[Dict[str, int]]] = {
            name: extract_label_ranges_from_layers(layers)
            for name, layers in self.timelines.items()
        }
        self.center_in_canvas = TransformMatrix(
            c=canvas_size[0] // 2, f=canvas_size[1] // 2
        )

    def length(self, symbol_name):
        return compute_layers_length(self.timelines.get(symbol_name))

    def render_symbol(self, name, frame_index):
        canvas = Image.new("RGBA", self.canvas_size, color=self.background_color)
        self._render_symbol(
            canvas, name, frame_index, self.center_in_canvas, ColorEffect()
        )
        return canvas

    def compute_frame_bounds(
        self, name: Optional[str], frame_index: int
    ) -> Optional[Tuple[int, int, int, int]]:
        return self._compute_bounds(name, frame_index, self.center_in_canvas)

    def compute_union_bounds(
        self,
        name: Optional[str],
        start_frame: int,
        end_frame: int,
        padding: int = 2,
    ) -> Optional[Tuple[int, int, int, int]]:
        union: Optional[Tuple[int, int, int, int]] = None
        for frame_index in range(start_frame, end_frame):
            frame_bounds = self._compute_bounds(
                name, frame_index, self.center_in_canvas
            )
            union = _union_bounds(union, frame_bounds)

        if union is None:
            return None

        return (
            max(0, union[0] - padding),
            max(0, union[1] - padding),
            union[2] + padding,
            union[3] + padding,
        )

    def render_symbol_compact(
        self,
        name: Optional[str],
        frame_index: int,
        viewport: Tuple[int, int, int, int],
    ) -> Image.Image:
        vp_x, vp_y, vp_x2, vp_y2 = viewport
        compact_w = max(1, vp_x2 - vp_x)
        compact_h = max(1, vp_y2 - vp_y)

        orig_canvas_size = self.canvas_size
        orig_atlas_w = self.sprite_atlas.canvas_width
        orig_atlas_h = self.sprite_atlas.canvas_height

        self.canvas_size = (compact_w, compact_h)
        self.sprite_atlas.canvas_width = compact_w
        self.sprite_atlas.canvas_height = compact_h

        compact_center = TransformMatrix(
            c=orig_canvas_size[0] // 2 - vp_x,
            f=orig_canvas_size[1] // 2 - vp_y,
        )

        try:
            canvas = Image.new(
                "RGBA", (compact_w, compact_h), color=self.background_color
            )
            self._render_symbol(
                canvas, name, frame_index, compact_center, ColorEffect()
            )
            return canvas
        finally:
            self.canvas_size = orig_canvas_size
            self.sprite_atlas.canvas_width = orig_atlas_w
            self.sprite_atlas.canvas_height = orig_atlas_h

    def _compute_bounds(
        self,
        name: Optional[str],
        frame_index: int,
        matrix: TransformMatrix,
    ) -> Optional[Tuple[int, int, int, int]]:
        combined: Optional[Tuple[int, int, int, int]] = None

        for layer in reversed(self.timelines.get(name, [])):
            frames = layer.get("FR", [])
            frame = self._find_frame(frames, frame_index)
            if frame is None:
                continue

            low = 0
            high = len(frames) - 1
            while low != high:
                mid = (low + high + 1) // 2
                if frame_index < frames[mid]["I"]:
                    high = mid - 1
                else:
                    low = mid
            frame = frames[low]
            if not (frame["I"] <= frame_index < frame["I"] + frame["DU"]):
                continue

            for element in frame.get("E", []):
                if "SI" in element:
                    instance = element["SI"]
                    element_name = instance.get("SN")
                    if not element_name:
                        continue
                    instance_frame = self._resolve_instance_frame(
                        element_name, instance, frame["I"], frame_index
                    )
                    transform = TransformMatrix.parse(instance.get("M3D", IDENTITY_M3D))
                    child_bounds = self._compute_bounds(
                        element_name, instance_frame, matrix @ transform
                    )
                    combined = _union_bounds(combined, child_bounds)
                elif "ASI" in element:
                    atlas_instance = element["ASI"]
                    sprite_name = atlas_instance.get("N")
                    if not sprite_name:
                        continue
                    transform = TransformMatrix.parse(
                        atlas_instance.get("M3D", IDENTITY_M3D)
                    )
                    sprite_bounds = self._compute_sprite_bounds(
                        sprite_name, matrix @ transform
                    )
                    combined = _union_bounds(combined, sprite_bounds)

        return combined

    def _compute_sprite_bounds(
        self,
        sprite_name: str,
        matrix: TransformMatrix,
    ) -> Optional[Tuple[int, int, int, int]]:
        info = self.sprite_atlas.sprite_info.get(sprite_name)
        if not info:
            return None

        box = info["box"]
        width = box[2] - box[0]
        height = box[3] - box[1]
        if info.get("rotated"):
            width, height = height, width

        corners = matrix.m @ np.array(
            [[0, width, 0, width], [0, 0, height, height], [1, 1, 1, 1]]
        )
        return (
            math.floor(float(min(corners[0]))),
            math.floor(float(min(corners[1]))),
            math.ceil(float(max(corners[0]))),
            math.ceil(float(max(corners[1]))),
        )

    def _render_symbol(self, canvas, name, frame_index, matrix, color):
        canvas_stack = []
        for layer in reversed(self.timelines.get(name, [])):
            frames = layer.get("FR", [])
            frame = self._find_frame(frames, frame_index)
            if frame is None:
                continue

            low = 0
            high = len(frames) - 1
            while low != high:
                mid = (low + high + 1) // 2
                if frame_index < frames[mid]["I"]:
                    high = mid - 1
                else:
                    low = mid
            frame = frames[low]
            if not (frame["I"] <= frame_index < frame["I"] + frame["DU"]):
                continue

            if (layer.get("Clpb") and not canvas_stack) or layer.get("LT") == "Clp":
                canvas_stack.append(canvas)
                canvas = Image.new("RGBA", self.canvas_size, color=(0, 0, 0, 0))

            for element in frame.get("E", []):
                if "SI" in element:
                    instance = element["SI"]
                    element_name = instance.get("SN")
                    if not element_name:
                        continue
                    instance_frame = self._resolve_instance_frame(
                        element_name,
                        instance,
                        frame["I"],
                        frame_index,
                    )
                    element_color = (
                        color @ ColorEffect.parse(instance["C"])
                        if "C" in instance
                        else color
                    )
                    transform = TransformMatrix.parse(instance.get("M3D", IDENTITY_M3D))
                    self._render_symbol(
                        canvas,
                        element_name,
                        instance_frame,
                        matrix @ transform,
                        element_color,
                    )
                else:
                    atlas_instance = element.get("ASI", {})
                    sprite_name = atlas_instance.get("N")
                    transform = TransformMatrix.parse(
                        atlas_instance.get("M3D", IDENTITY_M3D)
                    )
                    sprite, dest = self.sprite_atlas.get_sprite(
                        sprite_name, matrix @ transform, color
                    )
                    if sprite is not None:
                        canvas.alpha_composite(sprite, dest=dest)

            if layer.get("LT") == "Clp":
                mask_canvas = canvas
                masked_canvas = canvas_stack.pop()
                base_canvas = canvas_stack.pop()

                mask_bbox = mask_canvas.getbbox()
                if mask_bbox is None:
                    warnings.warn(
                        f"Mask `{layer.get('LN')}` in symbol `{name}` is fully transparent"
                    )
                    base_canvas.alpha_composite(masked_canvas)
                else:
                    mask_canvas = mask_canvas.crop(mask_bbox)
                    masked_canvas = masked_canvas.crop(mask_bbox)
                    masked_alpha = masked_canvas.getchannel("A")

                    mask_alpha = np.array(mask_canvas.getchannel("A"))
                    mask_alpha = (
                        mask_alpha
                        if np.max(mask_alpha) == 0
                        else mask_alpha / np.max(mask_alpha) * 255
                    )
                    mask_alpha_arr = mask_alpha.clip(0, 255).astype("uint8")
                    try:
                        mask_alpha = Image.fromarray(mask_alpha_arr, "L")
                    except TypeError:
                        h, w = mask_alpha_arr.shape[:2]
                        mask_alpha = Image.frombytes(
                            "L",
                            (w, h),
                            np.ascontiguousarray(mask_alpha_arr).tobytes(),
                        )
                    masked_canvas.putalpha(
                        ImageChops.multiply(masked_alpha, mask_alpha)
                    )
                    base_canvas.alpha_composite(masked_canvas, dest=mask_bbox[:2])

                canvas = base_canvas

    def _resolve_instance_frame(
        self,
        symbol_name: str,
        instance: dict,
        frame_start: int,
        parent_frame_index: int,
    ) -> int:
        symbol_length = self.length(symbol_name)
        if symbol_length <= 0:
            return 0

        first_frame = _safe_int(instance.get("FF", 0), default=0)
        first_frame = max(0, min(first_frame, symbol_length - 1))

        symbol_type = (instance.get("ST") or "G").upper()
        if symbol_type == "MC":
            return first_frame

        offset = max(0, parent_frame_index - frame_start)

        loop_mode = instance.get("LP") or "LP"
        if isinstance(loop_mode, str):
            loop_mode = loop_mode.upper()
        else:
            loop_mode = "LP"

        if loop_mode == "SF":
            return first_frame

        target = first_frame + offset
        if loop_mode == "PO":
            return min(target, symbol_length - 1)

        return target % symbol_length

    @staticmethod
    def _find_frame(frames: List[dict], frame_index: int) -> Optional[dict]:
        if not frames:
            return None
        low = 0
        high = len(frames) - 1
        while low != high:
            mid = (low + high + 1) // 2
            if frame_index < frames[mid]["I"]:
                high = mid - 1
            else:
                low = mid
        frame = frames[low]
        if frame["I"] <= frame_index < frame["I"] + frame["DU"]:
            return frame
        return None

    def get_label_ranges(self, symbol_name: Optional[str]):
        return self.label_map.get(symbol_name, [])

    def get_label_range(self, symbol_name: Optional[str], label_name: str):
        for entry in self.label_map.get(symbol_name, []):
            if entry["name"] == label_name:
                return entry
        return None

    def close(self) -> None:
        self.sprite_atlas = None
        self.timelines.clear()
        self.label_map.clear()


def _collect_sprite_sizes(
    spritemap_json: Dict[str, Any],
) -> Dict[str, Tuple[int, int]]:
    sizes: Dict[str, Tuple[int, int]] = {}
    for sprite in spritemap_json.get("ATLAS", {}).get("SPRITES", []):
        data = sprite.get("SPRITE", sprite)
        name = data.get("name")
        if not name:
            continue
        sizes[name] = (int(data.get("w", 0)), int(data.get("h", 0)))
    return sizes


def _collect_timelines(animation_json: Dict[str, Any]):
    timelines: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for symbol in animation_json.get("SD", {}).get("S", []):
        name = symbol.get("SN")
        if not name:
            continue
        timelines[name] = symbol.get("TL", {}).get("L", [])
    timelines[None] = animation_json.get("AN", {}).get("TL", {}).get("L", [])
    return timelines


def _transform_bounds(
    bounds: Tuple[float, float, float, float], matrix: TransformMatrix
) -> Tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = bounds
    corners = (
        (min_x, min_y),
        (min_x, max_y),
        (max_x, min_y),
        (max_x, max_y),
    )
    a, b, c, d, e, f = matrix.m.reshape(-1)[:6]
    transformed = ((a * x + b * y + c, d * x + e * y + f) for (x, y) in corners)
    xs, ys = zip(*transformed)
    return (min(xs), min(ys), max(xs), max(ys))


def _element_bounds(element, sprite_sizes, symbol_bounds_fn):
    if not isinstance(element, dict):
        return None
    if "ASI" in element:
        atlas = element["ASI"]
        sprite_name = atlas.get("N")
        if not sprite_name:
            return None
        sprite_size = sprite_sizes.get(sprite_name)
        if not sprite_size:
            return None
        matrix = TransformMatrix.parse(atlas.get("M3D"))
        width, height = sprite_size
        return _transform_bounds((0.0, 0.0, float(width), float(height)), matrix)
    if "SI" in element:
        instance = element["SI"]
        child_name = instance.get("SN")
        if not child_name:
            return None
        child_bounds = symbol_bounds_fn(child_name)
        if not child_bounds:
            return None
        matrix = TransformMatrix.parse(instance.get("M3D"))
        return _transform_bounds(child_bounds, matrix)
    return None


def _infer_canvas_size(animation_json, spritemap_json, atlas_size):
    sprite_sizes = _collect_sprite_sizes(spritemap_json)
    if not sprite_sizes:
        return atlas_size

    timelines = _collect_timelines(animation_json)
    if not timelines:
        return atlas_size

    bounds_cache: Dict[Optional[str], Optional[Tuple[float, float, float, float]]] = {}
    visiting: set[Optional[str]] = set()

    def symbol_bounds(
        symbol_name: Optional[str],
    ) -> Optional[Tuple[float, float, float, float]]:
        if symbol_name in bounds_cache:
            return bounds_cache[symbol_name]
        if symbol_name in visiting:
            return None
        visiting.add(symbol_name)

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")

        for layer in timelines.get(symbol_name, []):
            for frame in layer.get("FR", []):
                for element in frame.get("E", []):
                    bounds = _element_bounds(element, sprite_sizes, symbol_bounds)
                    if not bounds:
                        continue
                    min_x = min(min_x, bounds[0])
                    min_y = min(min_y, bounds[1])
                    max_x = max(max_x, bounds[2])
                    max_y = max(max_y, bounds[3])

        visiting.remove(symbol_name)

        if min_x == float("inf"):
            bounds_cache[symbol_name] = None
        else:
            bounds_cache[symbol_name] = (min_x, min_y, max_x, max_y)
        return bounds_cache[symbol_name]

    for symbol_name in timelines.keys():
        symbol_bounds(symbol_name)

    max_abs_x = 0.0
    max_abs_y = 0.0
    for bounds in bounds_cache.values():
        if not bounds:
            continue
        min_x, min_y, max_x, max_y = bounds
        max_abs_x = max(max_abs_x, abs(min_x), abs(max_x))
        max_abs_y = max(max_abs_y, abs(min_y), abs(max_y))

    if max_abs_x == 0 and max_abs_y == 0:
        return atlas_size

    padding = 32.0
    inferred_width = int(math.ceil(max_abs_x * 2.0 + padding))
    inferred_height = int(math.ceil(max_abs_y * 2.0 + padding))

    MAX_CANVAS_DIM = 16384
    if inferred_width > MAX_CANVAS_DIM or inferred_height > MAX_CANVAS_DIM:
        warnings.warn(
            f"Inferred canvas size ({inferred_width}x{inferred_height}) exceeds "
            f"the {MAX_CANVAS_DIM}px safety cap; clamping."
        )
        inferred_width = min(inferred_width, MAX_CANVAS_DIM)
        inferred_height = min(inferred_height, MAX_CANVAS_DIM)

    return (
        max(atlas_size[0], inferred_width),
        max(atlas_size[1], inferred_height),
    )


class AdobeSpritemapRenderer:
    def __init__(
        self,
        animation_path: str,
        spritemap_json_path: str,
        atlas_image_path: str,
        canvas_size=None,
        resample=Image.BICUBIC,
        filter_single_frame: bool = True,
        filter_unused_symbols: bool = False,
        root_animation_only: bool = False,
    ):
        self.animation_path = animation_path
        self.spritemap_json_path = spritemap_json_path
        self.atlas_image_path = atlas_image_path

        with open(animation_path, "r", encoding="utf-8-sig") as animation_file:
            self.animation_json = normalize_animation_document(
                json.load(animation_file)
            )

        with open(spritemap_json_path, "rb") as spritemap_file:
            spritemap_json = json.loads(spritemap_file.read().decode("utf-8-sig"))

        atlas_image = Image.open(atlas_image_path)

        if canvas_size is None:
            canvas_size = _infer_canvas_size(
                self.animation_json,
                spritemap_json,
                atlas_image.size,
            )
        self.frame_rate = self.animation_json.get("MD", {}).get("FRT", 24)
        self.filter_single_frame = filter_single_frame
        self.filter_unused_symbols = filter_unused_symbols
        self.root_animation_only = root_animation_only

        direct_children = collect_direct_child_symbols(self.animation_json)
        if direct_children:
            self._referenced_symbols = direct_children
        elif filter_unused_symbols:
            self._referenced_symbols = collect_referenced_symbols(self.animation_json)
        else:
            self._referenced_symbols = None

        self.sprite_atlas = SpriteAtlas(
            spritemap_json, atlas_image, canvas_size, resample
        )
        self.symbols = Symbols(self.animation_json, self.sprite_atlas, canvas_size)

    def get_root_animation_name(self) -> Optional[str]:
        an = self.animation_json.get("AN", {})
        return an.get("SN") or an.get("N") or None

    def is_symbol_referenced(self, symbol_name: str) -> bool:
        if self._referenced_symbols is None:
            return True
        return symbol_name in self._referenced_symbols

    def list_symbol_names(self, include_all: bool = False) -> List[str]:
        names = [
            symbol.get("SN")
            for symbol in self.animation_json.get("SD", {}).get("S", [])
            if symbol.get("SN")
        ]
        if include_all or self._referenced_symbols is None:
            return names
        return [n for n in names if n in self._referenced_symbols]

    def build_animation_frames(
        self,
    ) -> Dict[str, List[Tuple[str, Image.Image, Tuple[int, int, int, int, int, int]]]]:
        animations: Dict[
            str,
            List[Tuple[str, Image.Image, Tuple[int, int, int, int, int, int]]],
        ] = {}

        root_name = self.get_root_animation_name()
        if root_name:
            root_frames = self._render_symbol_frames(None)
            if root_frames:
                if not self.filter_single_frame or len(root_frames) > 1:
                    animations.setdefault(root_name, []).extend(root_frames)

        if not self.root_animation_only:
            for symbol_name in self.list_symbol_names():
                frames = self._render_symbol_frames(symbol_name)
                if not frames:
                    continue
                if self.filter_single_frame and len(frames) <= 1:
                    continue
                folder_name = strip_trailing_digits(symbol_name)
                animations.setdefault(folder_name, []).extend(frames)

            for label in self.symbols.get_label_ranges(None):
                frames = self._render_symbol_frames(
                    None,
                    start_frame=label["start"],
                    end_frame=label["end"],
                    frame_name_prefix=label["name"],
                )
                if not frames:
                    continue
                if self.filter_single_frame and len(frames) <= 1:
                    continue
                folder_name = label["name"]
                animations.setdefault(folder_name, []).extend(frames)

        return animations

    def iter_animations(
        self,
    ) -> Generator[
        Tuple[str, Iterator[FrameTuple]],
        None,
        None,
    ]:
        root_name = self.get_root_animation_name()
        if root_name:
            total = self.symbols.length(None)
            if not self.filter_single_frame or total > 1:
                yield root_name, self._iter_symbol_frames(None)

        if not self.root_animation_only:
            for symbol_name in self.list_symbol_names():
                total = self.symbols.length(symbol_name)
                if total == 0:
                    continue
                if self.filter_single_frame and total <= 1:
                    continue
                folder_name = strip_trailing_digits(symbol_name)
                yield folder_name, self._iter_symbol_frames(symbol_name)

            for label in self.symbols.get_label_ranges(None):
                total = label["end"] - label["start"]
                if total <= 0:
                    continue
                if self.filter_single_frame and total <= 1:
                    continue
                yield label["name"], self._iter_symbol_frames(
                    None,
                    start_frame=label["start"],
                    end_frame=label["end"],
                    frame_name_prefix=label["name"],
                )

    def _render_symbol_frames(
        self,
        symbol_name: Optional[str],
        start_frame: int = 0,
        end_frame: Optional[int] = None,
        frame_name_prefix: Optional[str] = None,
    ):
        total_frames = self.symbols.length(symbol_name)
        if total_frames == 0:
            return []

        if end_frame is None or end_frame > total_frames:
            end_frame = total_frames

        if start_frame >= end_frame:
            return []

        union_bounds = self.symbols.compute_union_bounds(
            symbol_name, start_frame, end_frame
        )
        if union_bounds is None:
            return []

        frames_with_index: List[Tuple[int, Image.Image]] = []
        for frame_index in range(start_frame, end_frame):
            frame_image = self.symbols.render_symbol_compact(
                symbol_name, frame_index, union_bounds
            )
            if frame_image is not None:
                frames_with_index.append((frame_index - start_frame, frame_image))

        if not frames_with_index:
            return []

        min_x, min_y, max_x, max_y = float("inf"), float("inf"), 0, 0
        for _, frame in frames_with_index:
            bbox = frame.getbbox()
            if bbox:
                min_x = min(min_x, bbox[0])
                min_y = min(min_y, bbox[1])
                max_x = max(max_x, bbox[2])
                max_y = max(max_y, bbox[3])

        if min_x > max_x:
            return []

        prefix = frame_name_prefix or (symbol_name if symbol_name else "timeline")

        rendered_frames: List[
            Tuple[str, Image.Image, Tuple[int, int, int, int, int, int]]
        ] = []
        for frame_index, frame in frames_with_index:
            cropped_frame = frame.crop((min_x, min_y, max_x, max_y))
            frame.close()
            frame_name = f"{prefix}_{frame_index:04d}"
            rendered_frames.append(
                (
                    frame_name,
                    cropped_frame,
                    (0, 0, cropped_frame.width, cropped_frame.height, 0, 0),
                )
            )

        return rendered_frames

    def _iter_symbol_frames(
        self,
        symbol_name: Optional[str],
        start_frame: int = 0,
        end_frame: Optional[int] = None,
        frame_name_prefix: Optional[str] = None,
    ) -> Generator[
        Tuple[str, Image.Image, Tuple[int, int, int, int, int, int]],
        None,
        None,
    ]:
        total_frames = self.symbols.length(symbol_name)
        if total_frames == 0:
            return

        if end_frame is None or end_frame > total_frames:
            end_frame = total_frames

        if start_frame >= end_frame:
            return

        viewport = self.symbols.compute_union_bounds(
            symbol_name, start_frame, end_frame
        )
        if viewport is None:
            return

        prefix = frame_name_prefix or (symbol_name if symbol_name else "timeline")

        for frame_index in range(start_frame, end_frame):
            frame_image = self.symbols.render_symbol_compact(
                symbol_name, frame_index, viewport
            )
            if frame_image is None:
                continue
            frame_name = f"{prefix}_{frame_index - start_frame:04d}"
            yield (
                frame_name,
                frame_image,
                (0, 0, frame_image.width, frame_image.height, 0, 0),
            )

    def ensure_animation_defaults(self, settings_manager, spritesheet_name):
        default_duration_ms = (
            max(1, round(1000 / self.frame_rate)) if self.frame_rate > 0 else 42
        )

        root_name = self.get_root_animation_name()
        if root_name:
            full_name = f"{spritesheet_name}/{root_name}"
            sprite_settings = settings_manager.animation_settings.setdefault(
                full_name, {}
            )
            sprite_settings.setdefault("duration", default_duration_ms)

        if not self.root_animation_only:
            for animation_name in self.list_symbol_names():
                folder_name = strip_trailing_digits(animation_name)
                full_name = f"{spritesheet_name}/{folder_name}"
                sprite_settings = settings_manager.animation_settings.setdefault(
                    full_name, {}
                )
                sprite_settings.setdefault("duration", default_duration_ms)

            for label in self.symbols.get_label_ranges(None):
                label_name = label["name"]
                full_name = f"{spritesheet_name}/{label_name}"
                sprite_settings = settings_manager.animation_settings.setdefault(
                    full_name, {}
                )
                sprite_settings.setdefault("duration", default_duration_ms)

    def render_animation(self, target):
        target_type, target_value = self._normalize_target(target)

        if target_type == "root_animation":
            root_name = self.get_root_animation_name()
            return self._render_symbol_frames(None, frame_name_prefix=root_name)

        if target_type == "timeline_label":
            label_range = self.symbols.get_label_range(None, target_value)
            if not label_range:
                return []
            return self._render_symbol_frames(
                None,
                start_frame=label_range["start"],
                end_frame=label_range["end"],
                frame_name_prefix=target_value,
            )

        return self._render_symbol_frames(target_value)

    def _normalize_target(self, target):
        if isinstance(target, dict):
            return target.get("type", "symbol"), target.get("value")
        return "symbol", target

    def close(self) -> None:
        if getattr(self, "symbols", None):
            try:
                self.symbols.close()
            except Exception:
                pass
            finally:
                self.symbols = None

        if getattr(self, "sprite_atlas", None):
            try:
                self.sprite_atlas.close()
            except Exception:
                pass
            finally:
                self.sprite_atlas = None

        self.animation_json = None
