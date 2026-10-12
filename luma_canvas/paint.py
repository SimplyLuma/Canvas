# SPDX-License-Identifier: MPL-2.0
"""Colors, paints and the current style.

Everything a color control needs that is not drawing: conversions between
the models the picker shows (hex, RGB, HSL, HSB, CSS) and OKLCH input, a
forgiving parser for anything a person pastes into a color field, gradient
paints, and the document's current style that new shapes are drawn with.

Colors are sRGB floats from 0 to 1. Strings are only ever a view of them.
Nothing in this module imports GTK, so every rule here is unit tested.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable


# ── the color value ──────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Color:
    r: float
    g: float
    b: float
    a: float = 1.0

    def clamped(self) -> "Color":
        return Color(_clamp01(self.r), _clamp01(self.g), _clamp01(self.b), _clamp01(self.a))

    def with_alpha(self, alpha: float) -> "Color":
        return Color(self.r, self.g, self.b, _clamp01(alpha))

    def to_dict(self) -> dict[str, float]:
        return {"r": round(self.r, 4), "g": round(self.g, 4), "b": round(self.b, 4),
                "a": round(self.a, 4)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Color":
        return cls(float(value.get("r", 0)), float(value.get("g", 0)),
                   float(value.get("b", 0)), float(value.get("a", 1)))

    def bytes(self) -> tuple[int, int, int]:
        return (_byte(self.r), _byte(self.g), _byte(self.b))


def _clamp01(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else float(value)


def _byte(value: float) -> int:
    return int(round(_clamp01(value) * 255))


BLACK = Color(0, 0, 0, 1)
WHITE = Color(1, 1, 1, 1)


# ── hex ──────────────────────────────────────────────────────────────────

def to_hex(color: Color, *, alpha: bool | None = None, prefix: bool = True) -> str:
    """`#rrggbb`, or `#rrggbbaa` when alpha is below 100% (or asked for)."""
    r, g, b = color.bytes()
    text = f"{r:02x}{g:02x}{b:02x}"
    include = (color.a < 0.9995) if alpha is None else alpha
    if include:
        text += f"{_byte(color.a):02x}"
    return ("#" if prefix else "") + text


def parse_hex(text: str) -> Color | None:
    value = text.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{3,8}", value) or len(value) not in {3, 4, 6, 8}:
        return None
    if len(value) in {3, 4}:
        value = "".join(ch * 2 for ch in value)
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    a = int(value[6:8], 16) / 255 if len(value) == 8 else 1.0
    return Color(r, g, b, a)


# ── HSB (HSV) and HSL ────────────────────────────────────────────────────

def rgb_to_hsv(color: Color) -> tuple[float, float, float]:
    """Hue in degrees 0..360, saturation and brightness 0..1."""
    r, g, b = color.r, color.g, color.b
    high, low = max(r, g, b), min(r, g, b)
    delta = high - low
    if delta <= 1e-12:
        hue = 0.0
    elif high == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif high == g:
        hue = 60 * ((b - r) / delta + 2)
    else:
        hue = 60 * ((r - g) / delta + 4)
    saturation = 0.0 if high <= 1e-12 else delta / high
    return hue % 360, saturation, high


def hsv_to_rgb(hue: float, saturation: float, value: float, alpha: float = 1.0) -> Color:
    hue = hue % 360
    saturation, value = _clamp01(saturation), _clamp01(value)
    chroma = value * saturation
    x = chroma * (1 - abs((hue / 60) % 2 - 1))
    m = value - chroma
    sector = int(hue // 60) % 6
    r, g, b = [(chroma, x, 0), (x, chroma, 0), (0, chroma, x),
               (0, x, chroma), (x, 0, chroma), (chroma, 0, x)][sector]
    return Color(r + m, g + m, b + m, _clamp01(alpha))


def rgb_to_hsl(color: Color) -> tuple[float, float, float]:
    hue, _, _ = rgb_to_hsv(color)
    high, low = max(color.r, color.g, color.b), min(color.r, color.g, color.b)
    lightness = (high + low) / 2
    delta = high - low
    if delta <= 1e-12:
        return hue, 0.0, lightness
    saturation = delta / (1 - abs(2 * lightness - 1)) if abs(2 * lightness - 1) < 1 else 0.0
    return hue, _clamp01(saturation), lightness


def hsl_to_rgb(hue: float, saturation: float, lightness: float, alpha: float = 1.0) -> Color:
    saturation, lightness = _clamp01(saturation), _clamp01(lightness)
    value = lightness + saturation * min(lightness, 1 - lightness)
    hsv_saturation = 0.0 if value <= 1e-12 else 2 * (1 - lightness / value)
    return hsv_to_rgb(hue, hsv_saturation, value, alpha)


# ── OKLab / OKLCH ────────────────────────────────────────────────────────

def _to_linear(channel: float) -> float:
    sign = -1 if channel < 0 else 1
    channel = abs(channel)
    return sign * (channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)


def _from_linear(channel: float) -> float:
    sign = -1 if channel < 0 else 1
    channel = abs(channel)
    return sign * (12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055)


def oklab_to_srgb_unclipped(lightness: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    red = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    green = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    blue = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return _from_linear(red), _from_linear(green), _from_linear(blue)


def srgb_to_oklab(color: Color) -> tuple[float, float, float]:
    red, green, blue = _to_linear(color.r), _to_linear(color.g), _to_linear(color.b)
    l = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    m = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    s = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def srgb_to_oklch(color: Color) -> tuple[float, float, float]:
    lightness, a, b = srgb_to_oklab(color)
    chroma = math.hypot(a, b)
    hue = math.degrees(math.atan2(b, a)) % 360 if chroma > 1e-6 else 0.0
    return lightness, chroma, hue


def oklch_to_srgb(lightness: float, chroma: float, hue: float, alpha: float = 1.0) -> Color:
    """OKLCH to sRGB, gamut mapped by reducing chroma at constant hue.

    Clipping each channel independently shifts hue visibly for saturated
    colors; holding lightness and hue and finding the largest chroma that fits
    is what CSS Color 4 recommends and what reads as "the same color".
    """
    lightness = _clamp01(lightness)

    def rgb_for(c: float) -> tuple[float, float, float]:
        radians = math.radians(hue)
        return oklab_to_srgb_unclipped(lightness, c * math.cos(radians), c * math.sin(radians))

    def inside(rgb: tuple[float, float, float]) -> bool:
        return all(-1e-4 <= channel <= 1 + 1e-4 for channel in rgb)

    rgb = rgb_for(chroma)
    if not inside(rgb):
        low, high = 0.0, max(0.0, chroma)
        for _ in range(24):
            middle = (low + high) / 2
            if inside(rgb_for(middle)):
                low = middle
            else:
                high = middle
        rgb = rgb_for(low)
    return Color(*(_clamp01(channel) for channel in rgb), _clamp01(alpha))


# ── CSS text ─────────────────────────────────────────────────────────────

CSS_NAMED_COLORS = {
    "aliceblue": "f0f8ff", "antiquewhite": "faebd7", "aqua": "00ffff", "aquamarine": "7fffd4",
    "azure": "f0ffff", "beige": "f5f5dc", "bisque": "ffe4c4", "black": "000000",
    "blanchedalmond": "ffebcd", "blue": "0000ff", "blueviolet": "8a2be2", "brown": "a52a2a",
    "burlywood": "deb887", "cadetblue": "5f9ea0", "chartreuse": "7fff00", "chocolate": "d2691e",
    "coral": "ff7f50", "cornflowerblue": "6495ed", "cornsilk": "fff8dc", "crimson": "dc143c",
    "cyan": "00ffff", "darkblue": "00008b", "darkcyan": "008b8b", "darkgoldenrod": "b8860b",
    "darkgray": "a9a9a9", "darkgreen": "006400", "darkgrey": "a9a9a9", "darkkhaki": "bdb76b",
    "darkmagenta": "8b008b", "darkolivegreen": "556b2f", "darkorange": "ff8c00",
    "darkorchid": "9932cc", "darkred": "8b0000", "darksalmon": "e9967a",
    "darkseagreen": "8fbc8f", "darkslateblue": "483d8b", "darkslategray": "2f4f4f",
    "darkslategrey": "2f4f4f", "darkturquoise": "00ced1", "darkviolet": "9400d3",
    "deeppink": "ff1493", "deepskyblue": "00bfff", "dimgray": "696969", "dimgrey": "696969",
    "dodgerblue": "1e90ff", "firebrick": "b22222", "floralwhite": "fffaf0",
    "forestgreen": "228b22", "fuchsia": "ff00ff", "gainsboro": "dcdcdc", "ghostwhite": "f8f8ff",
    "gold": "ffd700", "goldenrod": "daa520", "gray": "808080", "green": "008000",
    "greenyellow": "adff2f", "grey": "808080", "honeydew": "f0fff0", "hotpink": "ff69b4",
    "indianred": "cd5c5c", "indigo": "4b0082", "ivory": "fffff0", "khaki": "f0e68c",
    "lavender": "e6e6fa", "lavenderblush": "fff0f5", "lawngreen": "7cfc00",
    "lemonchiffon": "fffacd", "lightblue": "add8e6", "lightcoral": "f08080",
    "lightcyan": "e0ffff", "lightgoldenrodyellow": "fafad2", "lightgray": "d3d3d3",
    "lightgreen": "90ee90", "lightgrey": "d3d3d3", "lightpink": "ffb6c1",
    "lightsalmon": "ffa07a", "lightseagreen": "20b2aa", "lightskyblue": "87cefa",
    "lightslategray": "778899", "lightslategrey": "778899", "lightsteelblue": "b0c4de",
    "lightyellow": "ffffe0", "lime": "00ff00", "limegreen": "32cd32", "linen": "faf0e6",
    "magenta": "ff00ff", "maroon": "800000", "mediumaquamarine": "66cdaa",
    "mediumblue": "0000cd", "mediumorchid": "ba55d3", "mediumpurple": "9370db",
    "mediumseagreen": "3cb371", "mediumslateblue": "7b68ee", "mediumspringgreen": "00fa9a",
    "mediumturquoise": "48d1cc", "mediumvioletred": "c71585", "midnightblue": "191970",
    "mintcream": "f5fffa", "mistyrose": "ffe4e1", "moccasin": "ffe4b5", "navajowhite": "ffdead",
    "navy": "000080", "oldlace": "fdf5e6", "olive": "808000", "olivedrab": "6b8e23",
    "orange": "ffa500", "orangered": "ff4500", "orchid": "da70d6", "palegoldenrod": "eee8aa",
    "palegreen": "98fb98", "paleturquoise": "afeeee", "palevioletred": "db7093",
    "papayawhip": "ffefd5", "peachpuff": "ffdab9", "peru": "cd853f", "pink": "ffc0cb",
    "plum": "dda0dd", "powderblue": "b0e0e6", "purple": "800080", "rebeccapurple": "663399",
    "red": "ff0000", "rosybrown": "bc8f8f", "royalblue": "4169e1", "saddlebrown": "8b4513",
    "salmon": "fa8072", "sandybrown": "f4a460", "seagreen": "2e8b57", "seashell": "fff5ee",
    "sienna": "a0522d", "silver": "c0c0c0", "skyblue": "87ceeb", "slateblue": "6a5acd",
    "slategray": "708090", "slategrey": "708090", "snow": "fffafa", "springgreen": "00ff7f",
    "steelblue": "4682b4", "tan": "d2b48c", "teal": "008080", "thistle": "d8bfd8",
    "tomato": "ff6347", "turquoise": "40e0d0", "violet": "ee82ee", "wheat": "f5deb3",
    "white": "ffffff", "whitesmoke": "f5f5f5", "yellow": "ffff00", "yellowgreen": "9acd32",
}

_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?"


def _split_arguments(body: str) -> tuple[list[str], str | None]:
    """`10 20 30 / 50%`, `10, 20, 30, .5` and mixtures, into parts and alpha."""
    body = body.strip()
    alpha = None
    if "/" in body:
        body, alpha = body.split("/", 1)
        alpha = alpha.strip()
    parts = [part for part in re.split(r"[\s,]+", body.strip()) if part]
    if alpha is None and len(parts) == 4:
        alpha = parts.pop()
    return parts, alpha


def _number(text: str, *, percent_of: float = 1.0) -> float:
    text = text.strip().lower()
    if text.endswith("%"):
        return float(text[:-1]) / 100 * percent_of
    return float(text)


def _alpha(text: str | None) -> float:
    if text is None:
        return 1.0
    text = text.strip().lower()
    if text.endswith("%"):
        return _clamp01(float(text[:-1]) / 100)
    return _clamp01(float(text))


def _hue(text: str) -> float:
    text = text.strip().lower()
    for unit, factor in (("deg", 1.0), ("grad", 0.9), ("rad", 180 / math.pi), ("turn", 360.0)):
        if text.endswith(unit):
            return float(text[: -len(unit)]) * factor
    return float(text)


def parse_color(text: str) -> Color | None:
    """Read anything a person might paste into a color field.

    Hex with or without `#` (3, 4, 6 or 8 digits), `rgb()`/`rgba()` in comma
    or space syntax with percentages, `hsl()`/`hsla()`, `hsb()`/`hsv()`,
    `oklch()`, `oklab()`, CSS named colors and `transparent`. Returns None for
    anything else, so a field can say the input is invalid.
    """
    if not text:
        return None
    value = text.strip().rstrip(";").strip()
    lowered = value.lower()
    if lowered == "transparent":
        return Color(0, 0, 0, 0)
    if lowered in CSS_NAMED_COLORS:
        return parse_hex(CSS_NAMED_COLORS[lowered])
    direct = parse_hex(value)
    if direct is not None:
        return direct
    match = re.fullmatch(r"([a-z]+)\s*\((.*)\)", lowered, flags=re.S)
    if not match:
        return None
    function, body = match.group(1), match.group(2)
    try:
        parts, alpha_text = _split_arguments(body)
        if len(parts) != 3:
            return None
        alpha = _alpha(alpha_text)
        if function in {"rgb", "rgba"}:
            channels = [_clamp01(_number(part, percent_of=255) / 255) for part in parts]
            return Color(*channels, alpha)
        if function in {"hsl", "hsla"}:
            return hsl_to_rgb(_hue(parts[0]), _number(parts[1], percent_of=100) / 100,
                              _number(parts[2], percent_of=100) / 100, alpha)
        if function in {"hsb", "hsv", "hsba", "hsva"}:
            return hsv_to_rgb(_hue(parts[0]), _number(parts[1], percent_of=100) / 100,
                              _number(parts[2], percent_of=100) / 100, alpha)
        if function == "oklch":
            lightness = _number(parts[0], percent_of=1.0)
            if not parts[0].endswith("%") and lightness > 1.0:
                lightness /= 100
            chroma = _number(parts[1], percent_of=0.4)
            return oklch_to_srgb(lightness, chroma, _hue(parts[2]), alpha)
        if function == "oklab":
            lightness = _number(parts[0], percent_of=1.0)
            a = _number(parts[1], percent_of=0.4)
            b = _number(parts[2], percent_of=0.4)
            chroma = math.hypot(a, b)
            hue = math.degrees(math.atan2(b, a))
            return oklch_to_srgb(lightness, chroma, hue, alpha)
    except (ValueError, ZeroDivisionError):
        return None
    return None


def css_string(color: Color) -> str:
    r, g, b = color.bytes()
    percent = round(color.a * 100)
    if percent >= 100:
        return f"rgb({r} {g} {b})"
    return f"rgb({r} {g} {b} / {percent}%)"


def format_number(value: float, digits: int = 2) -> str:
    """Rounded and trimmed: 320, 48.5, 12.25."""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def color_of(value: Any, fallback: Color | None = None) -> Color | None:
    """A model value (hex string, css string, or color dict) as a Color."""
    if value is None:
        return fallback
    if isinstance(value, Color):
        return value
    if isinstance(value, dict):
        return Color.from_dict(value)
    parsed = parse_color(str(value))
    return parsed if parsed is not None else fallback


def model_string(color: Color) -> str:
    """The string the schema 1 model stores for a solid paint."""
    return to_hex(color)


# ── paints ───────────────────────────────────────────────────────────────

PAINT_TYPES = ("solid", "linear", "radial")


def solid(color: Color, *, opacity: float = 1.0) -> dict[str, Any]:
    return {"type": "solid", "color": color.to_dict(), "opacity": opacity, "visible": True}


def gradient(kind: str, stops: Iterable[tuple[float, Color]],
             handles: list[list[float]] | None = None) -> dict[str, Any]:
    if kind not in {"linear", "radial"}:
        raise ValueError(f"unknown gradient type: {kind}")
    default_handles = ([[0.5, 0.0], [0.5, 1.0], [1.0, 0.0]] if kind == "linear"
                       else [[0.5, 0.5], [0.5, 1.0], [1.0, 0.5]])
    return {
        "type": kind,
        "stops": [{"position": float(position), "color": color.to_dict()}
                  for position, color in stops],
        "handles": copy.deepcopy(handles or default_handles),
        "opacity": 1.0,
        "visible": True,
    }


def convert_paint(paint: dict[str, Any], kind: str) -> dict[str, Any]:
    """Change a paint's type the way Figma does.

    Solid to gradient gives the color at 100% fading to the same color at 0%;
    gradient to solid keeps the first stop; gradient to gradient keeps stops.
    """
    current = paint.get("type", "solid")
    if kind == current:
        return copy.deepcopy(paint)
    if kind == "solid":
        stops = paint.get("stops") or []
        color = Color.from_dict(stops[0]["color"]) if stops else Color(0.851, 0.851, 0.851)
        return solid(color, opacity=paint.get("opacity", 1.0))
    if current == "solid":
        color = Color.from_dict(paint.get("color", {}))
        result = gradient(kind, [(0.0, color.with_alpha(1.0)), (1.0, color.with_alpha(0.0))])
        result["opacity"] = paint.get("opacity", 1.0)
        return result
    result = gradient(kind, [(stop["position"], Color.from_dict(stop["color"]))
                             for stop in paint.get("stops", [])])
    result["opacity"] = paint.get("opacity", 1.0)
    return result


def sorted_stops(paint: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(paint.get("stops", []), key=lambda stop: float(stop["position"]))


def interpolate(stops: list[dict[str, Any]], position: float) -> Color:
    """The color a gradient has at a position, interpolated in sRGB."""
    ordered = sorted(stops, key=lambda stop: float(stop["position"]))
    if not ordered:
        return Color(0, 0, 0, 0)
    if position <= ordered[0]["position"]:
        return Color.from_dict(ordered[0]["color"])
    for before, after in zip(ordered, ordered[1:]):
        if before["position"] <= position <= after["position"]:
            span = after["position"] - before["position"]
            t = 0.0 if span <= 1e-9 else (position - before["position"]) / span
            a, b = Color.from_dict(before["color"]), Color.from_dict(after["color"])
            return Color(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t,
                         a.b + (b.b - a.b) * t, a.a + (b.a - a.a) * t)
    return Color.from_dict(ordered[-1]["color"])


def paint_color(paint: dict[str, Any] | None) -> Color | None:
    """A representative color: the solid color, or a gradient's first stop."""
    if not paint:
        return None
    if paint.get("type", "solid") == "solid":
        return Color.from_dict(paint.get("color", {}))
    stops = sorted_stops(paint)
    return Color.from_dict(stops[0]["color"]) if stops else None


