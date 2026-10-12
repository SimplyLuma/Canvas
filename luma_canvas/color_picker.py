# SPDX-License-Identifier: MPL-2.0
"""Canvas's color picker, style chip and paint swatches.

The picker is a 240px popover (never GTK's color dialog) that edits live:
a saturation and brightness field, a hue slider that keeps its hue at grays,
an alpha slider over a checkerboard, value fields for Hex, RGB, HSL, HSB and
CSS with scrubbing and typed arithmetic, swatches from the document, color
styles and recent colors, linear and radial gradients with a stop bar and
on-canvas handles, and an eyedropper that samples the rendered canvas or,
outside it, the screen through the desktop portal.

Every continuous interaction reports three phases to its consumer, so a
drag in the field or a scrub is one undo step:

    on_change(paint, "begin")   # once, before the first change
    on_change(paint, "change")  # for every update, to preview live
    on_change(paint, "end")     # once, when the pointer or key is released

`paint` is a paint dictionary from `paint.py` (`{"type": "solid", "color":
{r, g, b, a}, ...}` or a linear or radial gradient), or None for no paint.

Public entry points:

* `open_color_popover(anchor_widget, paint, on_change, ...)`
* `open_paint_picker(anchor_widget, canvas, target)` binds the picker to the
  canvas selection's fill or stroke, or to the current style.
* `StyleChip(canvas_provider)`, the fill and stroke chip for the tool rail.
* `PaintSwatch(paint, on_activate)`, a 20px swatch button for paint rows.
* `pick_screen_color(widget, callback)`, the portal's PickColor.
"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import secrets
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .paint import (  # noqa: E402
    Color, RecentColors, color_of, convert_paint, css_string, document_colors, hsl_to_rgb,
    hsv_to_rgb, interpolate, normalized_style, object_paint, paint_color, parse_color,
    rgb_to_hsl, rgb_to_hsv, solid, sorted_stops, to_hex,
)

try:  # The kit's safe arithmetic, when the platform is new enough.
    from luma_appkit import evaluate_number  # type: ignore
except ImportError:  # pragma: no cover - older platform
    evaluate_number = None

PICKER_WIDTH = 240
CONTENT_WIDTH = 216
FIELD_HEIGHT = 160
SLIDER_HEIGHT = 12
MODELS = ("Hex", "RGB", "HSL", "HSB", "CSS")
SOURCES = ("Document colors", "Color styles", "Recent")
Phase = str
OnChange = Callable[[dict[str, Any] | None, Phase], None]

CSS = """
popover.canvas-color-picker > contents { padding: 12px; min-width: 216px; border-radius: 12px; }
.canvas-color-picker .picker-types { min-height: 26px; }
.canvas-color-picker .picker-types > button { min-height: 26px; min-width: 0; padding: 0 9px;
  font-size: 12px; font-weight: 600; }
.canvas-color-picker .picker-types > button:checked { font-weight: 650; }
.canvas-color-picker .picker-close { min-width: 26px; min-height: 26px; padding: 0; }
.canvas-color-picker .picker-area { border-radius: 6px; }
.canvas-color-picker .picker-area:focus-visible { outline: 2px solid alpha(@luma_accent, .85);
  outline-offset: 2px; }
.canvas-color-picker .picker-slider { border-radius: 6px; }
.canvas-color-picker .picker-slider:focus-visible { outline: 2px solid alpha(@luma_accent, .85);
  outline-offset: 2px; }
.canvas-color-picker .picker-dropper { min-width: 28px; min-height: 28px; padding: 0;
  border-radius: 6px; }
.canvas-color-picker .picker-model { min-height: 26px; min-width: 0; padding: 0 4px 0 8px;
  font-size: 12px; font-weight: 600; }
.canvas-color-picker .picker-model > button { min-height: 26px; padding: 0 4px 0 8px; font-size: 12px;
  font-weight: 600; }
.canvas-color-picker .picker-value { min-height: 26px; padding: 0 6px; border-radius: 6px;
  background: @luma_fill; box-shadow: inset 0 0 0 1px @luma_line; }
.canvas-color-picker .picker-value:focus-within { box-shadow: inset 0 0 0 1px alpha(@luma_accent, .9),
  0 0 0 2px alpha(@luma_accent, .25); }
.canvas-color-picker .picker-value.error { box-shadow: inset 0 0 0 1px @luma_destructive; }
.canvas-color-picker .picker-value > entry, .canvas-color-picker .picker-value > text {
  min-height: 24px; min-width: 0; padding: 0; border: 0; box-shadow: none; background: transparent;
  font-size: 12px; font-feature-settings: "tnum"; color: @luma_ink; }
.canvas-color-picker .picker-value > label.picker-letter { font-size: 11px; font-weight: 550;
  color: @luma_muted; margin-right: 4px; }
.canvas-color-picker .picker-value > label.picker-unit { font-size: 11px; color: @luma_faint;
  margin-left: 2px; }
.canvas-color-picker .picker-source > button { min-height: 22px; padding: 0 4px; font-size: 11px;
  font-weight: 600; color: @luma_muted; background: transparent; box-shadow: none; }
.canvas-color-picker .picker-add { min-width: 22px; min-height: 22px; padding: 0; }
.canvas-color-picker .picker-empty { font-size: 11px; color: @luma_faint; }
.canvas-style-chip { border-radius: 8px; }
.canvas-style-chip:hover { background: @luma_hover; }
.canvas-style-chip:focus-visible { outline: 2px solid alpha(@luma_accent, .85); outline-offset: 1px; }
.canvas-paint-swatch { min-width: 20px; min-height: 20px; padding: 0; border-radius: 4px;
  background: transparent; box-shadow: none; border: 0; }
.canvas-paint-swatch:focus-visible { outline: 2px solid alpha(@luma_accent, .85); outline-offset: 2px; }
 .canvas-color-picker.phone .picker-types > button,
.canvas-color-picker.phone .picker-close,
.canvas-color-picker.phone .picker-dropper,
.canvas-color-picker.phone .picker-model,
.canvas-color-picker.phone .picker-source > button,
.canvas-color-picker.phone .picker-add { min-height: 44px; }
.canvas-color-picker.phone .picker-value > entry,
.canvas-color-picker.phone .picker-value > text { min-height: 44px; }
"""

_css_installed = False


def install_css() -> None:
    global _css_installed
    display = Gdk.Display.get_default()
    if _css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    _css_installed = True


# ── drawing helpers ──────────────────────────────────────────────────────

def _rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, w, h)


def _point(x: float, y: float) -> Graphene.Point:
    return Graphene.Point().init(x, y)


def _rgba(color: Color, alpha: float | None = None) -> Gdk.RGBA:
    return Gdk.RGBA(red=color.r, green=color.g, blue=color.b, alpha=color.a if alpha is None else alpha)


def _rounded(x: float, y: float, w: float, h: float, radius: float) -> Gsk.RoundedRect:
    value = Gsk.RoundedRect()
    value.init_from_rect(_rect(x, y, w, h), radius)
    return value


def append_checkerboard(snapshot: Gtk.Snapshot, area: Graphene.Rect, size: float = 4.0) -> None:
    """Checks (#ffffff and #e6e6e6) under anything with alpha (spec 6.3)."""
    tile = _rect(area.get_x(), area.get_y(), size * 2, size * 2)
    snapshot.push_repeat(area, tile)
    x, y = area.get_x(), area.get_y()
    snapshot.append_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=1), _rect(x, y, size * 2, size * 2))
    light = Gdk.RGBA(red=0.902, green=0.902, blue=0.902, alpha=1)
    snapshot.append_color(light, _rect(x + size, y, size, size))
    snapshot.append_color(light, _rect(x, y + size, size, size))
    snapshot.pop()


def append_thumb(snapshot: Gtk.Snapshot, x: float, y: float, diameter: float, fill: Color,
                 *, checks: bool = False) -> None:
    """A round thumb: fill, a 2px white ring and a faint outer line."""
    builder = Gsk.PathBuilder.new()
    builder.add_circle(_point(x, y), diameter / 2)
    circle = builder.to_path()
    shadow = Gsk.Shadow()
    shadow.color = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.22)
    shadow.dx, shadow.dy, shadow.radius = 0, 1, 2
    snapshot.push_shadow([shadow])
    snapshot.append_fill(circle, Gsk.FillRule.WINDING, Gdk.RGBA(red=1, green=1, blue=1, alpha=1))
    snapshot.pop()
    inner = Gsk.PathBuilder.new()
    inner.add_circle(_point(x, y), diameter / 2 - 2)
    inner_path = inner.to_path()
    if checks and fill.a < 1:
        snapshot.push_fill(inner_path, Gsk.FillRule.WINDING)
        append_checkerboard(snapshot, _rect(x - diameter / 2, y - diameter / 2, diameter, diameter), 3)
        snapshot.pop()
    snapshot.append_fill(inner_path, Gsk.FillRule.WINDING, _rgba(fill))
    snapshot.append_stroke(circle, Gsk.Stroke.new(1.0), Gdk.RGBA(red=0, green=0, blue=0, alpha=0.2))


