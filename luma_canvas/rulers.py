# SPDX-License-Identifier: MPL-2.0
"""Rulers, guides and auto-scrolling: the arithmetic, without GTK.

Rulers are 20px bands along the canvas's top and left. Their labels sit on
"nice" document values (1, 2 or 5 times a power of ten) at least 64 screen
pixels apart, with minor ticks between them no closer than 6 pixels. Every
tick lands on a device pixel's center, so the lines stay one device pixel
wide and crisp at any zoom and any fractional display scale.

Guides are `guide` layers on the page: `metadata["axis"]` is "x" for a
vertical guide at `x`, "y" for a horizontal one at `y`.

Auto-scroll: while a drag nears the canvas's edge, the view moves toward
it at a speed proportional to how deep the pointer is in the edge zone (and
faster still past the edge).
"""

from __future__ import annotations

import math

RULER_SIZE = 20.0
LABEL_SPACING = 64.0
MINOR_SPACING = 6.0
EDGE_ZONE = 32.0
MAX_SPEED = 1200.0        # view px per second at the edge
BEYOND_LIMIT = 120.0      # past the edge, speed keeps growing for this many px


def nice_step(zoom: float, spacing: float = LABEL_SPACING) -> float:
    """The smallest 1-2-5 step in document units whose ticks are `spacing` px apart."""
    target = spacing / max(zoom, 1e-9)
    exponent = math.floor(math.log10(target))
    for power in (exponent, exponent + 1):
        for base in (1, 2, 5):
            step = base * 10 ** power
            if step >= target - 1e-9:
                return float(step)
    return float(10 ** (exponent + 1))


def minor_divisions(step: float, zoom: float) -> int:
    """How many parts a labeled step splits into (10, 5, 2 or none)."""
    leading = round(step / 10 ** math.floor(math.log10(step)))
    for parts in ((10, 5, 2) if leading in {1, 5} else (4, 2)):
        if step / parts * zoom >= MINOR_SPACING:
            return parts
    return 1


def ticks(start: float, end: float, zoom: float, offset: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Major (document value, view position) and minor ticks visible between view `start` and `end`.

    `offset` is the view position of document 0 on this axis.
    """
    step = nice_step(zoom)
    parts = minor_divisions(step, zoom)
    first = math.floor((start - offset) / zoom / step) * step
    last = (end - offset) / zoom
    major: list[tuple[float, float]] = []
    minor: list[tuple[float, float]] = []
    value = first
    guard = 0
    while value <= last + step and guard < 10000:
        major.append((value, value * zoom + offset))
        for part in range(1, parts):
            sub = value + step * part / parts
            minor.append((sub, sub * zoom + offset))
        value = round((value + step) / step) * step
        guard += 1
    return ([tick for tick in major if start - 1 <= tick[1] <= end + 1],
            [tick for tick in minor if start - 1 <= tick[1] <= end + 1])


def crisp(position: float, scale: float) -> float:
    """A 1-device-pixel line's center nearest `position` (view px)."""
    return (math.floor(position * scale) + 0.5) / scale


def label(value: float, step: float) -> str:
    if step >= 1 or abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    digits = max(0, -math.floor(math.log10(step)))
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def autoscroll_velocity(x: float, y: float, width: float, height: float, *,
                        inset_left: float = 0.0, inset_top: float = 0.0) -> tuple[float, float]:
    """View px per second to pan the content by, for a pointer at (x, y).

    Positive values move the content right or down (the view shows what is
    to the left or above); the pointer near the right edge gives a negative x.
    """
    def axis(position: float, low: float, high: float) -> float:
        if position < low + EDGE_ZONE:
            depth = min(low + EDGE_ZONE - position, EDGE_ZONE + BEYOND_LIMIT)
            return MAX_SPEED * depth / EDGE_ZONE / 2
        if position > high - EDGE_ZONE:
            depth = min(position - (high - EDGE_ZONE), EDGE_ZONE + BEYOND_LIMIT)
            return -MAX_SPEED * depth / EDGE_ZONE / 2
        return 0.0
    return axis(x, inset_left, width), axis(y, inset_top, height)


def guide_axis(item) -> str:
    return "x" if (getattr(item, "metadata", None) or {}).get("axis") == "x" else "y"


def guide_position(item) -> float:
    return float(item.x if guide_axis(item) == "x" else item.y)
