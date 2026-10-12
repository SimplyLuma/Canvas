# SPDX-License-Identifier: MPL-2.0
"""What a blend mode must produce, worked out from the compositing spec.

The reference is arithmetic, not a stored image: every renderer (Cairo, GL,
Vulkan), every export and the SVG writer are checked against the same numbers,
so a reference render can never drift from what the spec says.

`CASES` are the scenes the pixel tests draw: a backdrop rectangle with a second
rectangle over it, in each blend mode, opaque and half transparent.
"""

from __future__ import annotations

from dataclasses import dataclass

from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.paint import BLEND_MODES

# ── the separable blend functions (W3C Compositing and Blending 1, §9) ───


def _multiply(b: float, s: float) -> float:
    return b * s


def _screen(b: float, s: float) -> float:
    return b + s - b * s


def _hard_light(b: float, s: float) -> float:
    return _multiply(b, 2 * s) if s <= 0.5 else _screen(b, 2 * s - 1)


def _overlay(b: float, s: float) -> float:
    return _hard_light(s, b)


def _color_dodge(b: float, s: float) -> float:
    if b <= 0:
        return 0.0
    if s >= 1:
        return 1.0
    return min(1.0, b / (1 - s))


def _color_burn(b: float, s: float) -> float:
    if b >= 1:
        return 1.0
    if s <= 0:
        return 0.0
    return 1 - min(1.0, (1 - b) / s)


def _soft_light(b: float, s: float) -> float:
    if s <= 0.5:
        return b - (1 - 2 * s) * b * (1 - b)
    d = ((16 * b - 12) * b + 4) * b if b <= 0.25 else b ** 0.5
    return b + (2 * s - 1) * (d - b)


SEPARABLE = {
    "normal": lambda b, s: s,
    "multiply": _multiply,
    "screen": _screen,
    "overlay": _overlay,
    "darken": min,
    "lighten": max,
    "color-dodge": _color_dodge,
    "color-burn": _color_burn,
    "hard-light": _hard_light,
    "soft-light": _soft_light,
    "difference": lambda b, s: abs(b - s),
    "exclusion": lambda b, s: b + s - 2 * b * s,
}

# ── the non-separable ones (§10): hue, saturation, color, luminosity ─────

RGB = tuple[float, float, float]


def _lum(c: RGB) -> float:
    return 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]


def _clip_color(c: RGB) -> RGB:
    lum = _lum(c)
    low, high = min(c), max(c)
    values = list(c)
    if low < 0:
        values = [lum + (value - lum) * lum / (lum - low) if lum != low else lum for value in values]
    if high > 1:
        values = [lum + (value - lum) * (1 - lum) / (high - lum) if high != lum else lum for value in values]
    return (values[0], values[1], values[2])


def _set_lum(c: RGB, lum: float) -> RGB:
    d = lum - _lum(c)
    return _clip_color((c[0] + d, c[1] + d, c[2] + d))


def _sat(c: RGB) -> float:
    return max(c) - min(c)


def _set_sat(c: RGB, sat: float) -> RGB:
    values = list(c)
    order = sorted(range(3), key=lambda index: values[index])
    low, mid, high = order
    if values[high] > values[low]:
        values[mid] = (values[mid] - values[low]) * sat / (values[high] - values[low])
        values[high] = sat
    else:
        values[mid] = values[high] = 0.0
    values[low] = 0.0
    return (values[0], values[1], values[2])


NON_SEPARABLE = {
    "hue": lambda b, s: _set_lum(_set_sat(s, _sat(b)), _lum(b)),
    "saturation": lambda b, s: _set_lum(_set_sat(b, _sat(s)), _lum(b)),
    "color": lambda b, s: _set_lum(s, _lum(b)),
    "luminosity": lambda b, s: _set_lum(b, _lum(s)),
}


def blend(mode: str, backdrop: RGB, source: RGB) -> RGB:
    """The blended color of two opaque colors."""
    if mode in NON_SEPARABLE:
        return NON_SEPARABLE[mode](backdrop, source)
    function = SEPARABLE.get(mode, SEPARABLE["normal"])
    return tuple(min(1.0, max(0.0, function(backdrop[index], source[index]))) for index in range(3))


def composite(mode: str, backdrop: RGB, source: RGB, alpha: float) -> RGB:
    """`source` at `alpha` blended onto an opaque `backdrop`.

    With an opaque backdrop the general formula collapses to a mix between the
    backdrop and the blended color (§11.1 with ab = 1).
    """
    blended = blend(mode, backdrop, source)
    return tuple(backdrop[index] * (1 - alpha) + blended[index] * alpha for index in range(3))