def paint_preview(snapshot: Gtk.Snapshot, area: Graphene.Rect, paint: dict[str, Any] | None,
                  radius: float = 4.0) -> None:
    """A swatch: checks under alpha, a gradient ramp, or no paint's red slash."""
    x, y, w, h = area.get_x(), area.get_y(), area.get_width(), area.get_height()
    snapshot.push_rounded_clip(_rounded(x, y, w, h, radius))
    if paint is None:
        snapshot.append_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=1), area)
        slash = Gsk.PathBuilder.new()
        slash.move_to(x + w - 2, y + 2)
        slash.line_to(x + 2, y + h - 2)
        snapshot.append_stroke(slash.to_path(), Gsk.Stroke.new(1.5),
                               Gdk.RGBA(red=0.9, green=0.22, blue=0.2, alpha=1))
    else:
        append_checkerboard(snapshot, area, 3 if w <= 24 else 4)
        if paint.get("type", "solid") == "solid":
            snapshot.append_color(_rgba(Color.from_dict(paint.get("color", {}))), area)
        else:
            stops = []
            for stop in sorted_stops(paint):
                value = Gsk.ColorStop()
                value.offset = max(0.0, min(1.0, float(stop["position"])))
                value.color = _rgba(Color.from_dict(stop["color"]))
                stops.append(value)
            if len(stops) >= 2:
                if paint.get("type") == "radial":
                    snapshot.append_radial_gradient(area, _point(x + w / 2, y + h / 2), w / 2, h / 2, 0, 1, stops)
                else:
                    snapshot.append_linear_gradient(area, _point(x, y + h / 2), _point(x + w, y + h / 2), stops)
    snapshot.pop()
    border = Gsk.PathBuilder.new()
    border.add_rounded_rect(_rounded(x + 0.5, y + 0.5, w - 1, h - 1, max(0.0, radius - 0.5)))
    snapshot.append_stroke(border.to_path(), Gsk.Stroke.new(1.0), Gdk.RGBA(red=0, green=0, blue=0, alpha=0.12))


# ── the picker's parts ───────────────────────────────────────────────────

class _Area(Gtk.Widget):
    """A focusable drawn control that reports begin, change and end."""

    __gsignals__ = {"interaction": (GObject.SignalFlags.RUN_LAST, None, (str,))}

    def __init__(self, width: int, height: int, name: str) -> None:
        super().__init__()
        self.set_size_request(width, height)
        self.set_focusable(True)
        self.set_can_focus(True)
        self.update_property([Gtk.AccessibleProperty.LABEL], [name])
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self._origin = (0.0, 0.0)
        self._axis: str | None = None

    def _begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self.grab_focus()
        self._origin = (x, y)
        self._axis = None
        self.emit("interaction", "begin")
        self.pointer(x, y, gesture.get_current_event_state())
        self.emit("interaction", "change")

    def _update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        state = gesture.get_current_event_state()
        x, y = self._origin[0] + dx, self._origin[1] + dy
        if state & Gdk.ModifierType.SHIFT_MASK:
            if self._axis is None and math.hypot(dx, dy) > 4:
                self._axis = "x" if abs(dx) >= abs(dy) else "y"
            if self._axis == "x":
                y = self._origin[1]
            elif self._axis == "y":
                x = self._origin[0]
        self.pointer(x, y, state)
        self.emit("interaction", "change")

    def _end(self, _gesture: Gtk.GestureDrag, _dx: float, _dy: float) -> None:
        self.emit("interaction", "end")

    def _key(self, _controller: Any, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        step = 10 if state & Gdk.ModifierType.SHIFT_MASK else 1
        handled = self.key(Gdk.keyval_name(keyval) or "", step)
        if handled:
            self.emit("interaction", "begin")
            self.emit("interaction", "change")
            self.emit("interaction", "end")
        return handled

    def pointer(self, x: float, y: float, state: Gdk.ModifierType) -> None:  # pragma: no cover
        raise NotImplementedError

    def key(self, name: str, step: int) -> bool:  # pragma: no cover
        return False


class SaturationField(_Area):
    """Saturation left to right, brightness top to bottom, at the picker's hue."""

    def __init__(self, picker: "ColorPicker") -> None:
        super().__init__(CONTENT_WIDTH, FIELD_HEIGHT, "Saturation and brightness")
        self.picker = picker
        self.add_css_class("picker-area")
        self.set_cursor_from_name("crosshair")

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        picker = self.picker
        area = _rect(0, 0, width, height)
        snapshot.push_rounded_clip(_rounded(0, 0, width, height, 6))
        snapshot.append_color(_rgba(hsv_to_rgb(picker.hue, 1, 1)), area)
        white = [Gsk.ColorStop(), Gsk.ColorStop()]
        white[0].offset, white[0].color = 0.0, Gdk.RGBA(red=1, green=1, blue=1, alpha=1)
        white[1].offset, white[1].color = 1.0, Gdk.RGBA(red=1, green=1, blue=1, alpha=0)
        snapshot.append_linear_gradient(area, _point(0, 0), _point(width, 0), white)
        black = [Gsk.ColorStop(), Gsk.ColorStop()]
        black[0].offset, black[0].color = 0.0, Gdk.RGBA(red=0, green=0, blue=0, alpha=0)
        black[1].offset, black[1].color = 1.0, Gdk.RGBA(red=0, green=0, blue=0, alpha=1)
        snapshot.append_linear_gradient(area, _point(0, 0), _point(0, height), black)
        snapshot.pop()
        edge = Gsk.PathBuilder.new()
        edge.add_rounded_rect(_rounded(0.5, 0.5, width - 1, height - 1, 5.5))
        snapshot.append_stroke(edge.to_path(), Gsk.Stroke.new(1.0), Gdk.RGBA(red=0, green=0, blue=0, alpha=0.1))
        x = picker.saturation * width
        y = (1 - picker.brightness) * height
        append_thumb(snapshot, x, y, 12, hsv_to_rgb(picker.hue, picker.saturation, picker.brightness))

    def pointer(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        width, height = max(1, self.get_width()), max(1, self.get_height())
        self.picker.set_hsv(saturation=min(1.0, max(0.0, x / width)),
                            brightness=min(1.0, max(0.0, 1 - y / height)))

    def key(self, name: str, step: int) -> bool:
        s, v = self.picker.saturation * 100, self.picker.brightness * 100
        moves = {"Left": (-step, 0), "Right": (step, 0), "Up": (0, step), "Down": (0, -step)}
        if name not in moves:
            return False
        ds, dv = moves[name]
        self.picker.set_hsv(saturation=max(0, min(100, s + ds)) / 100,
                            brightness=max(0, min(100, v + dv)) / 100)
        return True

    def describe(self) -> None:
        text = f"Saturation {round(self.picker.saturation * 100)}%, brightness {round(self.picker.brightness * 100)}%"
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text])