def same_paint(first: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    return json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ── paints on schema 1 objects ───────────────────────────────────────────
#
# The schema 1 model stores a solid as a hex string in `fill` or `stroke` and
# a gradient in `fill_gradient` or `stroke_gradient`. These two functions are
# the only place that knows, so the schema 2 paint stacks can replace them.

def object_paint(item: Any, target: str) -> dict[str, Any] | None:
    gradient_value = getattr(item, f"{target}_gradient", None)
    if gradient_value:
        return copy.deepcopy(gradient_value)
    value = getattr(item, target, None)
    color = color_of(value)
    return solid(color) if color is not None else None


def assign_paint(item: Any, target: str, paint: dict[str, Any] | None) -> None:
    """Write a paint onto an object as its bottom paint (no history; callers wrap that).

    With several paints the bottom one is replaced and the rest stay; `None`
    removes the bottom paint.
    """
    stack = paint_stack(item, target)
    if paint is None:
        stack = stack[1:]
    elif stack:
        stack[0] = {**_with_defaults(paint), "blend": stack[0].get("blend", "normal")}
    else:
        stack = [_with_defaults(paint)]
    set_paint_stack(item, target, stack)


def _unit_point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def normalized_effect(effect: dict[str, Any]) -> dict[str, Any] | None:
    """Read both the inspector's and schema 2's effect spellings."""
    kind = str(effect.get("type") or effect.get("kind") or "").replace("-", "_")
    if not effect.get("visible", effect.get("enabled", True)):
        return None
    color = color_of(effect.get("color") or effect.get("colour"), Color(0, 0, 0, 0.25))
    return {"kind": kind, "x": float(effect.get("x", 0) or 0), "y": float(effect.get("y", 0) or 0),
            "blur": max(0.0, float(effect.get("blur", effect.get("radius", 0)) or 0)),
            "spread": float(effect.get("spread", 0) or 0), "color": color,
            # A progressive blur grows from `blur` to `end_blur` along the line
            # from `from` to `to` (both in the layer's unit box, as Figma
            # stores them). Without that line it is an even blur.
            "progressive": bool(effect.get("progressive")) and _unit_point(effect.get("from")) is not None,
            "end_blur": max(0.0, float(effect.get("end_radius", effect.get("end_blur", 0)) or 0)),
            "from": _unit_point(effect.get("from")), "to": _unit_point(effect.get("to")),
            # Figma's "Show behind transparent areas": a shadow shows through a
            # see-through layer unless it is switched off, and then the layer's
            # own shape is cut out of it.
            "behind": effect.get("behind", True) is not False}


def group_effects_of(item: Any) -> list[dict[str, Any]]:
    """A group's own drop shadows and blur: the effects its layers cast together.

    A group has no shape, so these are drawn on everything in it at once, as
    Figma draws them. An inner shadow is not among them: it needs a shape to
    fall inside, so each layer in the group casts its own (see
    `model.migrate_group_paint`).
    """
    if getattr(item, "kind", None) not in {"group", "spread", "mask"}:
        return []
    return [value for value in (normalized_effect(effect) for effect in getattr(item, "effects", None) or []
                                if isinstance(effect, dict))
            if value and value["kind"] in {"drop_shadow", "layer_blur"}]


# ── paint stacks (schema 2) ──────────────────────────────────────────────
#
# A layer paints a stack of fills and a stack of strokes, bottom to top, kept
# in `fills` and `strokes`. `fill`/`fill_gradient` (and the stroke pair) mirror
# the bottom paint for code that only knows one paint. Renderers and exporters
# read through `paint_stack`; editors write through `set_paint_stack`.

BLEND_MODES = ("normal", "darken", "multiply", "color-burn", "lighten", "screen", "color-dodge", "overlay",
               "soft-light", "hard-light", "difference", "exclusion", "hue", "saturation", "color",
               "luminosity")


def _stack_extra_key(target: str) -> str:
    return "extra_fills" if target == "fill" else "extra_strokes"


def _with_defaults(paint: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(paint)
    value.setdefault("type", "solid")
    value.setdefault("opacity", 1.0)
    value.setdefault("visible", True)
    value.setdefault("blend", "normal")
    return value


def paint_stack(item: Any, target: str) -> list[dict[str, Any]]:
    """Every fill (or stroke) of a layer, bottom to top, hidden ones included.

    The stored stack is the truth, except where a writer that only knows the
    scalar fields changed them since: then the bottom paint follows them.
    """
    stored = getattr(item, f"{target}s", None)
    if isinstance(stored, list) and stored:
        stack = [_with_defaults(value) for value in stored if isinstance(value, dict)]
        if not stack:
            return []
        base = base_paint(item, target)
        wanted = mirror_fields(base) if base is not None else None
        if mirror_fields(stack[0]) != wanted:
            if base is None:
                if stack[0].get("visible", True) is not False:
                    stack.pop(0)
            else:
                base["blend"] = stack[0].get("blend", "normal")
                stack[0] = base
        return stack
    stack = []
    base = base_paint(item, target)
    metadata = getattr(item, "metadata", None) or {}
    if base is not None:
        stack.append(base)
    else:
        hidden = color_of(metadata.get(f"hidden_{target}"))
        if hidden is not None:
            stack.append({**solid(hidden), "visible": False, "blend": "normal"})
    for value in metadata.get(_stack_extra_key(target)) or []:
        if isinstance(value, dict):
            stack.append(_with_defaults(value))
    return stack


def set_paint_stack(item: Any, target: str, paints: list[dict[str, Any]]) -> None:
    """Write a whole stack, keeping the scalar mirror of its bottom paint."""
    stack = [_with_defaults(paint) for paint in paints]
    setattr(item, f"{target}s", stack)
    mirror = mirror_fields(stack[0]) if stack else None
    setattr(item, target, mirror["fill"] if mirror else None)
    setattr(item, f"{target}_gradient", copy.deepcopy(mirror["gradient"]) if mirror else None)
    metadata = getattr(item, "metadata", None) or {}
    legacy = (_stack_extra_key(target), f"hidden_{target}")
    if any(key in metadata for key in legacy):
        item.metadata = {key: value for key, value in metadata.items() if key not in legacy}
    if target == "stroke" and any(paint.get("visible", True) is not False for paint in stack) \
            and not getattr(item, "stroke_width", 0):
        item.stroke_width = 1.0


PAINT_KIND_NAMES = {"linear": "Linear", "radial": "Radial", "angular": "Angular", "diamond": "Diamond",
                    "image": "Image", "video": "Video", "pattern": "Pattern"}


def stack_paint(item: Any, target: str, index: int) -> dict[str, Any] | None:
    """One paint of a layer's stack by its position (0 is the bottom)."""
    stack = paint_stack(item, target)
    return stack[index] if 0 <= index < len(stack) else None


def replace_stack_paint(item: Any, target: str, index: int, paint: dict[str, Any] | None) -> None:
    """Replace one paint in the stack, keeping its visibility and blend mode
    (those change through `update_stack_paint`); `None` removes it."""
    stack = paint_stack(item, target)
    if not 0 <= index < len(stack):
        if paint is not None:
            stack.append(_with_defaults(paint))
            set_paint_stack(item, target, stack)
        return
    if paint is None:
        stack.pop(index)
    else:
        previous = stack[index]
        merged = copy.deepcopy(paint)
        for key in ("visible", "blend"):
            merged[key] = previous.get(key, True if key == "visible" else "normal")
        stack[index] = _with_defaults(merged)
    set_paint_stack(item, target, stack)


def update_stack_paint(item: Any, target: str, index: int, **values: Any) -> None:
    """Change fields of one paint (visible, opacity, blend) in place."""
    stack = paint_stack(item, target)
    if 0 <= index < len(stack):
        stack[index].update(values)
        set_paint_stack(item, target, stack)


def move_stack_paint(item: Any, target: str, source: int, destination: int) -> None:
    stack = paint_stack(item, target)
    if not (0 <= source < len(stack)) or source == destination:
        return
    paint = stack.pop(source)
    stack.insert(max(0, min(len(stack), destination)), paint)
    set_paint_stack(item, target, stack)


def paint_label(paint: dict[str, Any] | None) -> str:
    """What a paint row reads: `RRGGBB` for a solid, the kind for the rest."""
    if not paint:
        return ""
    kind = paint.get("type", "solid")
    if kind == "solid":
        return to_hex(Color.from_dict(paint.get("color", {})).with_alpha(1.0), alpha=False, prefix=False).upper()
    return PAINT_KIND_NAMES.get(kind, str(kind).title())


def paint_opacity(paint: dict[str, Any] | None) -> float:
    """The opacity a row shows: the paint's, times a solid color's own alpha."""
    if not paint:
        return 1.0
    opacity = float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0)
    if paint.get("type", "solid") == "solid":
        opacity *= Color.from_dict(paint.get("color", {})).a
    return max(0.0, min(1.0, opacity))


def with_paint_opacity(paint: dict[str, Any], opacity: float) -> dict[str, Any]:
    """A paint at `opacity` (a solid's color alpha folds into it)."""
    value = copy.deepcopy(paint)
    if value.get("type", "solid") == "solid":
        color = Color.from_dict(value.get("color", {}))
        value["color"] = color.with_alpha(1.0).to_dict()
    value["opacity"] = max(0.0, min(1.0, float(opacity)))
    return value


def base_paint(item: Any, target: str) -> dict[str, Any] | None:
    """The paint the scalar `fill`/`stroke` fields describe."""
    gradient_value = getattr(item, f"{target}_gradient", None)
    if gradient_value:
        return _with_defaults(gradient_value)
    color = color_of(getattr(item, target, None))
    return _with_defaults(solid(color)) if color is not None else None


def mirror_fields(paint: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the scalar fields hold for a bottom paint (None when hidden or absent)."""
    if not paint or paint.get("visible", True) is False:
        return None
    kind = paint.get("type", "solid")
    if kind == "solid":
        color = Color.from_dict(paint.get("color", {}))
        alpha = color.a * float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0)
        return {"fill": to_hex(color.with_alpha(alpha)), "gradient": None}
    first = paint_color(paint) or BLACK
    gradient_value = {key: value for key, value in paint.items() if key not in {"blend", "visible"}}
    gradient_value.setdefault("opacity", 1.0)
    return {"fill": to_hex(first), "gradient": gradient_value}


def visible_paints(item: Any, target: str) -> list[dict[str, Any]]:
    return [paint for paint in paint_stack(item, target) if paint.get("visible", True) is not False
            and float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0) > 0]


def has_paint(item: Any, target: str) -> bool:
    return bool(visible_paints(item, target))


# ── current style ────────────────────────────────────────────────────────

DEFAULT_SHAPE_FILL = "#d9d9d9"
DEFAULT_LINE_STROKE = "#000000"
DEFAULT_TEXT_FILL = "#000000"
DEFAULT_FRAME_FILL = "#ffffff"

SHAPE_KINDS = {"rectangle", "ellipse", "polygon", "star"}
LINE_KINDS = {"line", "arrow", "path"}
TEXT_KINDS = {"text"}
FRAME_KINDS = {"frame", "page", "section", "component"}


def default_current_style() -> dict[str, Any]:
    """What a new document draws with (spec 6.2)."""
    return {
        "front": "fill",
        "shape": {"fill": DEFAULT_SHAPE_FILL, "fill_gradient": None,
                  "stroke": None, "stroke_gradient": None, "stroke_width": 1.0,
                  "stroke_align": "inside", "radius": 0.0, "effects": []},
        "line": {"stroke": DEFAULT_LINE_STROKE, "stroke_gradient": None,
                 "stroke_width": 1.0, "stroke_align": "center", "stroke_cap": "butt"},
        "text": {"fill": DEFAULT_TEXT_FILL, "font_family": "Figtree", "font_size": 16,
                 "font_weight": 400},
        "frame": {"fill": DEFAULT_FRAME_FILL},
    }


def style_category(kind: str) -> str | None:
    if kind in SHAPE_KINDS:
        return "shape"
    if kind in LINE_KINDS:
        return "line"
    if kind in TEXT_KINDS:
        return "text"
    if kind in FRAME_KINDS:
        return "frame"
    return None


def normalized_style(style: dict[str, Any] | None) -> dict[str, Any]:
    """Fill in anything an older document's style is missing."""
    result = default_current_style()
    if isinstance(style, dict):
        for key, value in style.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key].update(copy.deepcopy(value))
            else:
                result[key] = copy.deepcopy(value)
    if result.get("front") not in {"fill", "stroke"}:
        result["front"] = "fill"
    return result


def style_for_new(style: dict[str, Any], kind: str) -> dict[str, Any]:
    """The model fields a newly drawn object of this kind starts with."""
    style = normalized_style(style)
    category = style_category(kind)
    if category == "shape":
        shape = style["shape"]
        values = {"fill": shape.get("fill"), "fill_gradient": copy.deepcopy(shape.get("fill_gradient")),
                  "stroke": shape.get("stroke"),
                  "stroke_gradient": copy.deepcopy(shape.get("stroke_gradient")),
                  "stroke_width": float(shape.get("stroke_width") or 0.0) if shape.get("stroke") else 0.0,
                  "stroke_align": shape.get("stroke_align", "inside"),
                  "effects": copy.deepcopy(shape.get("effects") or [])}
        if kind == "rectangle":
            values["radius"] = float(shape.get("radius") or 0.0)
        return values
    if category == "line":
        line = style["line"]
        stroke = line.get("stroke")
        if not stroke:
            # A line is never invisible: without a line stroke it borrows the
            # shape fill as a 1px stroke.
            stroke = style["shape"].get("fill") or DEFAULT_LINE_STROKE
            width = 1.0
        else:
            width = float(line.get("stroke_width") or 1.0)
        return {"fill": None, "fill_gradient": None, "stroke": stroke,
                "stroke_gradient": copy.deepcopy(line.get("stroke_gradient")) if line.get("stroke") else None,
                "stroke_width": width, "stroke_align": "center",
                "stroke_cap": line.get("stroke_cap", "butt")}
    if category == "text":
        return {"fill": style["text"].get("fill") or DEFAULT_TEXT_FILL}
    if category == "frame":
        return {"fill": style["frame"].get("fill") or DEFAULT_FRAME_FILL}
    return {}


STYLE_KEYS = {
    "shape": {"fill", "fill_gradient", "stroke", "stroke_gradient", "stroke_width",
              "stroke_align", "effects"},
    "line": {"stroke", "stroke_gradient", "stroke_width", "stroke_cap"},
    "text": {"fill"},
    "frame": {"fill"},
}


def remember_edit(style: dict[str, Any], kind: str, key: str, value: Any) -> dict[str, Any]:
    """Editing a selection teaches the current style (spec 6.2, rule 1)."""
    style = normalized_style(style)
    category = style_category(kind)
    if category is None or key not in STYLE_KEYS[category]:
        return style
    if category == "line" and key == "stroke_width" and not value:
        return style
    style[category][key] = copy.deepcopy(value)
    if key in {"fill", "stroke"} and value is None:
        style[category][f"{key}_gradient"] = None
    return style


# ── swatch sources ───────────────────────────────────────────────────────

def document_colors(objects: Iterable[Any], limit: int = 30) -> list[str]:
    """Distinct solid colors on visible layers, most used first."""
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for item in objects:
        if not getattr(item, "visible", True):
            continue
        for key in ("fill", "stroke"):
            if key == "stroke" and not getattr(item, "stroke_width", 0):
                continue
            color = color_of(getattr(item, key, None))
            if color is None:
                continue
            text = to_hex(color)
            counts[text] = counts.get(text, 0) + 1
            order.setdefault(text, len(order))
    ranked = sorted(counts, key=lambda text: (-counts[text], order[text]))
    return ranked[:limit]


class RecentColors:
    """The last colors committed through any picker, per user."""

    LIMIT = 16

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            path = base / "luma-canvas" / "recent-colors.json"
        self.path = path

    def load(self) -> list[str]:
        try:
            values = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return [value for value in values if isinstance(value, str) and parse_color(value)][: self.LIMIT]

    def add(self, color: Color) -> list[str]:
        text = to_hex(color)
        values = [value for value in self.load() if value.lower() != text.lower()]
        values.insert(0, text)
        values = values[: self.LIMIT]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values))
            temporary.replace(self.path)
        except OSError:
            pass
        return values
