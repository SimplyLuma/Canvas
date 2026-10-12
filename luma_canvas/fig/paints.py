# SPDX-License-Identifier: MPL-2.0
"""Figma paints and effects as Canvas paints and effects.

Gradient geometry is exact. Figma stores a gradient's `transform`, which maps
the layer's unit box to gradient space, where a linear gradient runs from
(0, 0.5) to (1, 0.5) and a radial or diamond gradient is centered at
(0.5, 0.5) with radius 0.5. Canvas stores handles in the unit box:

- linear: the start point, and an end point placed so the gradient's
  direction is perpendicular to its lines of equal color in layer pixels,
  which keeps a diagonal gradient on a non-square layer exact;
- radial: the center, the end of the second axis, the end of the first axis;
- angular, diamond and image paints keep the transform itself.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from .paths import Affine
from .report import ImportReport

BLEND_CSS = {
    "DARKEN": "darken", "MULTIPLY": "multiply", "LINEAR_BURN": "color-burn", "COLOR_BURN": "color-burn",
    "LIGHTEN": "lighten", "SCREEN": "screen", "LINEAR_DODGE": "color-dodge", "COLOR_DODGE": "color-dodge",
    "OVERLAY": "overlay", "SOFT_LIGHT": "soft-light", "HARD_LIGHT": "hard-light",
    "DIFFERENCE": "difference", "EXCLUSION": "exclusion", "HUE": "hue", "SATURATION": "saturation",
    "COLOR": "color", "LUMINOSITY": "luminosity",
}
# Figma's two linear modes have no exact equivalent in the compositing spec
# (or in any renderer Canvas draws with): the nearest mode is used and the
# report says which.
APPROXIMATE_BLENDS = {"LINEAR_BURN": "Linear burn", "LINEAR_DODGE": "Linear dodge"}
SCALE_MODES = {"FILL": "fill", "FIT": "fit", "STRETCH": "crop", "TILE": "tile"}


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def clamp01(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else value


def color_dict(color: Any, alpha_scale: float = 1.0) -> dict[str, float]:
    color = color if isinstance(color, dict) else {}
    return {"r": round(clamp01(number(color.get("r"))), 4), "g": round(clamp01(number(color.get("g"))), 4),
            "b": round(clamp01(number(color.get("b"))), 4),
            "a": round(clamp01(number(color.get("a"), 1.0) * alpha_scale), 4)}


def hex_color(color: Any, alpha_scale: float = 1.0) -> str:
    value = color_dict(color, alpha_scale)
    text = "#" + "".join(f"{int(round(value[key] * 255)):02x}" for key in ("r", "g", "b"))
    if value["a"] < 0.9995:
        text += f"{int(round(value['a'] * 255)):02x}"
    return text


def blend_name(value: Any) -> str | None:
    return BLEND_CSS.get(value) if isinstance(value, str) else None


def visible_paints(paints: Any) -> list[dict[str, Any]]:
    if not isinstance(paints, list):
        return []
    return [paint for paint in paints if isinstance(paint, dict) and paint.get("visible", True) is not False
            and number(paint.get("opacity"), 1.0) > 0]


def _stops(paint: dict[str, Any]) -> list[dict[str, Any]]:
    stops = []
    for stop in paint.get("stops") or []:
        if isinstance(stop, dict):
            stops.append({"position": round(clamp01(number(stop.get("position"))), 5),
                          "color": color_dict(stop.get("color"))})
    stops.sort(key=lambda stop: stop["position"])
    if not stops:
        stops = [{"position": 0.0, "color": {"r": 0, "g": 0, "b": 0, "a": 1}},
                 {"position": 1.0, "color": {"r": 0, "g": 0, "b": 0, "a": 0}}]
    return stops


def matrix_list(matrix: Affine) -> list[float]:
    return [round(matrix.a, 6), round(matrix.b, 6), round(matrix.c, 6), round(matrix.d, 6),
            round(matrix.e, 6), round(matrix.f, 6)]


def _handle(point: tuple[float, float]) -> list[float]:
    return [round(point[0], 5), round(point[1], 5)]


def gradient_handles(kind: str, transform: Affine, width: float, height: float) -> list[list[float]] | None:
    inverse = transform.inverse()
    if inverse is None:
        return None
    if kind == "radial":
        return [_handle(inverse.apply(0.5, 0.5)), _handle(inverse.apply(0.5, 1.0)), _handle(inverse.apply(1.0, 0.5))]
    start = inverse.apply(0.0, 0.5)
    fallback_end = inverse.apply(1.0, 0.5)
    width_handle = inverse.apply(0.0, 1.0)
    if width > 1e-6 and height > 1e-6:
        gx, gy = transform.a / width, transform.c / height
        length = gx * gx + gy * gy
        if length > 1e-18:
            sx, sy = start[0] * width, start[1] * height
            ex, ey = sx + gx / length, sy + gy / length
            return [_handle(start), _handle((ex / width, ey / height)), _handle(width_handle)]
    return [_handle(start), _handle(fallback_end), _handle(width_handle)]


class PaintConverter:
    """Converts paints, remembering images and what could not be carried."""

    def __init__(self, report: ImportReport, image_asset: Callable[..., str | None]) -> None:
        self.report = report
        self.image_asset = image_asset

    def blend_of(self, value: Any, layer: str) -> str:
        """A Figma blend mode as Canvas draws it, noting the two it approximates."""
        name = blend_name(value)
        if name is None:
            return "normal"
        if value in APPROXIMATE_BLENDS:
            self.report.note("blend_approximate", layer, extra=APPROXIMATE_BLENDS[value])
        return name

    def paint(self, paint: dict[str, Any], width: float, height: float, layer: str) -> dict[str, Any] | None:
        kind = paint.get("type", "SOLID")
        opacity = round(clamp01(number(paint.get("opacity"), 1.0)), 4)
        blend = self.blend_of(paint.get("blendMode"), layer)
        if kind == "SOLID":
            return {"type": "solid", "color": color_dict(paint.get("color")), "opacity": opacity, "visible": True,
                    "blend": blend}
        if kind in {"GRADIENT_LINEAR", "GRADIENT_RADIAL"}:
            transform = Affine.from_figma(paint.get("transform"))
            canvas_kind = "linear" if kind == "GRADIENT_LINEAR" else "radial"
            handles = gradient_handles(canvas_kind, transform, width, height)
            if handles is None:
                return None
            return {"type": canvas_kind, "stops": _stops(paint), "handles": handles, "opacity": opacity,
                    "visible": True, "blend": blend}
        if kind in {"GRADIENT_ANGULAR", "GRADIENT_DIAMOND"}:
            transform = Affine.from_figma(paint.get("transform"))
            if transform.inverse() is None:
                return None
            return {"type": "angular" if kind == "GRADIENT_ANGULAR" else "diamond", "stops": _stops(paint),
                    "transform": matrix_list(transform), "opacity": opacity, "visible": True, "blend": blend}
        if kind in {"IMAGE", "VIDEO"}:
            if kind == "VIDEO":
                self.report.note("video", layer)
            image = paint.get("image") if isinstance(paint.get("image"), dict) else None
            thumbnail = paint.get("imageThumbnail") if isinstance(paint.get("imageThumbnail"), dict) else None
            asset_id = self.image_asset(image or {}, layer, thumbnail)
            if paint.get("paintFilter") or paint.get("filterColorAdjust"):
                if _has_adjustments(paint.get("paintFilter")) or _has_adjustments(paint.get("filterColorAdjust")):
                    self.report.note("image_filters", layer)
            result = {"type": "image", "asset_id": asset_id,
                      "scale_mode": SCALE_MODES.get(paint.get("imageScaleMode"), "fill"),
                      "transform": matrix_list(Affine.from_figma(paint.get("transform"))),
                      "scale": round(number(paint.get("scale"), 1.0) or 1.0, 5),
                      "rotation": round(number(paint.get("rotation")), 3),
                      "image_width": int(number(paint.get("originalImageWidth"))),
                      "image_height": int(number(paint.get("originalImageHeight"))),
                      "opacity": opacity, "visible": True, "blend": blend}
            return result
        self.report.note("paints", layer, extra=str(kind))
        return None

    def stack(self, paints: Any, width: float, height: float, layer: str) -> list[dict[str, Any]]:
        """Every visible paint of one Figma stack, bottom to top, as Canvas paints."""
        return [value for value in (self.paint(paint, width, height, layer)
                                    for paint in visible_paints(paints)) if value]

    def fills(self, props: dict[str, Any], width: float, height: float, layer: str) -> dict[str, Any]:
        """Model fields for a layer's fills: the whole `stack`, plus `fill` and
        `fill_gradient` mirroring its bottom paint for code that reads one paint."""
        converted = self.stack(props.get("fillPaints"), width, height, layer)
        return _stack_fields("fill", converted)

    def stroke(self, props: dict[str, Any], width: float, height: float, layer: str) -> dict[str, Any]:
        """The same for a layer's strokes: Figma stacks stroke paints as it does fills."""
        converted = self.stack(props.get("strokePaints"), width, height, layer)
        return _stack_fields("stroke", converted)