class ChannelSlider(_Area):
    """The hue rainbow or the alpha ramp over checks."""

    def __init__(self, picker: "ColorPicker", channel: str) -> None:
        super().__init__(CONTENT_WIDTH - 36, SLIDER_HEIGHT, "Hue" if channel == "hue" else "Opacity")
        self.picker = picker
        self.channel = channel
        self.add_css_class("picker-slider")
        self.set_hexpand(True)
        # Accessible values are doubles; Python ints would arrive as ints.
        self.update_property([Gtk.AccessibleProperty.VALUE_MIN, Gtk.AccessibleProperty.VALUE_MAX],
                             [0.0, 360.0 if channel == "hue" else 100.0])

    def _value(self) -> float:
        return self.picker.hue / 360 if self.channel == "hue" else self.picker.alpha

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, hit_height = self.get_width(), self.get_height()
        height=min(hit_height,SLIDER_HEIGHT)
        snapshot.save();snapshot.translate(_point(0,(hit_height-height)/2))
        area = _rect(0, 0, width, height)
        radius = height / 2
        snapshot.push_rounded_clip(_rounded(0, 0, width, height, radius))
        stops = []
        if self.channel == "hue":
            for index in range(7):
                stop = Gsk.ColorStop()
                stop.offset = index / 6
                stop.color = _rgba(hsv_to_rgb(index * 60, 1, 1))
                stops.append(stop)
        else:
            append_checkerboard(snapshot, area, 4)
            color = self.picker.color()
            for offset, alpha in ((0.0, 0.0), (1.0, 1.0)):
                stop = Gsk.ColorStop()
                stop.offset = offset
                stop.color = _rgba(color, alpha)
                stops.append(stop)
        snapshot.append_linear_gradient(area, _point(0, 0), _point(width, 0), stops)
        snapshot.pop()
        edge = Gsk.PathBuilder.new()
        edge.add_rounded_rect(_rounded(0.5, 0.5, width - 1, height - 1, radius - 0.5))
        snapshot.append_stroke(edge.to_path(), Gsk.Stroke.new(1.0), Gdk.RGBA(red=0, green=0, blue=0, alpha=0.1))
        inset = 7
        x = inset + self._value() * (width - inset * 2)
        fill = hsv_to_rgb(self.picker.hue, 1, 1) if self.channel == "hue" else self.picker.color()
        append_thumb(snapshot, x, height / 2, 14, fill, checks=self.channel == "alpha")

        snapshot.restore()

    def pointer(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        inset = 7
        value = min(1.0, max(0.0, (x - inset) / max(1, self.get_width() - inset * 2)))
        if self.channel == "hue":
            self.picker.set_hsv(hue=value * 360)
        else:
            self.picker.set_alpha(value)

    def key(self, name: str, step: int) -> bool:
        if self.channel == "hue":
            current, top = self.picker.hue, 360.0
        else:
            current, top = self.picker.alpha * 100, 100.0
        values = {"Left": current - step, "Down": current - step, "Right": current + step,
                  "Up": current + step, "Home": 0.0, "End": top}
        if name not in values:
            return False
        value = max(0.0, min(top, values[name]))
        if self.channel == "hue":
            self.picker.set_hsv(hue=value)
        else:
            self.picker.set_alpha(value / 100)
        return True


class GradientBar(_Area):
    """The gradient ramp with its stops; click adds, drag moves, drag away removes."""

    def __init__(self, picker: "ColorPicker") -> None:
        super().__init__(CONTENT_WIDTH - 34, 26, "Gradient stops")
        self.picker = picker
        self.set_hexpand(True)
        self.add_css_class("picker-slider")
        self._dragging: int | None = None
        self._start_y = 0.0

    def _geometry(self) -> tuple[float, float]:
        return 6.0, max(1.0, self.get_width() - 12.0)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        paint = self.picker.paint
        if not paint or paint.get("type") == "solid":
            return
        left, span = self._geometry()
        ramp = _rect(left, 0, span, 12)
        snapshot.push_rounded_clip(_rounded(left, 0, span, 12, 3))
        append_checkerboard(snapshot, ramp, 4)
        snapshot.pop()
        preview = dict(paint)
        preview["type"] = "linear"
        paint_preview(snapshot, ramp, preview, 3)
        accent = self.get_style_context().lookup_color("luma_accent")[1]
        for index, stop in enumerate(paint.get("stops", [])):
            x = left + float(stop["position"]) * span
            selected = index == self.picker.stop_index
            area = _rounded(x - 6, 13, 12, 12, 3)
            path = Gsk.PathBuilder.new()
            path.add_rounded_rect(area)
            square = path.to_path()
            snapshot.append_fill(square, Gsk.FillRule.WINDING, Gdk.RGBA(red=1, green=1, blue=1, alpha=1))
            inner = Gsk.PathBuilder.new()
            inner.add_rounded_rect(_rounded(x - 4, 15, 8, 8, 2))
            snapshot.append_fill(inner.to_path(), Gsk.FillRule.WINDING, _rgba(Color.from_dict(stop["color"]), 1.0))
            snapshot.append_stroke(square, Gsk.Stroke.new(2.0 if selected else 1.0),
                                   accent if selected else Gdk.RGBA(red=0, green=0, blue=0, alpha=0.25))

    def _stop_at(self, x: float) -> int | None:
        left, span = self._geometry()
        for index, stop in enumerate(self.picker.paint.get("stops", [])):
            if abs(left + float(stop["position"]) * span - x) <= 7:
                return index
        return None

    def _begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self.grab_focus()
        self._origin = (x, y)
        self._start_y = y
        index = self._stop_at(x)
        self.emit("interaction", "begin")
        if index is None:
            left, span = self._geometry()
            index = self.picker.add_stop(min(1.0, max(0.0, (x - left) / span)))
        self._dragging = index
        self.picker.select_stop(index)
        self.emit("interaction", "change")

    def _update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._dragging is None:
            return
        left, span = self._geometry()
        x = self._origin[0] + dx
        y = self._start_y + dy
        if abs(y - 12) > 30 and len(self.picker.paint.get("stops", [])) > 2:
            self.picker.remove_stop(self._dragging)
            self._dragging = None
        else:
            self.picker.move_stop(self._dragging, min(1.0, max(0.0, (x - left) / span)))
        self.emit("interaction", "change")

    def key(self, name: str, step: int) -> bool:
        if name in {"Delete", "BackSpace"} and len(self.picker.paint.get("stops", [])) > 2:
            self.picker.remove_stop(self.picker.stop_index)
            return True
        if name in {"Left", "Right"}:
            stop = self.picker.paint["stops"][self.picker.stop_index]
            delta = (step if name == "Right" else -step) / 100
            self.picker.move_stop(self.picker.stop_index, min(1.0, max(0.0, float(stop["position"]) + delta)))
            return True
        return False


class ValueField(Gtk.Box):
    """A compact value box: optional letter, entry, unit; scrub the letter or the value."""

    __gsignals__ = {
        "committed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "scrub": (GObject.SignalFlags.RUN_LAST, None, (str, float)),
    }

    def __init__(self, letter: str = "", unit: str = "", *, width_chars: int = 3,
                 scrub_step: float = 1.0, name: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("picker-value")
        self.scrub_step = scrub_step
        self._restore = ""
        self._scrub_origin = 0.0
        self._scrub_value = 0.0
        self._scrubbing = False
        if letter:
            self.letter = Gtk.Label(label=letter)
            self.letter.add_css_class("picker-letter")
            self.letter.set_cursor_from_name("ew-resize")
            self.append(self.letter)
            self._add_scrub(self.letter)
        self.entry = Gtk.Text()
        self.entry.set_hexpand(True)
        self.entry.set_width_chars(width_chars)
        self.entry.set_max_width_chars(width_chars)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [name or letter or unit])
        self.entry.connect("activate", lambda *_: self._commit())
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: setattr(self, "_restore", self.entry.get_text()))
        focus.connect("leave", lambda *_: self._commit(leaving=True))
        self.entry.add_controller(focus)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.entry.add_controller(keys)
        self.append(self.entry)
        if unit:
            label = Gtk.Label(label=unit)
            label.add_css_class("picker-unit")
            label.set_cursor_from_name("ew-resize")
            self.append(label)
            if not letter:
                self._add_scrub(label)

    def _add_scrub(self, widget: Gtk.Widget) -> None:
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._scrub_begin)
        drag.connect("drag-update", self._scrub_update)
        drag.connect("drag-end", self._scrub_end)
        widget.add_controller(drag)

    def value(self) -> float | None:
        text = self.entry.get_text().strip().rstrip("%°")
        if evaluate_number is not None:
            return evaluate_number(text)
        try:
            return float(text)
        except ValueError:
            return None

    def set_text(self, text: str) -> None:
        if not self.entry.has_focus() or self._scrubbing:
            self.entry.set_text(text)
        self.remove_css_class("error")

    def _commit(self, leaving: bool = False) -> None:
        text = self.entry.get_text()
        if leaving and text == self._restore:
            return
        self.emit("committed", text)
        self._restore = self.entry.get_text()

    def _key(self, _controller: Any, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        name = Gdk.keyval_name(keyval) or ""
        if name == "Escape":
            self.entry.set_text(self._restore)
            self.remove_css_class("error")
            root = self.get_root()
            if root is not None:
                root.set_focus(None)
            return True
        if name in {"Up", "Down"}:
            current = self.value()
            if current is None:
                return False
            step = self.scrub_step * (10 if state & Gdk.ModifierType.SHIFT_MASK else 1)
            self.emit("scrub", "begin", current)
            self.emit("scrub", "change", current + (step if name == "Up" else -step))
            self.emit("scrub", "end", current)
            self._restore = self.entry.get_text()
            return True
        return False

    def _scrub_begin(self, gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        current = self.value()
        if current is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._scrubbing = True
        self._scrub_origin = current
        self.emit("scrub", "begin", current)

    def _scrub_update(self, gesture: Gtk.GestureDrag, dx: float, _dy: float) -> None:
        if not self._scrubbing:
            return
        state = gesture.get_current_event_state()
        scale = 10 if state & Gdk.ModifierType.SHIFT_MASK else 0.1 if state & Gdk.ModifierType.ALT_MASK else 1
        self.emit("scrub", "change", self._scrub_origin + round(dx / 2.0) * self.scrub_step * scale)

    def _scrub_end(self, *_args: Any) -> None:
        if self._scrubbing:
            self._scrubbing = False
            self.emit("scrub", "end", self._scrub_origin)


class SwatchGrid(Gtk.Widget):
    """20px swatches with 2px gaps; click applies, tooltips name styles."""

    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_LAST, None, (int,))}
    SIZE, COLUMNS = 20.0, 10

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[tuple[dict[str, Any] | None, str]] = []
        self.selected: int | None = None
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)
        self.set_cursor_from_name("pointer")

    def set_entries(self, entries: list[tuple[dict[str, Any] | None, str]]) -> None:
        self.entries = entries
        rows = max(1, math.ceil(len(entries) / self.COLUMNS))
        self.set_size_request(CONTENT_WIDTH, int(rows * self.SIZE + (rows - 1) * 2))
        self.queue_resize()
        self.queue_draw()

    def _gap(self) -> float:
        return (CONTENT_WIDTH - self.COLUMNS * self.SIZE) / (self.COLUMNS - 1)

    def _index_at(self, x: float, y: float) -> int | None:
        gap = self._gap()
        column = int(x // (self.SIZE + gap))
        row = int(y // (self.SIZE + 2))
        if column >= self.COLUMNS or x - column * (self.SIZE + gap) > self.SIZE:
            return None
        index = row * self.COLUMNS + column
        return index if 0 <= index < len(self.entries) else None

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        gap = self._gap()
        accent = self.get_style_context().lookup_color("luma_accent")[1]
        for index, (paint, _name) in enumerate(self.entries):
            row, column = divmod(index, self.COLUMNS)
            x, y = column * (self.SIZE + gap), row * (self.SIZE + 2)
            paint_preview(snapshot, _rect(x, y, self.SIZE, self.SIZE), paint, 4)
            if index == self.selected:
                ring = Gsk.PathBuilder.new()
                ring.add_rounded_rect(_rounded(x - 1.5, y - 1.5, self.SIZE + 3, self.SIZE + 3, 5))
                snapshot.append_stroke(ring.to_path(), Gsk.Stroke.new(1.5), accent)

    def _tooltip(self, _widget: Gtk.Widget, x: int, y: int, _keyboard: bool, tooltip: Gtk.Tooltip) -> bool:
        index = self._index_at(x, y)
        if index is None:
            return False
        tooltip.set_text(self.entries[index][1])
        return True

    def _clicked(self, _gesture: Gtk.GestureClick, _count: int, x: float, y: float) -> None:
        index = self._index_at(x, y)
        if index is not None:
            self.emit("chosen", index)


class _Glyph(Gtk.Widget):
    """A tiny eyedropper glyph drawn in the ink color, sized for 14px."""

    def __init__(self) -> None:
        super().__init__()
        self.set_size_request(16, 16)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        ink = self.get_color()
        w, h = self.get_width(), self.get_height()
        s = min(w, h) / 24
        ox, oy = (w - 24 * s) / 2, (h - 24 * s) / 2
        builder = Gsk.PathBuilder.new()

        def p(x: float, y: float) -> tuple[float, float]:
            return ox + x * s, oy + y * s
        # Bulb and barrel of a dropper, drawn on a 24 unit grid.
        builder.move_to(*p(14.5, 4.5))
        builder.line_to(*p(19.5, 9.5))
        builder.move_to(*p(17, 7))
        builder.line_to(*p(19.2, 4.8))
        builder.cubic_to(*p(20.3, 3.7), *p(22, 5.4), *p(20.9, 6.5))
        builder.line_to(*p(18.7, 8.7))
        builder.move_to(*p(16.2, 6.2))
        builder.line_to(*p(6.5, 15.9))
        builder.line_to(*p(5.5, 18.5))
        builder.line_to(*p(8.1, 17.5))
        builder.line_to(*p(17.8, 7.8))
        stroke = Gsk.Stroke.new(1.8 * s)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        snapshot.append_stroke(builder.to_path(), stroke, ink)


# ── the picker ───────────────────────────────────────────────────────────

class _Settings:
    """Per-user picker preferences: the value model and the swatch source."""

    def __init__(self) -> None:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.path = base / "luma-canvas" / "picker.json"

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, **values: Any) -> None:
        data = self.load()
        data.update(values)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data))
        except OSError:
            pass


