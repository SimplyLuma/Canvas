# SPDX-License-Identifier: MPL-2.0
"""Viewport arithmetic for the canvas input router.

The canvas maps document points to view points with one zoom and one offset:
`view = document * zoom + offset`. Every navigation gesture is a change to
those three numbers, and each is defined here without GTK so the rules can be
tested exactly:

* zooming keeps the document point under the pointer under the pointer;
* a mouse wheel notch pans 50 px, a touchpad pans by exactly its delta;
* Shift turns a vertical wheel into a horizontal one;
* Ctrl+wheel zooms 1.2x a notch, Ctrl+touchpad zooms continuously;
* zoom stays within 1% and 25600%, and input past a limit changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


ZOOM_MIN = 0.01
ZOOM_MAX = 256.0
WHEEL_STEP_PX = 50.0
WHEEL_ZOOM_FACTOR = 1.2
SMOOTH_ZOOM_RATE = 0.01
FIT_MARGIN = 48.0
ANIMATION_MS = 200
ZOOM_LADDER = (0.01, 0.02, 0.03, 0.06, 0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0,
               16.0, 32.0, 64.0, 128.0, 256.0)


def clamp_zoom(zoom: float) -> float:
    return max(ZOOM_MIN, min(ZOOM_MAX, zoom))


@dataclass(slots=True)
class View:
    zoom: float = 1.0
    x: float = 0.0          # view offset of document origin
    y: float = 0.0

    def to_view(self, dx: float, dy: float) -> tuple[float, float]:
        return dx * self.zoom + self.x, dy * self.zoom + self.y

    def to_document(self, vx: float, vy: float) -> tuple[float, float]:
        return (vx - self.x) / self.zoom, (vy - self.y) / self.zoom

    def zoomed_at(self, zoom: float, vx: float, vy: float) -> "View":
        """Change zoom keeping the document point under (vx, vy) fixed."""
        zoom = clamp_zoom(zoom)
        if abs(zoom - self.zoom) < 1e-12:
            return View(self.zoom, self.x, self.y)
        px, py = self.to_document(vx, vy)
        return View(zoom, vx - px * zoom, vy - py * zoom)

    def panned(self, dx: float, dy: float) -> "View":
        return View(self.zoom, self.x + dx, self.y + dy)


def ladder_step(zoom: float, direction: int) -> float:
    """The next zoom ladder value above (1) or below (-1) the current zoom."""
    epsilon = 1e-6
    if direction > 0:
        for value in ZOOM_LADDER:
            if value > zoom * (1 + epsilon):
                return value
        return ZOOM_MAX
    for value in reversed(ZOOM_LADDER):
        if value < zoom * (1 - epsilon):
            return value
    return ZOOM_MIN


@dataclass(frozen=True, slots=True)
class ScrollAction:
    kind: str                # "pan" | "zoom" | "none"
    dx: float = 0.0          # view px to move the content by
    dy: float = 0.0
    factor: float = 1.0      # zoom multiplier


def scroll_action(dx: float, dy: float, *, smooth: bool, ctrl: bool,
                  shift: bool) -> ScrollAction:
    """What a scroll event does.

    `smooth` is true for surface-unit (touchpad, high resolution) deltas,
    which are pixels; wheel-unit deltas are notches. Both axes use the sign
    GtkScrolledWindow uses (the view moves by +dx, +dy, so content moves the
    other way); natural scrolling is already applied to the deltas by the
    system. Shift swaps the axes, as GtkScrolledWindow does.
    """
    if ctrl:
        amount = dy if abs(dy) >= abs(dx) else 0.0
        if amount == 0:
            return ScrollAction("none")
        if smooth:
            return ScrollAction("zoom", factor=math.exp(-amount * SMOOTH_ZOOM_RATE))
        return ScrollAction("zoom", factor=WHEEL_ZOOM_FACTOR ** (-amount))
    scale = 1.0 if smooth else WHEEL_STEP_PX
    if shift:
        # As GtkScrolledWindow does: Shift swaps the axes, whatever the source.
        dx, dy = dy, dx
    pan_x, pan_y = dx * scale, dy * scale
    if pan_x == 0 and pan_y == 0:
        return ScrollAction("none")
    return ScrollAction("pan", -pan_x, -pan_y)


def fit_rect(left: float, top: float, right: float, bottom: float,
             view_width: float, view_height: float, *, margin: float = FIT_MARGIN,
             max_zoom: float | None = 1.0) -> View:
    """The view that shows a document rectangle centered with a margin."""
    width = max(right - left, 1e-6)
    height = max(bottom - top, 1e-6)
    available_w = max(1.0, view_width - margin * 2)
    available_h = max(1.0, view_height - margin * 2)
    zoom = min(available_w / width, available_h / height)
    if max_zoom is not None:
        zoom = min(zoom, max_zoom)
    zoom = clamp_zoom(zoom)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    return View(zoom, view_width / 2 - cx * zoom, view_height / 2 - cy * zoom)


def fit_rect_with_insets(left: float, top: float, right: float, bottom: float,
                         view_width: float, view_height: float, *,
                         inset_left: float, inset_right: float,
                         inset_top: float, inset_bottom: float,
                         max_zoom: float = 2.0) -> View:
    """Fit a rectangle in the visible space between floating controls."""
    width = max(right - left, 1e-6)
    height = max(bottom - top, 1e-6)
    available_w = max(1.0, view_width - inset_left - inset_right)
    available_h = max(1.0, view_height - inset_top - inset_bottom)
    zoom = clamp_zoom(min(max_zoom, available_w / width, available_h / height))
    x = inset_left + (available_w - width * zoom) / 2 - left * zoom
    y = inset_top + (available_h - height * zoom) / 2 - top * zoom
    return View(zoom, x, y)


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def interpolate_view(start: View, end: View, t: float, width: float, height: float) -> View:
    """A frame of an animated zoom: log-zoom and the view center both glide.

    Interpolating the offsets directly swings the content sideways during a
    big zoom; moving the document point at the view center and the zoom's
    logarithm reads as one smooth camera move.
    """
    k = ease_in_out(t)
    zoom = math.exp(math.log(start.zoom) + (math.log(end.zoom) - math.log(start.zoom)) * k)
    sx, sy = start.to_document(width / 2, height / 2)
    ex, ey = end.to_document(width / 2, height / 2)
    cx, cy = sx + (ex - sx) * k, sy + (ey - sy) * k
    return View(zoom, width / 2 - cx * zoom, height / 2 - cy * zoom)