def _stack_fields(target: str, stack: list[dict[str, Any]]) -> dict[str, Any]:
    """A paint stack with the scalar mirror of its bottom paint.

    The mirror is the one Canvas itself writes (`paint.mirror_fields`), so an
    imported layer is indistinguishable from one edited in Canvas.
    """
    from ..paint import mirror_fields
    mirror = mirror_fields(stack[0]) if stack else None
    return {target: mirror["fill"] if mirror else None,
            f"{target}_gradient": mirror["gradient"] if mirror else None,
            "stack": stack}


def _offset_line(effect: dict[str, Any]) -> tuple[list[float], list[float]] | None:
    """Where a progressive blur starts and ends, in the layer's unit box."""
    def unit(value: Any) -> list[float] | None:
        if not isinstance(value, dict):
            return None
        x, y = number(value.get("x"), math.nan), number(value.get("y"), math.nan)
        return None if math.isnan(x) or math.isnan(y) else [round(x, 5), round(y, 5)]
    start, end = unit(effect.get("startOffset")), unit(effect.get("endOffset"))
    if start is None or end is None or start == end:
        return None
    return start, end


def _has_adjustments(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(abs(number(item)) > 1e-6 for item in value.values() if isinstance(item, (int, float)))


def effects(props: dict[str, Any], report: ImportReport, layer: str) -> list[dict[str, Any]]:
    result = []
    for effect in props.get("effects") or []:
        if not isinstance(effect, dict):
            continue
        kind = effect.get("type")
        visible = effect.get("visible", True) is not False
        if kind in {"DROP_SHADOW", "INNER_SHADOW"}:
            offset = effect.get("offset") if isinstance(effect.get("offset"), dict) else {}
            result.append({
                "type": "drop_shadow" if kind == "DROP_SHADOW" else "inner_shadow",
                "x": round(number(offset.get("x")), 4), "y": round(number(offset.get("y")), 4),
                "blur": round(max(0.0, number(effect.get("radius"))), 4),
                "spread": round(number(effect.get("spread")), 4),
                "color": hex_color(effect.get("color")), "visible": visible,
                "blend": blend_name(effect.get("blendMode")) or "normal",
                **({"behind": bool(effect.get("showShadowBehindNode"))} if kind == "DROP_SHADOW" else {}),
            })
            if blend_name(effect.get("blendMode")) and visible:
                report.note("effect_blend", layer)
        elif kind in {"FOREGROUND_BLUR", "BACKGROUND_BLUR"}:
            radius = round(max(0.0, number(effect.get("radius"))), 4)
            entry = {"type": "layer_blur" if kind == "FOREGROUND_BLUR" else "background_blur",
                     "radius": radius, "visible": visible}
            if effect.get("blurOpType") == "PROGRESSIVE":
                # The blur grows along the line Figma stores with the effect.
                start = round(max(0.0, number(effect.get("startRadius"))), 4)
                line = _offset_line(effect)
                entry.update(progressive=True, radius=min(start, radius), end_radius=max(start, radius))
                if line is None:
                    # No direction in the file: an even blur at the lighter end.
                    entry.update(progressive=False, end_radius=min(start, radius))
                    report.note("progressive_blur", layer)
                else:
                    entry.update({"from": line[0], "to": line[1]})
            result.append(entry)
        elif visible:
            report.note("effects", layer, extra=str(kind).replace("_", " ").capitalize())
    return result