class ColorPicker(Gtk.Box):
    """The picker's content. `ColorPopover` hosts it; tests can host it directly."""

    def __init__(self, paint: dict[str, Any] | None, on_change: OnChange, *,
                 canvas: Any = None, title: str = "Fill", allow_gradients: bool = True,
                 on_close: Callable[[], None] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        install_css()
        self.on_change = on_change
        self.canvas = canvas
        self.title = title
        self.on_close = on_close
        self.settings = _Settings()
        preferences = self.settings.load()
        self.model = preferences.get("model", "Hex") if preferences.get("model") in MODELS else "Hex"
        self.source = preferences.get("source", SOURCES[0]) if preferences.get("source") in SOURCES else SOURCES[0]
        self.paint: dict[str, Any] = copy.deepcopy(paint) if paint else solid(Color(0.851, 0.851, 0.851))
        self.none = paint is None
        self.stop_index = 0
        self.hue, self.saturation, self.brightness, self.alpha = 0.0, 0.0, 0.85, 1.0
        self._in_gesture = False
        self._syncing = False
        self.set_size_request(CONTENT_WIDTH, -1)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{title} color"])

        # Paint type and close.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        types = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        types.add_css_class("luma-connected-buttons")
        types.add_css_class("compact")
        types.add_css_class("picker-types")
        self.type_buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for kind, label in (("solid", "Solid"), ("linear", "Linear"), ("radial", "Radial")):
            button = Gtk.ToggleButton(label=label)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._type_toggled, kind)
            button.set_visible(allow_gradients or kind == "solid")
            types.append(button)
            self.type_buttons[kind] = button
        header.append(types)
        header.append(Gtk.Box(hexpand=True))
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.add_css_class("picker-close")
        close.set_tooltip_text("Close")
        close.update_property([Gtk.AccessibleProperty.LABEL], ["Close"])
        close.connect("clicked", lambda *_: self.on_close() if self.on_close else None)
        header.append(close)
        self.append(header)

        # Gradient stops.
        self.gradient_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.gradient_bar = GradientBar(self)
        self.gradient_bar.connect("interaction", self._interaction)
        self.gradient_row.append(self.gradient_bar)
        flip = Gtk.Button(icon_name="object-flip-horizontal-symbolic")
        flip.add_css_class("flat")
        flip.add_css_class("picker-close")
        flip.set_tooltip_text("Flip gradient")
        flip.update_property([Gtk.AccessibleProperty.LABEL], ["Flip gradient"])
        flip.connect("clicked", lambda *_: self._single(self.flip_stops))
        self.gradient_row.append(flip)
        self.append(self.gradient_row)

        # Field and sliders.
        self.field = SaturationField(self)
        self.field.connect("interaction", self._interaction)
        self.append(self.field)
        sliders = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dropper = Gtk.Button()
        dropper.set_child(_Glyph())
        dropper.add_css_class("flat")
        dropper.add_css_class("picker-dropper")
        dropper.set_tooltip_text("Pick a color from the canvas or the screen · I")
        dropper.update_property([Gtk.AccessibleProperty.LABEL], ["Pick a color"])
        dropper.set_valign(Gtk.Align.CENTER)
        dropper.connect("clicked", lambda *_: self.start_eyedropper())
        sliders.append(dropper)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        column.set_hexpand(True)
        column.set_valign(Gtk.Align.CENTER)
        self.hue_slider = ChannelSlider(self, "hue")
        self.alpha_slider = ChannelSlider(self, "alpha")
        for slider in (self.hue_slider, self.alpha_slider):
            slider.connect("interaction", self._interaction)
            column.append(slider)
        sliders.append(column)
        self.append(sliders)

        # Value row.
        values = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.model_button = Gtk.MenuButton(label=self.model)
        self.model_button.add_css_class("flat")
        self.model_button.add_css_class("picker-model")
        self.model_button.update_property([Gtk.AccessibleProperty.LABEL], ["Color model"])
        model_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._model_popover = Gtk.Popover()
        self._model_popover.set_has_arrow(False)
        for model in MODELS:
            row = Gtk.Button(label=model)
            row.add_css_class("flat")
            row.connect("clicked", self._model_chosen, model)
            model_list.append(row)
        self._model_popover.set_child(model_list)
        self.model_button.set_popover(self._model_popover)
        values.append(self.model_button)
        self.value_fields = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.value_fields.set_hexpand(True)
        values.append(self.value_fields)
        self.alpha_field = ValueField(unit="%", width_chars=3, scrub_step=1, name="Opacity")
        self.alpha_field.set_size_request(50, -1)
        self.alpha_field.connect("committed", self._alpha_committed)
        self.alpha_field.connect("scrub", self._alpha_scrubbed)
        values.append(self.alpha_field)
        self.append(values)

        # Swatches.
        source_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.source_button = Gtk.MenuButton(label=self.source)
        self.source_button.add_css_class("flat")
        self.source_button.add_css_class("picker-source")
        source_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        source_popover = Gtk.Popover()
        source_popover.set_has_arrow(False)
        for source in SOURCES:
            row = Gtk.Button(label=source)
            row.add_css_class("flat")
            row.connect("clicked", self._source_chosen, source, source_popover)
            source_list.append(row)
        source_popover.set_child(source_list)
        self.source_button.set_popover(source_popover)
        source_row.append(self.source_button)
        source_row.append(Gtk.Box(hexpand=True))
        self.add_style = Gtk.Button(icon_name="list-add-symbolic")
        self.add_style.add_css_class("flat")
        self.add_style.add_css_class("picker-add")
        self.add_style.set_tooltip_text("Create a color style from this color")
        self.add_style.update_property([Gtk.AccessibleProperty.LABEL], ["Create color style"])
        self.add_style.connect("clicked", lambda *_: self._begin_style_name())
        source_row.append(self.add_style)
        self.append(source_row)
        self.style_name = Gtk.Entry(placeholder_text="Color style name")
        self.style_name.set_visible(False)
        self.style_name.connect("activate", lambda *_: self._create_style())
        self.append(self.style_name)
        self.swatches = SwatchGrid()
        self.swatches.connect("chosen", self._swatch_chosen)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(64)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(self.swatches)
        self.append(scroller)
        self.swatch_empty = Gtk.Label(label="No colors yet", xalign=0)
        self.swatch_empty.add_css_class("picker-empty")
        self.append(self.swatch_empty)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._picker_key)
        self.add_controller(keys)

        self._load_color_from_paint()
        self._rebuild_value_fields()
        self._refresh_swatches()
        self._sync_all()

    # ── state ────────────────────────────────────────────────────────────

    def color(self) -> Color:
        return hsv_to_rgb(self.hue, self.saturation, self.brightness, self.alpha)

    def _stop_color(self) -> Color:
        if self.paint.get("type", "solid") == "solid":
            return Color.from_dict(self.paint.get("color", {}))
        stops = self.paint.get("stops", [])
        if not stops:
            return Color(0, 0, 0, 1)
        self.stop_index = max(0, min(self.stop_index, len(stops) - 1))
        return Color.from_dict(stops[self.stop_index]["color"])

    def _load_color_from_paint(self) -> None:
        self._set_color_state(self._stop_color())

    def _set_color_state(self, color: Color) -> None:
        """Adopt a color without losing hue at grays or saturation at black."""
        hue, saturation, brightness = rgb_to_hsv(color)
        current = hsv_to_rgb(self.hue, self.saturation, self.brightness, self.alpha)
        if to_hex(current, alpha=True) == to_hex(color, alpha=True):
            return
        if brightness > 1e-6 and saturation > 1e-6:
            self.hue = hue
        if brightness > 1e-6:
            self.saturation = saturation
        self.brightness = brightness
        self.alpha = color.a

    def set_paint(self, paint: dict[str, Any] | None) -> None:
        """Follow an outside change (undo, the canvas) without emitting."""
        self.none = paint is None
        if paint is not None:
            self.paint = copy.deepcopy(paint)
        self._load_color_from_paint()
        self._sync_all()

    def _write_color(self) -> None:
        color = self.color()
        if self.paint.get("type", "solid") == "solid":
            self.paint["color"] = color.to_dict()
        else:
            stops = self.paint.setdefault("stops", [])
            if stops:
                stops[self.stop_index]["color"] = color.to_dict()
        self.none = False

    def set_hsv(self, *, hue: float | None = None, saturation: float | None = None,
                brightness: float | None = None) -> None:
        if hue is not None:
            self.hue = hue % 360 if hue < 360 else 360.0 - 1e-9
        if saturation is not None:
            self.saturation = saturation
        if brightness is not None:
            self.brightness = brightness
        self._write_color()
        self._sync_all(except_fields=False)

    def set_alpha(self, alpha: float) -> None:
        self.alpha = max(0.0, min(1.0, alpha))
        self._write_color()
        self._sync_all()

    def set_color(self, color: Color, *, keep_alpha: bool = False) -> None:
        if keep_alpha:
            color = color.with_alpha(self.alpha)
        self._set_color_state(color)
        self._write_color()
        self._sync_all()

    # ── gradients ────────────────────────────────────────────────────────

    def add_stop(self, position: float) -> int:
        stops = self.paint.setdefault("stops", [])
        color = interpolate(stops, position)
        stops.append({"position": position, "color": color.to_dict()})
        self.stop_index = len(stops) - 1
        self._load_color_from_paint()
        self._sync_all()
        return self.stop_index

    def select_stop(self, index: int) -> None:
        self.stop_index = index
        self._load_color_from_paint()
        self._sync_all()

    def move_stop(self, index: int, position: float) -> None:
        stops = self.paint.get("stops", [])
        if 0 <= index < len(stops):
            stops[index]["position"] = position
            self._sync_all()

    def remove_stop(self, index: int) -> None:
        stops = self.paint.get("stops", [])
        if len(stops) > 2 and 0 <= index < len(stops):
            stops.pop(index)
            self.stop_index = max(0, min(index, len(stops) - 1))
            self._load_color_from_paint()
            self._sync_all()

    def flip_stops(self) -> None:
        for stop in self.paint.get("stops", []):
            stop["position"] = 1.0 - float(stop["position"])
        self._sync_all()

    def _type_toggled(self, button: Gtk.ToggleButton, kind: str) -> None:
        if self._syncing or not button.get_active():
            return
        if self.paint.get("type", "solid") == kind:
            return
        self._emit("begin")
        self.paint = convert_paint(self.paint, kind)
        self.none = False
        self.stop_index = 0
        self._load_color_from_paint()
        self._sync_all()
        self._emit("change")
        self._emit("end")

    # ── emitting ─────────────────────────────────────────────────────────

    def current_paint(self) -> dict[str, Any] | None:
        return None if self.none else copy.deepcopy(self.paint)

    def _emit(self, phase: Phase) -> None:
        if phase == "begin":
            if self._in_gesture:
                return
            self._in_gesture = True
        elif phase == "end":
            if not self._in_gesture:
                return
            self._in_gesture = False
            color = paint_color(self.current_paint())
            if color is not None:
                RecentColors().add(color)
        elif not self._in_gesture:
            self.on_change(self.current_paint(), "begin")
            self._in_gesture = True
        self.on_change(self.current_paint(), phase)

    def _interaction(self, _widget: Gtk.Widget, phase: str) -> None:
        self._emit(phase)

    def _single(self, action: Callable[[], None]) -> None:
        self._emit("begin")
        action()
        self._emit("change")
        self._emit("end")

    # ── value fields ─────────────────────────────────────────────────────

    def _model_chosen(self, _button: Gtk.Button, model: str) -> None:
        self._model_popover.popdown()
        self.model = model
        self.model_button.set_label(model)
        self.settings.save(model=model)
        self._rebuild_value_fields()
        self._sync_all()

    def _rebuild_value_fields(self) -> None:
        child = self.value_fields.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.value_fields.remove(child)
            child = following
        self.fields: list[ValueField] = []
        if self.model in {"Hex", "CSS"}:
            field = ValueField(width_chars=6 if self.model == "Hex" else 14, name=self.model)
            field.set_hexpand(True)
            field.connect("committed", self._text_committed)
            delegate = field.entry
            delegate.connect("paste-clipboard", lambda *_: GLib.idle_add(self._pasted, field))
            self.value_fields.append(field)
            self.fields.append(field)
        else:
            letters = {"RGB": ("R", "G", "B"), "HSL": ("H", "S", "L"), "HSB": ("H", "S", "B")}[self.model]
            for index, letter in enumerate(letters):
                field = ValueField(letter, width_chars=3, name=f"{self.model} {letter}")
                field.set_hexpand(True)
                field.connect("committed", self._channel_committed, index)
                field.connect("scrub", self._channel_scrubbed, index)
                self.value_fields.append(field)
                self.fields.append(field)

    def _channels(self) -> tuple[float, float, float]:
        color = self.color()
        if self.model == "RGB":
            return tuple(float(value) for value in color.bytes())  # type: ignore[return-value]
        if self.model == "HSL":
            _, saturation, lightness = rgb_to_hsl(color)
            return self.hue, saturation * 100, lightness * 100
        return self.hue, self.saturation * 100, self.brightness * 100

    def _set_channels(self, values: list[float]) -> None:
        if self.model == "RGB":
            color = Color(*(max(0, min(255, value)) / 255 for value in values), self.alpha)
            self._set_color_state(color)
        elif self.model == "HSL":
            hue = max(0.0, min(360.0, values[0]))
            color = hsl_to_rgb(hue, max(0, min(100, values[1])) / 100, max(0, min(100, values[2])) / 100, self.alpha)
            self._set_color_state(color)
            self.hue = hue
        else:
            self.hue = max(0.0, min(360.0, values[0])) % 360 if values[0] < 360 else 360.0 - 1e-9
            self.saturation = max(0, min(100, values[1])) / 100
            self.brightness = max(0, min(100, values[2])) / 100
        self._write_color()

    def _channel_committed(self, field: ValueField, _text: str, index: int) -> None:
        value = field.value()
        if value is None:
            field.add_css_class("error")
            return
        values = list(self._channels())
        values[index] = value
        self._single(lambda: (self._set_channels(values), self._sync_all()))

    def _channel_scrubbed(self, _field: ValueField, phase: str, value: float, index: int) -> None:
        if phase == "begin":
            self._emit("begin")
            return
        if phase == "end":
            self._emit("end")
            return
        values = list(self._channels())
        values[index] = value
        self._set_channels(values)
        self._sync_all()
        self._emit("change")

    def _text_committed(self, field: ValueField, text: str) -> bool:
        color = parse_color(text)
        if color is None:
            field.add_css_class("error")
            return False
        field.remove_css_class("error")
        has_alpha = bool(text.strip().lower().startswith(("rgb", "hsl", "hsb", "hsv", "oklch", "oklab")) or
                         len(text.strip().lstrip("#")) in {4, 8} or text.strip().lower() == "transparent")
        self._single(lambda: self.set_color(color, keep_alpha=not has_alpha))
        return True

    def _pasted(self, field: ValueField) -> bool:
        self._text_committed(field, field.entry.get_text())
        return GLib.SOURCE_REMOVE

    def _alpha_committed(self, field: ValueField, _text: str) -> None:
        value = field.value()
        if value is None:
            field.add_css_class("error")
            return
        self._single(lambda: self.set_alpha(value / 100))

    def _alpha_scrubbed(self, _field: ValueField, phase: str, value: float) -> None:
        if phase in {"begin", "end"}:
            self._emit(phase)
            return
        self.set_alpha(value / 100)
        self._emit("change")

    # ── swatches and styles ──────────────────────────────────────────────

    def _document(self) -> Any:
        return getattr(self.canvas, "document", None)

    def _source_chosen(self, _button: Gtk.Button, source: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        self.source = source
        self.source_button.set_label(source)
        self.settings.save(source=source)
        self._refresh_swatches()

    def _refresh_swatches(self) -> None:
        entries: list[tuple[dict[str, Any] | None, str]] = []
        document = self._document()
        self._style_ids: list[str] = []
        if self.source == "Document colors" and document is not None:
            for text in document_colors(document.objects):
                color = parse_color(text)
                entries.append((solid(color), to_hex(color, prefix=False).upper()))
        elif self.source == "Color styles" and document is not None:
            for style in document.styles:
                if style.kind != "color":
                    continue
                paint = style.value.get("paint")
                if paint is None:
                    color = parse_color(str(style.value.get("value", "")))
                    paint = solid(color) if color else None
                entries.append((paint, style.name))
                self._style_ids.append(style.id)
        elif self.source == "Recent":
            for text in RecentColors().load():
                color = parse_color(text)
                entries.append((solid(color), to_hex(color, prefix=False).upper()))
        self.swatches.set_entries(entries)
        self.swatches.set_visible(bool(entries))
        empty = {"Document colors": "No colors in this document yet",
                 "Color styles": "No color styles yet", "Recent": "No recent colors yet"}[self.source]
        self.swatch_empty.set_label(empty)
        self.swatch_empty.set_visible(not entries)
        self.add_style.set_visible(document is not None and getattr(self.canvas, "editor", None) is not None)

    def _swatch_chosen(self, _grid: SwatchGrid, index: int) -> None:
        paint, _name = self.swatches.entries[index]
        if paint is None:
            return

        def apply() -> None:
            if paint.get("type", "solid") == "solid" and self.paint.get("type", "solid") != "solid":
                # A solid swatch recolors the selected gradient stop.
                self.set_color(Color.from_dict(paint["color"]))
                return
            opacity = self.paint.get("opacity", 1.0)
            self.paint = copy.deepcopy(paint)
            self.paint["opacity"] = opacity
            self.none = False
            self.stop_index = 0
            self._load_color_from_paint()
            self._sync_all()
        self._single(apply)

    def _begin_style_name(self) -> None:
        self.style_name.set_text(to_hex(self.color(), prefix=False).upper())
        self.style_name.set_visible(True)
        self.style_name.grab_focus()

    def _create_style(self) -> None:
        editor = getattr(self.canvas, "editor", None)
        document = self._document()
        name = self.style_name.get_text().strip() or to_hex(self.color(), prefix=False).upper()
        self.style_name.set_visible(False)
        if editor is None or document is None:
            return
        from .model import Style, new_id
        paint = self.current_paint()
        color = paint_color(paint) or self.color()
        style = Style(new_id("style"), name, "color", {"value": to_hex(color), "paint": paint})
        with editor.transaction(f"Create color style {name}"):
            editor.document.styles.append(style)
            editor.document.touch()
        self.source = "Color styles"
        self.source_button.set_label(self.source)
        self._refresh_swatches()
        if self.canvas is not None:
            self.canvas.emit("document-changed", f"Create color style {name}")

    # ── eyedropper ───────────────────────────────────────────────────────

    def start_eyedropper(self) -> None:
        popover = self.get_ancestor(Gtk.Popover)

        def picked(color: Color, _modifiers: dict[str, bool] | None = None) -> None:
            if popover is not None:
                popover.set_autohide(True)
            self._single(lambda: self.set_color(color.with_alpha(1.0) if color.a >= 1 else color))

        def cancelled() -> None:
            if popover is not None:
                popover.set_autohide(True)

        canvas = self.canvas
        if canvas is not None and hasattr(canvas, "begin_eyedropper") and canvas.get_mapped():
            if popover is not None:
                popover.set_autohide(False)
            canvas.begin_eyedropper(picked, on_cancel=cancelled)
            canvas.grab_focus()
        else:
            pick_screen_color(self, lambda color: picked(color) if color else None)

    # ── keyboard ─────────────────────────────────────────────────────────

    def _picker_key(self, _controller: Any, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        name = (Gdk.keyval_name(keyval) or "").lower()
        if state & Gdk.ModifierType.CONTROL_MASK and name == "z":
            root = self.canvas.get_root() if self.canvas is not None else None
            commands = getattr(root, "commands", None)
            if commands is not None:
                commands.invoke("edit.redo" if state & Gdk.ModifierType.SHIFT_MASK else "edit.undo")
                return True
        if name == "i" and not isinstance(self.get_root().get_focus() if self.get_root() else None, Gtk.Text):
            self.start_eyedropper()
            return True
        return False

    # ── sync ─────────────────────────────────────────────────────────────

    def _sync_all(self, except_fields: bool = False) -> None:
        self._syncing = True
        try:
            kind = self.paint.get("type", "solid")
            self.type_buttons.get(kind, self.type_buttons["solid"]).set_active(True)
            self.gradient_row.set_visible(kind != "solid")
            color = self.color()
            if self.model == "Hex" and self.fields:
                self.fields[0].set_text(to_hex(color, alpha=False, prefix=False).upper())
            elif self.model == "CSS" and self.fields:
                self.fields[0].set_text(css_string(color))
            else:
                for field, value in zip(self.fields, self._channels()):
                    field.set_text(f"{round(value)}")
            self.alpha_field.set_text(f"{round(self.alpha * 100)}")
            self.field.describe()
            self.hue_slider.update_property([Gtk.AccessibleProperty.VALUE_NOW, Gtk.AccessibleProperty.VALUE_TEXT],
                                            [float(round(self.hue)), f"Hue {round(self.hue)}°"])
            self.alpha_slider.update_property([Gtk.AccessibleProperty.VALUE_NOW, Gtk.AccessibleProperty.VALUE_TEXT],
                                              [float(round(self.alpha * 100)), f"Opacity {round(self.alpha * 100)}%"])
            for widget in (self.field, self.hue_slider, self.alpha_slider, self.gradient_bar):
                widget.queue_draw()
        finally:
            self._syncing = False


class ColorPopover(Gtk.Popover):
    """The picker in a kit popover surface, without an arrow, not modal."""

    def __init__(self, paint: dict[str, Any] | None, on_change: OnChange, **options: Any) -> None:
        super().__init__()
        install_css()
        self.add_css_class("canvas-color-picker")
        self.set_has_arrow(False)
        self.set_autohide(True)
        self.picker = ColorPicker(paint, on_change, on_close=self.popdown, **options)
        self.set_child(self.picker)
        self.connect("closed", self._closed)
        self.closed_callbacks: list[Callable[[], None]] = []

    def _closed(self, *_args: Any) -> None:
        self.picker._emit("end")
        for callback in self.closed_callbacks:
            callback()
        GLib.idle_add(self._unparent)

    def _unparent(self) -> bool:
        if self.get_parent() is not None:
            self.unparent()
        return GLib.SOURCE_REMOVE


def _popover_parent(anchor: Gtk.Widget, canvas: Any) -> tuple[Gtk.Widget, Gdk.Rectangle | None]:
    """Parent the popover to the canvas, pointing at the anchor.

    Inspector rows rebuild after an edit; a popover parented to a row would
    close with it. The canvas is never rebuilt and presents its popovers.
    """
    if (canvas is not None and isinstance(canvas, Gtk.Widget) and canvas is not anchor
            and canvas.get_root() is not None and canvas.get_root() is anchor.get_root()):
        found, bounds = anchor.compute_bounds(canvas)
        if found:
            rectangle = Gdk.Rectangle()
            rectangle.x, rectangle.y = int(round(bounds.get_x())), int(round(bounds.get_y()))
            rectangle.width = max(1, int(round(bounds.get_width())))
            rectangle.height = max(1, int(round(bounds.get_height())))
            return canvas, rectangle
    return anchor, None


def _find_canvas(widget: Gtk.Widget) -> Any:
    root = widget.get_root()
    return getattr(root, "canvas", None)


class ColorSheet(Gtk.Box):
    """The existing picker inside the shared phone action-center sheet.

    A live sheet stays in place when selection changes. It reserves canvas
    space rather than covering the object whose color is being edited.
    """
    def __init__(self,host,paint,on_change,**options):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        install_css();self.add_css_class('canvas-color-picker');self.add_css_class('phone')
        self.host=host;self.closed_callbacks=[];self._closed_once=False
        self.previous_panel=host._phone_panel
        self.picker=ColorPicker(paint,on_change,on_close=self.popdown,**options)
        header=self.picker.get_first_child()
        self.picker.remove(header);self.append(header)
        self.picker.field.set_size_request(112,100)
        for slider in (self.picker.hue_slider,self.picker.alpha_slider):slider.set_size_request(-1,44)
        self.picker.set_size_request(216,-1)
        values=self.picker.alpha_field.get_parent()
        sliders=self.picker.hue_slider.get_parent().get_parent()
        self.picker.remove(values);self.picker.remove(sliders);self.picker.remove(self.picker.field)
        color_row=Gtk.Box(spacing=8);color_row.append(self.picker.field)
        sliders.set_hexpand(True);color_row.append(sliders)
        self.picker.prepend(color_row);self.picker.prepend(values)
        self.picker.reorder_child_after(self.picker.gradient_row,None)
        self.scroller=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,max_content_height=200,child=self.picker)
        self.append(self.scroller)

    def popup(self):
        self.host._restore_phone_content()
        self.host._phone_color=self;self.host._phone_panel='color'
        self.host._phone_scroller=self.scroller
        if self.host._phone_landscape:
            from luma_appkit.creative_workspace import FloatingPanel
            from luma_appkit.structure_layers import LayerHost
            side=FloatingPanel('Color','palette',child=self,closable=False)
            side.set_halign(Gtk.Align.END);side.set_valign(Gtk.Align.START)
            side.set_margin_top(12);side.set_margin_end(12)
            self.host._phone_side=side
            LayerHost.window_host(self.host).add_overlay(side)
        else:self.host.phone_actions.grow('color',self)

    def set_autohide(self,_value):pass  # Canvas remains interactive for sampling/selection.

    def popdown(self):
        if self._closed_once:return
        self._closed_once=True;self.picker._emit('end')
        self.host._phone_color=None
        self.host._phone_panel=self.previous_panel if self.host.document.selection else None
        self.host._phone_action_signature=None
        for callback in self.closed_callbacks:callback()
        self.host._refresh_phone_actions()


def open_color_popover(anchor_widget: Gtk.Widget, paint: dict[str, Any] | None, on_change: OnChange, *,
                       canvas: Any = None, title: str = "Fill", allow_gradients: bool = True,
                       position: Gtk.PositionType = Gtk.PositionType.LEFT,
                       on_closed: Callable[[], None] | None = None, autohide: bool = True) -> ColorPopover:
    """Open Canvas's picker beside `anchor_widget`.

    `on_change(paint, phase)` receives every live change with phase "begin",
    "change" or "end"; one begin/end pair is one gesture (one undo step for
    consumers that wrap it in `Editor.begin_gesture`/`end_gesture`).
    """
    canvas = canvas if canvas is not None else _find_canvas(anchor_widget)
    host=anchor_widget.get_root()
    if getattr(host,"_phone_ui",False):
        if getattr(host,'_phone_color',None) is not None:host._phone_color.popdown()
        sheet=ColorSheet(host,paint,on_change,canvas=canvas,title=title,allow_gradients=allow_gradients)
        if on_closed is not None:sheet.closed_callbacks.append(on_closed)
        sheet.popup()
        return sheet
    popover = ColorPopover(paint, on_change, canvas=canvas, title=title, allow_gradients=allow_gradients)
    popover.set_autohide(autohide)
    parent, pointing = _popover_parent(anchor_widget, canvas)
    popover.set_parent(parent)
    if pointing is not None:
        popover.set_pointing_to(pointing)
    popover.set_position(position)

    def restore_focus() -> None:
        target = canvas if isinstance(canvas, Gtk.Widget) and canvas.get_mapped() else anchor_widget
        if target.get_mapped():
            target.grab_focus()
    popover.closed_callbacks.append(restore_focus)
    if on_closed is not None:
        popover.closed_callbacks.append(on_closed)
    popover.popup()
    return popover


# ── binding the picker to the selection or the current style ────────────

class PaintBinding:
    """Apply a picker's paint to the selection's fill or stroke, or to the current style.

    Each begin/end pair is one undo step labeled "Change fill" or "Change
    stroke". With nothing selected, the current style (the chip) changes:
    fill edits the shape fill, stroke edits the line stroke.
    """

    def __init__(self, canvas: Any, target: str, index: int | None = None) -> None:
        self.canvas = canvas
        self.target = target
        # Which paint of the stack (0 is the bottom); None is the bottom paint
        # as the rail and older callers know it.
        self.index = index
        from .model import paint_targets
        # A group stands for the layers inside it (it has no paint of its own).
        self.ids = [item.id for item in paint_targets(canvas.document, canvas.document.selection)
                    if not item.locked]
        self.label = "Change fill" if target == "fill" else "Change stroke"

    def paint(self) -> dict[str, Any] | None:
        document = self.canvas.document
        items = [document.object(object_id) for object_id in self.ids]
        items = [item for item in items if item is not None]
        if items and self.index is not None:
            from .paint import stack_paint
            value = stack_paint(items[0], self.target, self.index)
            if value is None:
                return None
            return {key: item for key, item in value.items() if key not in {"visible", "blend"}}
        if items:
            return object_paint(items[0], self.target)
        style = normalized_style(document.current_style)
        part = style["shape"] if self.target == "fill" else style["line"]
        gradient = part.get(f"{self.target}_gradient")
        if gradient:
            return copy.deepcopy(gradient)
        color = color_of(part.get(self.target))
        return solid(color) if color is not None else None

    def __call__(self, paint: dict[str, Any] | None, phase: Phase) -> None:
        editor = self.canvas.editor
        if phase == "begin":
            editor.begin_gesture(self.label)
            return
        if phase == "end":
            changed = editor.end_gesture()
            if changed:
                self.canvas.emit("document-changed", self.label)
            self.canvas.emit("style-changed")
            return
        document = self.canvas.document
        items = [document.object(object_id) for object_id in self.ids]
        items = [item for item in items if item is not None]
        if items and self.index is not None:
            editor.set_stack_paint([item.id for item in items], self.target, self.index, paint, label=self.label)
        elif items:
            editor.set_paint([item.id for item in items], self.target, paint, label=self.label)
        else:
            style = normalized_style(document.current_style)
            part = style["shape"] if self.target == "fill" else style["line"]
            if paint is None:
                part[self.target], part[f"{self.target}_gradient"] = None, None
            elif paint.get("type", "solid") == "solid":
                part[self.target] = to_hex(Color.from_dict(paint["color"]))
                part[f"{self.target}_gradient"] = None
            else:
                part[self.target] = to_hex(paint_color(paint) or Color(0, 0, 0))
                part[f"{self.target}_gradient"] = copy.deepcopy(paint)
            document.current_style = style
        self.canvas.queue_draw()
        self.canvas.emit("style-changed")


def open_paint_picker(anchor_widget: Gtk.Widget, canvas: Any, target: str = "fill", *,
                      position: Gtk.PositionType = Gtk.PositionType.LEFT, index: int | None = None) -> ColorPopover:
    """The picker bound to the selection's (or current style's) fill or stroke;
    `index` binds it to one paint of the stack."""
    binding = PaintBinding(canvas, target, index)
    popover = open_color_popover(anchor_widget, binding.paint(), binding, canvas=canvas,
                                 title="Fill" if target == "fill" else "Stroke", position=position, autohide=False)

    style_mode = not bool(binding.ids)
    closed_picker = False
    retargeting = False

    def gradient_begin() -> None:
        popover.picker._emit("begin")

    def gradient_change(paint: dict[str, Any]) -> None:
        popover.picker.paint = copy.deepcopy(paint)
        popover.picker._sync_all()
        popover.picker._emit("change")

    def gradient_end() -> None:
        popover.picker._emit("end")

    gradient_editor = {"target": target, "index": index, "begin": gradient_begin,
                       "change": gradient_change, "end": gradient_end}

    def refresh(*_args: Any) -> None:
        nonlocal retargeting
        if closed_picker or retargeting:
            return
        from .model import paint_targets
        ids = [item.id for item in paint_targets(canvas.document, canvas.document.selection)
               if not item.locked]
        retargeting = True
        try:
            if ids != binding.ids:
                # Finish the old layer's edit before rebinding. Never recreate or
                # reposition the popover when its inspector row is rebuilt.
                popover.picker._emit("end")
                binding.ids = ids
                if ids and binding.index is not None:
                    from .paint import stack_paint
                    if stack_paint(canvas.document.object(ids[0]), target, binding.index) is None:
                        binding.index = 0
                gradient_editor["index"] = binding.index
            enabled = bool(ids) or (style_mode and not canvas.document.selection)
            popover.picker.set_sensitive(enabled)
            if not popover.picker._in_gesture:
                popover.picker.set_paint(binding.paint() if enabled else None)
            if ids:
                canvas.gradient_editor = gradient_editor
            elif canvas.gradient_editor is gradient_editor:
                canvas.gradient_editor = None
            canvas.queue_draw()
        finally:
            retargeting = False

    handlers = [canvas.connect(signal, refresh) for signal in
                ("selection-changed", "document-changed", "style-changed")]
    refresh()

    def closed() -> None:
        nonlocal closed_picker
        closed_picker = True
        popover.picker._emit("end")
        for handler in handlers:
            canvas.disconnect(handler)
        if canvas.gradient_editor is gradient_editor:
            canvas.gradient_editor = None
            canvas.queue_draw()
    popover.closed_callbacks.append(closed)
    return popover


def _present_popovers(widget: Gtk.Widget) -> None:
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Popover) and child.get_visible():
            child.present()
        child = child.get_next_sibling()