def as_bytes(color: RGB) -> tuple[int, int, int]:
    return tuple(int(round(max(0.0, min(1.0, value)) * 255)) for value in color)


def hex_color(color: RGB) -> str:
    return "#%02x%02x%02x" % as_bytes(color)


# ── the scenes ───────────────────────────────────────────────────────────

# Exact 8 bit colors, so the reference never disagrees with the hex a
# document stores: #d95c33 over #3894d4.
BACKDROP: RGB = (217 / 255, 92 / 255, 51 / 255)
SOURCE: RGB = (56 / 255, 148 / 255, 212 / 255)
MODES = tuple(mode for mode in BLEND_MODES if mode != "normal")


@dataclass(frozen=True)
class Case:
    """One blend scene: what to build and the color it must produce."""
    name: str
    mode: str
    alpha: float
    kind: str = "layer"       # layer, paint, group, isolated

    @property
    def expected(self) -> RGB:
        return composite(self.mode, BACKDROP, SOURCE, self.alpha)


CASES: tuple[Case, ...] = tuple(
    [Case(f"layer-{mode}", mode, 1.0) for mode in MODES]
    + [Case(f"layer-{mode}-half", mode, 0.5) for mode in ("multiply", "screen", "overlay", "difference", "color")]
    + [Case(f"paint-{mode}", mode, 1.0, "paint") for mode in MODES]
    + [Case(f"group-{mode}", mode, 1.0, "group") for mode in ("multiply", "screen", "difference", "luminosity")]
    + [Case(f"isolated-{mode}", mode, 1.0, "isolated") for mode in ("multiply", "difference")]
)

SIZE = 60.0


def layer(kind: str, name: str, x: float, y: float, width: float, height: float,
          parent: str | None = None, **fields) -> LayoutObject:
    return LayoutObject(new_id(kind), kind, name, parent, x, y, width, height, **fields)


def case_document(case: Case) -> tuple[Document, tuple[float, float]]:
    """A document for one case, and the point whose color the test reads.

    Every case draws the same thing: `SOURCE` over `BACKDROP` in the middle of
    an opaque page-sized rectangle, reached a different way.

    * layer     — two rectangles, the top one with the blend mode.
    * paint     — one rectangle whose second fill carries the blend mode.
    * group     — the top rectangle inside a plain (pass through) group, so it
                  must still blend with the rectangle under the group.
    * isolated  — a group with the blend mode of its own: its layers blend
                  among themselves first, then the group blends as one.
    """
    document = Document.empty("Blend")
    back = layer("rectangle", "Backdrop", 0, 0, SIZE, SIZE, fill=hex_color(BACKDROP))
    objects = [back]
    top_fill = hex_color(SOURCE)
    if case.kind == "paint":
        back.fills = [{"type": "solid", "color": _rgb_dict(BACKDROP), "opacity": 1.0, "visible": True,
                       "blend": "normal"},
                      {"type": "solid", "color": _rgb_dict(SOURCE), "opacity": case.alpha, "visible": True,
                       "blend": case.mode}]
    elif case.kind == "group":
        group = layer("group", "Group", 0, 0, SIZE, SIZE)
        top = layer("rectangle", "Top", 0, 0, SIZE, SIZE, group.id, fill=top_fill,
                    opacity=case.alpha, metadata={"blend": case.mode})
        objects += [group, top]
    elif case.kind == "isolated":
        group = layer("group", "Group", 0, 0, SIZE, SIZE, metadata={"blend": case.mode})
        # Inside the group a white rectangle is covered by the source color, so
        # the group's own content is exactly SOURCE and only the group blends.
        objects += [group,
                    layer("rectangle", "White", 0, 0, SIZE, SIZE, group.id, fill="#ffffff"),
                    layer("rectangle", "Top", 0, 0, SIZE, SIZE, group.id, fill=top_fill)]
    else:
        objects.append(layer("rectangle", "Top", 0, 0, SIZE, SIZE, fill=top_fill,
                             opacity=case.alpha, metadata={"blend": case.mode}))
    document.objects = objects
    return document, (SIZE / 2, SIZE / 2)


def _rgb_dict(color: RGB) -> dict[str, float]:
    return {"r": color[0], "g": color[1], "b": color[2], "a": 1.0}