# ── swatches for rows and the tool rail ──────────────────────────────────

class PaintSwatch(Gtk.Button):
    """A 20px paint swatch button (checks under alpha, gradient ramps)."""

    def __init__(self, paint: dict[str, Any] | None, on_activate: Callable[["PaintSwatch"], None], *,
                 label: str = "Color") -> None:
        super().__init__()
        install_css()
        self.paint = paint
        self.add_css_class("canvas-paint-swatch")
        self.set_size_request(20, 20)
        self.set_valign(Gtk.Align.CENTER)
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self._area = _SwatchFace(self)
        self.set_child(self._area)
        self.connect("clicked", lambda *_: on_activate(self))

    def set_paint(self, paint: dict[str, Any] | None) -> None:
        self.paint = paint
        self._area.queue_draw()

    def get_rgba(self) -> Gdk.RGBA:
        color = paint_color(self.paint) or Color(0, 0, 0, 0)
        return _rgba(color)


class _SwatchFace(Gtk.Widget):
    def __init__(self, owner: PaintSwatch) -> None:
        super().__init__()
        self.owner = owner
        self.set_size_request(20, 20)
        self.set_can_target(False)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        paint_preview(snapshot, _rect(0, 0, self.get_width(), self.get_height()), self.owner.paint, 4)


class StyleChip(Gtk.Widget):
    """Fill and stroke for new shapes, at the foot of the tool rail (spec 6.2)."""

    SIZE = 32

    def __init__(self, canvas_provider: Callable[[], Any]) -> None:
        super().__init__()
        install_css()
        self.canvas_provider = canvas_provider
        self.add_css_class("canvas-style-chip")
        self.set_size_request(self.SIZE, self.SIZE + 4)
        self.set_halign(Gtk.Align.CENTER)
        self.set_focusable(True)
        self.set_tooltip_text("Fill and stroke for new shapes · X to switch, D to reset")
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Fill and stroke"])
        self.set_cursor_from_name("pointer")
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self._connected: Any = None

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        _present_popovers(self)

    def canvas(self) -> Any:
        canvas = self.canvas_provider()
        if canvas is not None and canvas is not self._connected:
            for name in ("selection-changed", "document-changed", "style-changed"):
                canvas.connect(name, lambda *_: self.queue_draw())
            self._connected = canvas
        return canvas

    def do_map(self) -> None:
        Gtk.Widget.do_map(self)
        self.canvas()

    def _paints(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str, bool]:
        canvas = self.canvas()
        if canvas is None:
            return solid(Color(0.851, 0.851, 0.851)), None, "fill", False
        style = normalized_style(canvas.document.current_style)
        from .model import paint_targets
        selection = paint_targets(canvas.document, canvas.document.selection)
        front = style.get("front", "fill")
        if selection:
            item = selection[0]
            return object_paint(item, "fill"), (object_paint(item, "stroke") if item.stroke_width else None), front, True
        shape, line = style["shape"], style["line"]
        fill = copy.deepcopy(shape.get("fill_gradient")) or (solid(color_of(shape["fill"])) if shape.get("fill") else None)
        stroke = copy.deepcopy(line.get("stroke_gradient")) or (solid(color_of(line["stroke"])) if line.get("stroke") else None)
        return fill, stroke, front, False

    def get_rgba(self) -> Gdk.RGBA:
        fill, stroke, front, _ = self._paints()
        color = paint_color(fill if front == "fill" else stroke) or Color(0, 0, 0, 0)
        return _rgba(color)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        fill, stroke, front, editing = self._paints()
        width = self.get_width()
        ox = (width - self.SIZE) / 2
        fill_rect = _rect(ox + 3, 3, 19, 19)
        ring_center = (ox + 19.5, 19.5)

        def draw_fill() -> None:
            shadow = Gsk.Shadow()
            shadow.color = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.18)
            shadow.dx, shadow.dy, shadow.radius = 0, 1, 2
            snapshot.push_shadow([shadow])
            paint_preview(snapshot, fill_rect, fill, 4)
            snapshot.pop()

        def draw_stroke() -> None:
            builder = Gsk.PathBuilder.new()
            builder.add_circle(_point(*ring_center), 8.5)
            ring = builder.to_path()
            color = paint_color(stroke)
            if color is None:
                snapshot.append_stroke(ring, Gsk.Stroke.new(3.0), Gdk.RGBA(red=1, green=1, blue=1, alpha=1))
                slash = Gsk.PathBuilder.new()
                slash.move_to(ring_center[0] + 6.5, ring_center[1] - 6.5)
                slash.line_to(ring_center[0] - 6.5, ring_center[1] + 6.5)
                snapshot.append_stroke(slash.to_path(), Gsk.Stroke.new(1.5),
                                       Gdk.RGBA(red=0.9, green=0.22, blue=0.2, alpha=1))
            else:
                snapshot.append_stroke(ring, Gsk.Stroke.new(5.0), Gdk.RGBA(red=0, green=0, blue=0, alpha=0.14))
                snapshot.append_stroke(ring, Gsk.Stroke.new(3.0), _rgba(color, 1.0))
            outline = Gsk.PathBuilder.new()
            outline.add_circle(_point(*ring_center), 10.5)
            snapshot.append_stroke(outline.to_path(), Gsk.Stroke.new(1.0), Gdk.RGBA(red=0, green=0, blue=0, alpha=0.1))

        if front == "fill":
            draw_stroke()
            draw_fill()
        else:
            draw_fill()
            draw_stroke()
        if editing:
            accent = self.get_style_context().lookup_color("luma_accent")[1]
            snapshot.append_color(accent, _rect(ox + 8, self.SIZE + 1, self.SIZE - 16, 2))

    def _target_at(self, x: float, y: float) -> str:
        fill, stroke, front, _ = self._paints()
        ox = (self.get_width() - self.SIZE) / 2
        in_fill = ox + 3 <= x <= ox + 22 and 3 <= y <= 22
        in_ring = math.hypot(x - (ox + 19.5), y - 19.5) <= 11
        if in_fill and in_ring:
            return front
        if in_ring:
            return "stroke"
        return "fill"

    def _clicked(self, _gesture: Gtk.GestureClick, _count: int, x: float, y: float) -> None:
        self.open(self._target_at(x, y))

    def open(self, target: str) -> ColorPopover | None:
        canvas = self.canvas()
        if canvas is None:
            return None
        return open_paint_picker(self, canvas, target, position=Gtk.PositionType.RIGHT)

    def _key(self, _controller: Any, keyval: int, _keycode: int, _state: Any) -> bool:
        if Gdk.keyval_name(keyval) in {"Return", "KP_Enter", "space"}:
            canvas = self.canvas()
            front = normalized_style(canvas.document.current_style).get("front", "fill") if canvas else "fill"
            popover = self.open(front)
            if popover is not None and popover.picker.fields:
                popover.picker.fields[0].entry.grab_focus()
            return True
        return False


# ── screen color through the desktop portal ──────────────────────────────

def pick_screen_color(widget: Gtk.Widget | None, callback: Callable[[Color | None], None]) -> None:
    """org.freedesktop.portal.Screenshot.PickColor; calls back with None when unavailable."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        callback(None)
        return
    token = f"luma_canvas_{secrets.token_hex(6)}"
    sender = (bus.get_unique_name() or ":0").lstrip(":").replace(".", "_")
    handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    state: dict[str, Any] = {"done": False}

    def finish(color: Color | None) -> None:
        if state["done"]:
            return
        state["done"] = True
        if state.get("subscription"):
            bus.signal_unsubscribe(state["subscription"])
        callback(color)

    def response(_connection, _sender, _path, _interface, _signal, parameters) -> None:
        code, results = parameters.unpack()
        color = results.get("color") if code == 0 else None
        finish(Color(*color, 1.0) if color else None)

    state["subscription"] = bus.signal_subscribe(
        "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response", handle,
        None, Gio.DBusSignalFlags.NONE, response)

    def called(connection, result) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error:
            finish(None)

    bus.call("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
             "org.freedesktop.portal.Screenshot", "PickColor",
             GLib.Variant("(sa{sv})", ("", {"handle_token": GLib.Variant("s", token)})),
             GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 30000, None, called)
