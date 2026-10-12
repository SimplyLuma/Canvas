# SPDX-License-Identifier: MPL-2.0
"""The inspector: properties of the selection, updated in place.

CANVAS-SPEC 11.2 and 14.6. Sections are built once for a set of selected
kinds and then only their values change: a drag on the canvas, an undo or a
scrub updates numbers without recreating a widget, and a field that has focus
is never overwritten, so typing is never interrupted and focus never jumps.
Sections are rebuilt only when the kinds in the selection change.

The inspector edits through its host (the window), which owns the editor,
history labels and autosave.

TODO(kit-request canvas-03-creative-inspector): retain these callbacks while
composing PropertySection, PropertyNumber, PropertyColor, PropertyChoice and
AlignmentActions when the shared v70 fields arrive.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Protocol, Sequence

from gi.repository import Gdk, GLib, Gtk, Pango
from luma_appkit import IconButton, InlineRenameField, NumericField

from .layers import KIND_ICONS, KIND_NAMES, layer_icon
from . import paint as paints
from .model import Document, LayoutObject, paint_targets
from .vector import corner_radii

BLEND_MODES = (
    ("normal", "Normal"), ("darken", "Darken"), ("multiply", "Multiply"), ("color-burn", "Color burn"),
    ("lighten", "Lighten"), ("screen", "Screen"), ("color-dodge", "Color dodge"), ("overlay", "Overlay"),
    ("soft-light", "Soft light"), ("hard-light", "Hard light"), ("difference", "Difference"),
    ("exclusion", "Exclusion"), ("hue", "Hue"), ("saturation", "Saturation"), ("color", "Color"),
    ("luminosity", "Luminosity"),
)
STROKE_ALIGNS = (("inside", "Inside"), ("center", "Center"), ("outside", "Outside"))
STROKE_CAPS = (("butt", "None"), ("round", "Round"), ("square", "Square"))
STROKE_JOINS = (("miter", "Miter"), ("round", "Round"), ("bevel", "Bevel"))
EFFECT_KINDS = (("drop_shadow", "Drop shadow"), ("inner_shadow", "Inner shadow"),
                ("layer_blur", "Layer blur"), ("background_blur", "Background blur"))
RADIUS_KINDS = {"rectangle", "frame", "image", "component", "instance", "page", "section"}
CORNERS = (("top-left", "Top left"), ("top-right", "Top right"),
           ("bottom-right", "Bottom right"), ("bottom-left", "Bottom left"))


class Host(Protocol):
    context: Any

    @property
    def document(self) -> Document: ...
    def edit(self, label: str, change: Callable[[], None], *, coalesce: bool = False) -> bool: ...
    def rename_layer(self, object_id: str, name: str) -> bool: ...
    def align(self, action: str) -> None: ...
    def open_layer_menu(self, button: Gtk.Widget) -> None: ...
    def export_selection(self, fmt: str, scale: int) -> None: ...
    def export_layers(self, object_ids: list[str] | None) -> None: ...
    def export_frames(self) -> None: ...
    def remember(self, key: str, value: object) -> None: ...
    def recall(self, key: str, default: object = None) -> object: ...
    def supports(self, capability: str) -> bool: ...


# ── color helpers (document data, not UI palette) ───────────────────────

def parse_color(text: str | None) -> Gdk.RGBA | None:
    if not text:
        return None
    rgba = Gdk.RGBA()
    value = text.strip()
    if value and not value.startswith(("#", "rgb", "hsl")) and all(c in "0123456789abcdefABCDEF" for c in value):
        value = "#" + value
    return rgba if rgba.parse(value) else None


def hex_of(rgba: Gdk.RGBA, *, alpha: bool = False) -> str:
    channels = [round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)]
    if alpha and rgba.alpha < 0.9995:
        channels.append(round(rgba.alpha * 255))
    return "#" + "".join(f"{max(0, min(255, value)):02x}" for value in channels)


def paint_from_css(text: str | None) -> dict[str, Any]:
    rgba = parse_color(text) or parse_color("#d9d9d9")
    return {"type": "solid", "color": {"r": round(rgba.red, 4), "g": round(rgba.green, 4),
                                        "b": round(rgba.blue, 4), "a": round(rgba.alpha, 4)},
            "opacity": 1.0, "visible": True, "blend": "normal", "style_id": None}


def css_from_paint(paint: dict[str, Any]) -> str:
    color = paint.get("color", {})
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue = (float(color.get(key, 0)) for key in ("r", "g", "b"))
    rgba.alpha = float(color.get("a", 1)) * float(paint.get("opacity", 1))
    return hex_of(rgba, alpha=True)


def _mixed(values: Iterable[Any]) -> tuple[bool, Any]:
    values = list(values)
    if not values:
        return False, None
    first = values[0]
    return any(value != first for value in values[1:]), first


def _focused(widget: Gtk.Widget) -> bool:
    root = widget.get_root()
    focus = root.get_focus() if root is not None else None
    while focus is not None:
        if focus is widget:
            return True
        focus = focus.get_parent()
    return False


def _set_number(field: NumericField, values: Sequence[float]) -> None:
    if _focused(field):
        return
    mixed, first = _mixed(round(value, 2) for value in values)
    if first is None:
        return
    if mixed:
        field.set_mixed()
    else:
        field.set_value(first)


def _dropdown(choices: Sequence[tuple[Any, str]], on_change: Callable[[Any], None],
              label: str) -> Gtk.DropDown:
    dropdown = Gtk.DropDown.new_from_strings([name for _value, name in choices])
    dropdown.add_css_class("luma-dropdown")
    dropdown.add_css_class("small")
    dropdown.add_css_class("canvas-dropdown")
    dropdown.update_property([Gtk.AccessibleProperty.LABEL], [label])
    dropdown.set_tooltip_text(label)
    dropdown.canvas_choices = [value for value, _name in choices]
    dropdown.canvas_quiet = False

    def changed(widget, _param) -> None:
        if widget.canvas_quiet:
            return
        index = widget.get_selected()
        if 0 <= index < len(widget.canvas_choices):
            on_change(widget.canvas_choices[index])

    dropdown.connect("notify::selected", changed)
    return dropdown


def _select(dropdown: Gtk.DropDown, value: Any) -> None:
    if value not in dropdown.canvas_choices or _focused(dropdown):
        return
    index = dropdown.canvas_choices.index(value)
    if dropdown.get_selected() != index:
        dropdown.canvas_quiet = True
        dropdown.set_selected(index)
        dropdown.canvas_quiet = False


def _icon_toggle(icon: str, label: str) -> Gtk.ToggleButton:
    button = Gtk.ToggleButton()
    button.set_child(Gtk.Image(icon_name=icon, pixel_size=14))
    button.add_css_class("luma-icon-button")
    button.add_css_class("quiet")
    button.add_css_class("canvas-icon-toggle")
    button.set_tooltip_text(label)
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    button.set_valign(Gtk.Align.CENTER)
    button.canvas_quiet = False
    return button


def _set_toggle(button: Gtk.ToggleButton, active: bool) -> None:
    if button.get_active() != active:
        button.canvas_quiet = True
        button.set_active(active)
        button.canvas_quiet = False


def _number(label: str, *, unit: str = "", minimum: float | None = None, maximum: float | None = None,
            step: float = 1.0, digits: int = 0, tooltip: str | None = None) -> NumericField:
    field = NumericField(label, unit=unit, step=step, minimum=minimum, maximum=maximum, digits=digits)
    field.add_css_class("canvas-number")
    field.set_hexpand(True)
    field.entry.add_css_class("numeric")
    if tooltip:
        field.label.set_tooltip_text(tooltip)
    field.canvas_apply_each = None
    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", lambda _c, keyval, _code, _state: _mixed_math(field, keyval))
    field.entry.add_controller(keys)
    return field


def _mixed_math(field: NumericField, keyval: int) -> bool:
    """`Mixed*2` or `Mixed+10` applies the arithmetic to each layer's own value
    (CANVAS-SPEC 11.2, AT-L7)."""
    if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
        return False
    text = field.entry.get_text().strip()
    if "mixed" not in text.casefold() or text.casefold() == "mixed" or field.canvas_apply_each is None:
        return False
    from luma_appkit import evaluate_number

    def per_layer(current: float) -> float | None:
        return evaluate_number(text.casefold().replace("mixed", f"({current!r})"))

    if per_layer(1.0) is None:
        return False
    field.canvas_apply_each(per_layer)
    return keyval != Gdk.KEY_Tab


def _glyph_field(icon: str, label: str, *, unit: str = "", minimum: float | None = None,
                 maximum: float | None = None, step: float = 1.0, digits: int = 0) -> NumericField:
    """A number whose leading glyph is its scrub handle (Figma's corner radius,
    opacity and stroke weight fields)."""
    field = _number("", unit=unit, minimum=minimum, maximum=maximum, step=step, digits=digits)
    field.label.set_visible(False)
    field.update_property([Gtk.AccessibleProperty.LABEL], [label])
    glyph = Gtk.Image(icon_name=icon, pixel_size=14)
    glyph.add_css_class("canvas-field-glyph")
    glyph.set_tooltip_text(f"Drag to change {label.lower()}")
    glyph.set_cursor_from_name("ew-resize")
    if hasattr(field, "_drag_begin") and hasattr(field, "_drag_update"):
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", field._drag_begin)
        drag.connect("drag-update", field._drag_update)
        glyph.add_controller(drag)
    field.prepend(glyph)
    return field


class Swatch(Gtk.Button):
    """A 20px paint chip with a checkerboard under alpha."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.add_css_class("canvas-swatch-button")
        self.set_valign(Gtk.Align.CENTER)
        self.set_tooltip_text(label)
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.rgba: Gdk.RGBA | None = None
        area = Gtk.DrawingArea(content_width=20, content_height=20)
        area.set_draw_func(self._draw)
        self.area = area
        self.set_child(area)

    def set_color(self, text: str | None) -> None:
        self.rgba = parse_color(text)
        self.area.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        radius = 4
        cr.new_sub_path()
        cr.arc(width - radius, radius, radius, -math.pi / 2, 0)
        cr.arc(width - radius, height - radius, radius, 0, math.pi / 2)
        cr.arc(radius, height - radius, radius, math.pi / 2, math.pi)
        cr.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.clip()
        # Checkerboard: an instrument drawn under transparent document color.
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.set_source_rgb(0.8, 0.8, 0.8)
        for row in range(0, height, 5):
            for column in range(0, width, 5):
                if (row // 5 + column // 5) % 2:
                    cr.rectangle(column, row, 5, 5)
        cr.fill()
        if self.rgba is not None:
            cr.set_source_rgba(self.rgba.red, self.rgba.green, self.rgba.blue, self.rgba.alpha)
            cr.paint()
        ring = self.get_color()
        cr.set_source_rgba(ring.red, ring.green, ring.blue, 0.14)
        cr.set_line_width(2)
        cr.new_sub_path()
        cr.arc(width - radius, radius, radius, -math.pi / 2, 0)
        cr.arc(width - radius, height - radius, radius, 0, math.pi / 2)
        cr.arc(radius, height - radius, radius, math.pi / 2, math.pi)
        cr.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.stroke()


# ── sections ────────────────────────────────────────────────────────────

class Section:
    """A titled, collapsible block. Subclasses fill `body` once and change
    only values in `update`."""

    title = ""
    key = ""

    def __init__(self, inspector: "Inspector") -> None:
        self.inspector = inspector
        self.host = inspector.host
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.add_css_class("canvas-section")
        self.widget.add_css_class(f"canvas-section-{self.key or 'plain'}")
        self.head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.head.add_css_class("canvas-section-head")
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.body.add_css_class("canvas-section-body")
        if self.title:
            title = Gtk.Button(hexpand=True)
            title.add_css_class("canvas-section-title")
            caption = Gtk.Label(label=self.title, xalign=0)
            title.set_child(caption)
            title.set_tooltip_text(f"Show or hide {self.title.lower()}")
            title.connect("clicked", lambda *_: self.set_collapsed(not self.collapsed))
            self.head.append(title)
            self.widget.append(self.head)
        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.NONE, reveal_child=True)
        self.revealer.set_child(self.body)
        self.widget.append(self.revealer)
        self.collapsed = self.key in inspector.collapsed
        self.revealer.set_reveal_child(not self.collapsed)
        if self.collapsed:
            self.widget.add_css_class("collapsed")

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.revealer.set_reveal_child(not collapsed)
        (self.widget.add_css_class if collapsed else self.widget.remove_css_class)("collapsed")
        self.inspector.remember_collapsed(self.key, collapsed)

    def add_action(self, icon: str, label: str, callback: Callable[[], None]) -> Gtk.Button:
        button = IconButton(icon, label, context=self.host.context, quiet=True)
        button.add_css_class("canvas-section-action")
        button.connect("clicked", lambda *_: callback())
        self.head.append(button)
        return button

    def grid(self) -> Gtk.Grid:
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=False)
        grid.add_css_class("canvas-field-grid")
        self.body.append(grid)
        return grid

    def update(self, selection: list[LayoutObject]) -> None:
        pass

    # Editing shortcuts for subclasses.

    # Fill and Stroke edit the layers inside a selected group (Figma).
    paint_section = False

    @property
    def targets(self) -> list[LayoutObject]:
        document = self.host.document
        return paint_targets(document, document.selection) if self.paint_section else document.selection

    @property
    def ids(self) -> list[str]:
        if self.paint_section:
            return [item.id for item in self.targets]
        return list(self.host.document.selected_ids)

    def edit(self, label: str, change: Callable[[], None], *, coalesce: bool = False) -> None:
        self.host.edit(label, change, coalesce=coalesce)


class HeaderSection(Section):
    key = "header"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.add_css_class("canvas-inspector-header")
        self.glyph = Gtk.Image(pixel_size=14)
        self.glyph.add_css_class("canvas-header-glyph")
        row.append(self.glyph)
        self.name_stack = Gtk.Stack(hexpand=True, hhomogeneous=False, vhomogeneous=False)
        self.name = Gtk.Label(xalign=0)
        self.name.add_css_class("canvas-header-name")
        self.name.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_stack.add_named(self.name, "label")
        double = Gtk.GestureClick()
        double.connect("pressed", lambda _g, count, _x, _y: self.begin_rename() if count == 2 else None)
        self.name.add_controller(double)
        row.append(self.name_stack)
        more = IconButton("luma-canvas-more-symbolic", "More actions", context=self.host.context, quiet=True)
        more.add_css_class("canvas-section-action")
        more.connect("clicked", lambda button: self.host.open_layer_menu(button))
        row.append(more)
        self.body.append(row)
        self.widget.remove_css_class("canvas-section")
        self.widget.add_css_class("canvas-section-header-block")

    def update(self, selection: list[LayoutObject]) -> None:
        if self.name_stack.get_visible_child_name() == "rename":
            return
        if len(selection) == 1:
            item = selection[0]
            self.glyph.set_from_icon_name(layer_icon(item, item.kind))
            self.name.set_label(item.name)
            self.name.set_tooltip_text(KIND_NAMES.get(item.kind, item.kind.title()))
        else:
            self.glyph.set_from_icon_name("luma-canvas-group-symbolic")
            self.name.set_label(f"{len(selection)} layers")
            self.name.set_tooltip_text(None)

    def begin_rename(self) -> None:
        selection = self.host.document.selection
        if len(selection) != 1:
            return
        object_id = selection[0].id

        def finish(text: str | None) -> bool:
            if text is not None and text.strip() and not self.host.rename_layer(object_id, text.strip()):
                return False
            self.name_stack.set_visible_child_name("label")
            GLib.idle_add(self._drop_field)
            return True

        field = InlineRenameField(self.name.get_label(), commit=finish, cancel=lambda: finish(None))
        field.add_css_class("canvas-header-rename")
        self._drop_field()
        self.name_stack.add_named(field, "rename")
        self.name_stack.set_visible_child_name("rename")
        field.begin()

    def _drop_field(self) -> bool:
        field = self.name_stack.get_child_by_name("rename")
        if field is not None and self.name_stack.get_visible_child_name() != "rename":
            self.name_stack.remove(field)
        return False


ALIGN_ACTIONS = (
    ("left", "Align left", "Alt+A", "luma-canvas-align-left-symbolic"),
    ("center-x", "Align horizontal centers", "Alt+H", "luma-canvas-align-center-horizontal-symbolic"),
    ("right", "Align right", "Alt+D", "luma-canvas-align-right-symbolic"),
    ("top", "Align top", "Alt+W", "luma-canvas-align-top-symbolic"),
    ("center-y", "Align vertical centers", "Alt+V", "luma-canvas-align-middle-symbolic"),
    ("bottom", "Align bottom", "Alt+S", "luma-canvas-align-bottom-symbolic"),
    ("distribute-x", "Distribute horizontal spacing", "Alt+Shift+H", "luma-canvas-distribute-horizontal-symbolic"),
    ("distribute-y", "Distribute vertical spacing", "Alt+Shift+V", "luma-canvas-distribute-vertical-symbolic"),
)


class AlignSection(Section):
    """Six alignments and two distributions at 28px, as in Figma. One layer
    aligns to its parent frame; distribution needs three."""

    key = "align"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, homogeneous=True)
        row.add_css_class("canvas-align-row")
        self.buttons: dict[str, Gtk.Button] = {}
        for action, label, shortcut, icon in ALIGN_ACTIONS:
            button = Gtk.Button()
            button.set_child(Gtk.Image(icon_name=icon, pixel_size=16))
            button.add_css_class("canvas-align-button")
            button.set_tooltip_text(f"{label} · {shortcut}")
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect("clicked", lambda _b, value=action: self.host.align(value))
            row.append(button)
            self.buttons[action] = button
        self.body.append(row)
        self.widget.add_css_class("canvas-section-untitled")

    def update(self, selection: list[LayoutObject]) -> None:
        for action, button in self.buttons.items():
            button.set_sensitive(len(selection) > 2 if action.startswith("distribute") else bool(selection))


class PositionSection(Section):
    title, key = "Position", "position"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        grid = self.grid()
        self.x = _number("X", tooltip="Drag to change X")
        self.y = _number("Y", tooltip="Drag to change Y")
        self.rotation = _number("∠", unit="°", minimum=-180, maximum=180, tooltip="Drag to change rotation")
        self.rotation.update_property([Gtk.AccessibleProperty.LABEL], ["Rotation"])
        self.x.connect("value-changed", lambda _f, value: self._move("x", value))
        self.y.connect("value-changed", lambda _f, value: self._move("y", value))
        self.x.canvas_apply_each = lambda fn: self._move_each("x", fn)
        self.y.canvas_apply_each = lambda fn: self._move_each("y", fn)
        self.rotation.connect("value-changed", lambda _f, value: self._rotate(value))
        grid.attach(self.x, 0, 0, 1, 1)
        grid.attach(self.y, 1, 0, 1, 1)
        grid.attach(self.rotation, 0, 1, 1, 1)
        flips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, halign=Gtk.Align.START)
        self.flip_h = _icon_toggle("luma-canvas-flip-horizontal-symbolic", "Flip horizontal")
        self.flip_v = _icon_toggle("luma-canvas-flip-vertical-symbolic", "Flip vertical")
        for button, key in ((self.flip_h, "flip_h"), (self.flip_v, "flip_v")):
            button.connect("toggled", lambda control, prop=key: None if control.canvas_quiet
                           else self._flip(prop, control.get_active()))
            flips.append(button)
        grid.attach(flips, 1, 1, 1, 1)
        for column in (0, 1):
            grid.get_child_at(column, 0).set_size_request(0, -1)

    def _origin(self, item: LayoutObject, key: str) -> float:
        # Figma: X and Y are relative to the nearest frame; groups and booleans
        # are not coordinate spaces of their own.
        for parent in self.host.document.ancestors(item.id):
            if parent.kind in {"frame", "page", "component", "section", "instance"}:
                return getattr(parent, key)
        return 0.0

    def update(self, selection: list[LayoutObject]) -> None:
        _set_number(self.x, [item.x - self._origin(item, "x") for item in selection])
        _set_number(self.y, [item.y - self._origin(item, "y") for item in selection])
        _set_number(self.rotation, [item.rotation for item in selection])
        _set_toggle(self.flip_h, all(item.flip_h for item in selection))
        _set_toggle(self.flip_v, all(item.flip_v for item in selection))

    def _move(self, key: str, value: float) -> None:
        document = self.host.document

        def change() -> None:
            for item in document.selection:
                if not item.locked:
                    self.host.editor.set_geometry(item.id, **{key: value + self._origin(item, key)})
        self.edit(f"Change {key.upper()}", change, coalesce=True)

    def _move_each(self, key: str, compute) -> None:
        def change() -> None:
            for item in self.host.document.selection:
                if item.locked:
                    continue
                origin = self._origin(item, key)
                value = compute(getattr(item, key) - origin)
                if value is not None:
                    self.host.editor.set_geometry(item.id, **{key: value + origin})
        self.edit(f"Change {key.upper()}", change)

    def _rotate(self, value: float) -> None:
        def change() -> None:
            for item in self.host.document.selection:
                self.host.editor.set_geometry(item.id, rotation=value)
        self.edit("Rotate", change, coalesce=True)

    def _flip(self, key: str, active: bool) -> None:
        self.edit("Flip horizontal" if key == "flip_h" else "Flip vertical",
                  lambda: self.host.editor.set_property(self.ids, key, active))


class LayoutSection(Section):
    title, key = "Layout", "layout"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        grid = self.grid()
        self.width = _number("W", minimum=0.01, tooltip="Drag to change width")
        self.height = _number("H", minimum=0.01, tooltip="Drag to change height")
        self.width.connect("value-changed", lambda _f, value: self._resize("width", value))
        self.height.connect("value-changed", lambda _f, value: self._resize("height", value))
        self.width.canvas_apply_each = lambda fn: self._resize_each("width", fn)
        self.height.canvas_apply_each = lambda fn: self._resize_each("height", fn)
        grid.attach(self.width, 0, 0, 1, 1)
        grid.attach(self.height, 1, 0, 1, 1)
        self.lock = _icon_toggle("luma-canvas-link-symbolic", "Keep proportions")
        self.lock.connect("toggled", lambda control: None if control.canvas_quiet
                          else self._set_lock(control.get_active()))
        grid.attach(self.lock, 2, 0, 1, 1)
        # Frames only: Figma's "Clip content" (children outside are hidden).
        self.clip = Gtk.CheckButton(label="Clip content")
        self.clip.add_css_class("canvas-clip-content")
        self.clip.canvas_quiet = False
        self.clip.connect("toggled", lambda control: None if control.canvas_quiet
                          else self._set_clip(control.get_active()))
        grid.attach(self.clip, 0, 1, 2, 1)

    def update(self, selection: list[LayoutObject]) -> None:
        _set_number(self.width, [item.width for item in selection])
        _set_number(self.height, [item.height for item in selection])
        _set_toggle(self.lock, all(item.metadata.get("lock_proportions") for item in selection))
        frames = bool(selection) and all(item.kind in {"frame", "page", "component", "section"} for item in selection)
        self.clip.set_visible(frames)
        if frames:
            self.clip.canvas_quiet = True
            self.clip.set_active(all(getattr(item, "clip_content", True) for item in selection))
            self.clip.canvas_quiet = False

    def _set_clip(self, active: bool) -> None:
        def change() -> None:
            ids = [item.id for item in self.host.document.selection]
            self.host.editor.set_property(ids, "clip_content", active)
        self.edit("Clip content" if active else "Show overflow", change)

    def _resize(self, key: str, value: float) -> None:
        def change() -> None:
            for item in self.host.document.selection:
                values = {key: value}
                if item.metadata.get("lock_proportions") and item.width and item.height:
                    ratio = item.height / item.width
                    if key == "width":
                        values["height"] = value * ratio
                    else:
                        values["width"] = value / ratio
                self.host.editor.set_geometry(item.id, **values)
        self.edit("Resize", change, coalesce=True)

    def _resize_each(self, key: str, compute) -> None:
        def change() -> None:
            for item in self.host.document.selection:
                value = compute(getattr(item, key))
                if value is not None:
                    self.host.editor.set_geometry(item.id, **{key: max(0.01, value)})
        self.edit("Resize", change)

    def _set_lock(self, active: bool) -> None:
        def change() -> None:
            for item in self.host.document.selection:
                metadata = dict(item.metadata)
                metadata["lock_proportions"] = active
                self.host.editor.set_property([item.id], "metadata", metadata)
        self.edit("Keep proportions", change)


class AppearanceSection(Section):
    title, key = "Appearance", "appearance"

    def __init__(self, inspector: "Inspector", *, radius: bool) -> None:
        super().__init__(inspector)
        grid = self.grid()
        self.opacity = _glyph_field("luma-canvas-blend-symbolic", "Opacity", unit="%", minimum=0, maximum=100)
        self.opacity.connect("value-changed", lambda _f, value: self.edit(
            "Change opacity", lambda: self.host.editor.set_property(self.ids, "opacity", max(0.0, min(1.0, value / 100))),
            coalesce=True))
        grid.attach(self.opacity, 0, 0, 1, 1)
        self.blend = _dropdown(BLEND_MODES, self._set_blend, "Blend mode")
        grid.attach(self.blend, 1, 0, 1, 1)
        self.radius = None
        if radius:
            self.radius = _glyph_field("luma-canvas-radius-symbolic", "Corner radius", minimum=0)
            self.radius.connect("value-changed", lambda _f, value: self._set_radius(None, value))
            grid.attach(self.radius, 0, 1, 1, 1)
            self.independent = _icon_toggle("luma-canvas-corners-symbolic", "Independent corners")
            self.independent.set_halign(Gtk.Align.START)
            self.independent.connect("toggled", lambda control: None if control.canvas_quiet
                                     else self.corners.set_visible(control.get_active()))
            grid.attach(self.independent, 1, 1, 1, 1)
            self.corners = Gtk.Grid(column_spacing=8, row_spacing=8)
            self.corner_fields = {}
            for index, (corner, label) in enumerate(CORNERS):
                field = _glyph_field("luma-canvas-radius-symbolic", f"{label} radius", minimum=0)
                field.connect("value-changed", lambda _f, value, name=corner: self._set_radius(name, value))
                self.corners.attach(field, index % 2, index // 2, 1, 1)
                self.corner_fields[corner] = field
            self.corners.set_visible(False)
            self.body.append(self.corners)
            # Figma's corner smoothing: the corners become squircles.
            self.smoothing = _glyph_field("luma-canvas-smoothing-symbolic", "Corner smoothing",
                                          unit="%", minimum=0, maximum=100)
            self.smoothing.connect("value-changed", lambda _f, value: self._set_smoothing(value))
            grid.attach(self.smoothing, 0, 2, 1, 1)

    def update(self, selection: list[LayoutObject]) -> None:
        _set_number(self.opacity, [item.opacity * 100 for item in selection])
        mixed, blend = _mixed(item.metadata.get("blend", "normal") for item in selection)
        if not mixed and blend is not None:
            _select(self.blend, blend)
        if self.radius is None:
            return
        radii = [corner_radii(item) for item in selection]
        uniform = all(len(set(values)) == 1 for values in radii)
        if uniform:
            _set_number(self.radius, [values[0] for values in radii])
        elif not _focused(self.radius):
            self.radius.set_mixed()
        if not uniform and not self.independent.get_active():
            _set_toggle(self.independent, True)
            self.corners.set_visible(True)
        for index, (corner, _label) in enumerate(CORNERS):
            _set_number(self.corner_fields[corner], [values[index] for values in radii])
        _set_number(self.smoothing, [float(item.metadata.get("corner_smoothing", 0.0) or 0.0) * 100
                                     for item in selection])

    def _set_blend(self, value: str) -> None:
        def change() -> None:
            for item in self.host.document.selection:
                metadata = dict(item.metadata)
                if value == "normal":
                    metadata.pop("blend", None)
                else:
                    metadata["blend"] = value
                self.host.editor.set_property([item.id], "metadata", metadata)
        self.edit("Change blend mode", change)

    def _set_smoothing(self, value: float) -> None:
        amount = max(0.0, min(1.0, value / 100))

        def change() -> None:
            for item in self.host.document.selection:
                if item.kind not in RADIUS_KINDS:
                    continue
                metadata = dict(item.metadata)
                if amount <= 0.0005:
                    metadata.pop("corner_smoothing", None)
                else:
                    metadata["corner_smoothing"] = round(amount, 4)
                self.host.editor.set_property([item.id], "metadata", metadata)
        self.edit("Change corner smoothing", change, coalesce=True)

    def _set_radius(self, corner: str | None, value: float) -> None:
        order = [name for name, _label in CORNERS]

        def change() -> None:
            for item in self.host.document.selection:
                if item.kind not in RADIUS_KINDS:
                    continue
                if corner is None:
                    self.host.editor.set_property([item.id], "radius", max(0.0, value))
                    self.host.editor.set_property([item.id], "corner_radii", [])
                else:
                    radii = list(corner_radii(item))
                    radii[order.index(corner)] = max(0.0, value)
                    self.host.editor.set_property([item.id], "corner_radii", radii)
        self.edit("Change corner radius", change, coalesce=True)


class PaintRow(Gtk.Box):
    """`[swatch] hex  opacity %  eye  remove` for one fill or stroke."""

    def __init__(self, section: Section, noun: str, *, on_color: Callable[[str], None],
                 on_visible: Callable[[bool], None], on_remove: Callable[[], None],
                 target: str | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.add_css_class("canvas-paint-row")
        self.section, self.noun, self.target = section, noun, target
        self.on_color, self.on_visible, self.on_remove = on_color, on_visible, on_remove
        self.color: str | None = None
        try:
            from .color_picker import PaintSwatch
            self.swatch = PaintSwatch(None, lambda _widget: self._open_picker(), label=f"{noun} color")
        except ImportError:
            self.swatch = Swatch(f"{noun} color")
            self.swatch.connect("clicked", lambda *_: self._open_picker())
        self.append(self.swatch)
        self.hex = Gtk.Entry(width_chars=5, max_width_chars=9, hexpand=True)
        self.hex.add_css_class("canvas-hex")
        self.hex.update_property([Gtk.AccessibleProperty.LABEL], [f"{noun} hex value"])
        self.hex.connect("activate", lambda *_: self._commit_hex())
        leave = Gtk.EventControllerFocus()
        leave.connect("leave", lambda *_: self._commit_hex())
        self.hex.add_controller(leave)
        self.append(self.hex)
        self.opacity = _number("", unit="%", minimum=0, maximum=100, tooltip=f"{noun} opacity")
        self.opacity.label.set_visible(False)
        self.opacity.add_css_class("canvas-paint-opacity")
        self.opacity.set_hexpand(False)
        self.opacity.set_size_request(56, -1)
        self.opacity.update_property([Gtk.AccessibleProperty.LABEL], [f"{noun} opacity"])
        self.opacity.connect("value-changed", lambda _f, value: self._set_alpha(value / 100))
        self.append(self.opacity)
        self.eye = _icon_toggle("luma-canvas-eye-symbolic", f"Hide {noun.lower()}")
        self.eye.connect("toggled", lambda control: None if control.canvas_quiet else self.on_visible(not control.get_active()))
        self.append(self.eye)
        remove = IconButton("luma-canvas-minus-symbolic", f"Remove {noun.lower()}", context=section.host.context, quiet=True)
        remove.add_css_class("canvas-row-action")
        remove.connect("clicked", lambda *_: self.on_remove())
        self.append(remove)

    def set_paint(self, color: str | None, visible: bool) -> None:
        self.color = color
        rgba = parse_color(color)
        if hasattr(self.swatch, "set_paint"):
            self.swatch.set_paint(paint_from_css(color) if color else None)
        else:
            self.swatch.set_color(color)
        if not _focused(self.hex):
            self.hex.set_text(hex_of(rgba)[1:].upper() if rgba else "")
        if rgba is not None:
            _set_number(self.opacity, [round(rgba.alpha * 100)])
        hidden = not visible
        _set_toggle(self.eye, hidden)
        self.eye.get_child().set_from_icon_name("luma-canvas-eye-off-symbolic" if hidden else "luma-canvas-eye-symbolic")
        label = f"Show {self.noun.lower()}" if hidden else f"Hide {self.noun.lower()}"
        self.eye.set_tooltip_text(label)
        (self.add_css_class if hidden else self.remove_css_class)("hidden-paint")

    def _commit_hex(self) -> None:
        rgba = parse_color(self.hex.get_text())
        current = parse_color(self.color)
        if rgba is None:
            self.hex.set_text(hex_of(current)[1:].upper() if current else "")
            return
        if current is not None:
            rgba.alpha = current.alpha
        value = hex_of(rgba, alpha=True)
        if current is None or value != hex_of(current, alpha=True):
            self.on_color(value)

    def _set_alpha(self, alpha: float) -> None:
        rgba = parse_color(self.color)
        if rgba is None:
            return
        rgba.alpha = max(0.0, min(1.0, alpha))
        self.on_color(hex_of(rgba, alpha=True))

    def _open_picker(self) -> None:
        from . import color_picker
        canvas = getattr(self.section.host, "canvas", None)
        if self.target and hasattr(color_picker, "open_paint_picker") and canvas is not None:
            # Bound to the selection: one undo step per gesture, gradients,
            # document colors and the eyedropper come with it.
            color_picker.open_paint_picker(self.swatch, canvas, self.target)
            return

        def changed(paint, phase: str = "change") -> None:
            if paint is not None and phase != "begin":
                self.on_color(css_from_paint(paint))
        color_picker.open_color_popover(self.swatch, paint_from_css(self.color), changed)


class StackPaintRow(PaintRow):
    """One paint of a stack: `[swatch] hex-or-kind  opacity %  eye  remove`.

    Right-click for its blend mode and order; drag a row onto another to
    reorder the stack.
    """

    def __init__(self, section: "PaintStackSection") -> None:
        super().__init__(section, section.noun, on_color=lambda _value: None,
                         on_visible=lambda visible: section.set_visible(self.index, visible),
                         on_remove=lambda: section.remove(self.index), target=section.target)
        self.stack_section = section
        self.index = 0
        self.paint: dict[str, Any] | None = None
        self.add_css_class("canvas-stack-row")
        menu = Gtk.GestureClick(button=3)
        menu.connect("pressed", lambda _gesture, _count, x, y: self._open_menu(x, y))
        self.add_controller(menu)
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(f"{section.target}:{self.index}"))
        source.connect("drag-begin", lambda *_: self.add_css_class("dragging"))
        source.connect("drag-end", lambda *_: self.remove_css_class("dragging"))
        self.add_controller(source)
        drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        drop.connect("enter", lambda *_: (self.add_css_class("drop-target"), Gdk.DragAction.MOVE)[1])
        drop.connect("leave", lambda *_: self.remove_css_class("drop-target"))
        drop.connect("drop", self._dropped)
        self.add_controller(drop)

    def set_stack_paint(self, index: int, paint: dict[str, Any]) -> None:
        self.index, self.paint = index, paint
        if hasattr(self.swatch, "set_paint"):
            self.swatch.set_paint({key: value for key, value in paint.items() if key != "visible"})
        solid_paint = paint.get("type", "solid") == "solid"
        self.hex.set_editable(solid_paint)
        self.hex.set_can_focus(solid_paint)
        (self.hex.remove_css_class if solid_paint else self.hex.add_css_class)("canvas-paint-kind")
        if not _focused(self.hex):
            self.hex.set_text(paints.paint_label(paint))
        _set_number(self.opacity, [round(paints.paint_opacity(paint) * 100)])
        hidden = paint.get("visible", True) is False
        _set_toggle(self.eye, hidden)
        self.eye.get_child().set_from_icon_name("luma-canvas-eye-off-symbolic" if hidden else "luma-canvas-eye-symbolic")
        self.eye.set_tooltip_text(f"Show {self.noun.lower()}" if hidden else f"Hide {self.noun.lower()}")
        (self.add_css_class if hidden else self.remove_css_class)("hidden-paint")
        blend = paint.get("blend", "normal")
        self.set_tooltip_text(None if blend == "normal" else f"Blend: {dict(BLEND_MODES).get(blend, blend)}")

    def _commit_hex(self) -> None:
        if self.paint is None or self.paint.get("type", "solid") != "solid":
            return
        color = paints.parse_color(self.hex.get_text())
        if color is None:
            self.hex.set_text(paints.paint_label(self.paint))
            return
        current = paints.Color.from_dict(self.paint.get("color", {}))
        if paints.to_hex(color.with_alpha(1.0)) == paints.to_hex(current.with_alpha(1.0)):
            return
        replacement = paints.with_paint_opacity({**self.paint, "color": color.with_alpha(1.0).to_dict()},
                                                paints.paint_opacity(self.paint))
        self.stack_section.replace(self.index, replacement, coalesce=True)

    def _set_alpha(self, alpha: float) -> None:
        if self.paint is None:
            return
        self.stack_section.replace(self.index, paints.with_paint_opacity(self.paint, alpha), coalesce=True)

    def _open_picker(self) -> None:
        from . import color_picker
        canvas = getattr(self.section.host, "canvas", None)
        if canvas is not None:
            color_picker.open_paint_picker(self.swatch, canvas, self.stack_section.target, index=self.index)

    def _open_menu(self, x: float, y: float) -> None:
        section = self.stack_section
        popover = Gtk.Popover()
        popover.add_css_class("canvas-settings-sheet")
        popover.set_parent(self)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
        popover.set_pointing_to(area)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_margin_start(6); grid.set_margin_end(6); grid.set_margin_top(6); grid.set_margin_bottom(6)
        caption = Gtk.Label(label="Blend", xalign=0)
        caption.add_css_class("canvas-field-label")
        grid.attach(caption, 0, 0, 1, 1)
        blend = _dropdown(BLEND_MODES, lambda value: section.set_blend(self.index, value), f"{self.noun} blend mode")
        _select(blend, (self.paint or {}).get("blend", "normal"))
        grid.attach(blend, 1, 0, 1, 1)
        count = section.count
        for row, (label, delta) in enumerate((("Move up", 1), ("Move down", -1)), start=1):
            button = Gtk.Button(label=label)
            button.add_css_class("luma-button")
            button.add_css_class("small")
            button.add_css_class("flat")
            button.set_sensitive(0 <= self.index + delta < count)
            button.connect("clicked", lambda _b, step=delta: (popover.popdown(),
                                                              section.move(self.index, self.index + step)))
            grid.attach(button, 0, row, 2, 1)
        popover.set_child(grid)
        popover.connect("closed", lambda widget: GLib.idle_add(lambda: widget.unparent() or False))
        popover.popup()

    def _dropped(self, _target, value, _x, _y) -> bool:
        self.remove_css_class("drop-target")
        try:
            target, index = str(value).split(":")
            source = int(index)
        except ValueError:
            return False
        if target != self.stack_section.target or source == self.index:
            return False
        self.stack_section.move(source, self.index)
        return True


class PaintStackSection(Section):
    """Fill or Stroke: every paint of the stack, the top paint first (Figma)."""

    paint_section = True
    target = "fill"
    noun = "Fill"
    default_paint = "#d9d9d9"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        self.add_action("luma-canvas-plus-symbolic", f"Add {self.noun.lower()}", self._add)
        self.stack_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.stack_rows.add_css_class("canvas-paint-stack")
        self.body.append(self.stack_rows)
        self.rows: list[StackPaintRow] = []
        self.count = 0
        self.empty = Gtk.Label(label=f"Click + to add a {self.noun.lower()}.", xalign=0)
        self.empty.add_css_class("canvas-empty-line")
        self.body.append(self.empty)
        self.mixed = Gtk.Label(label=f"Click + to replace mixed {self.noun.lower()}s.", xalign=0)
        self.mixed.add_css_class("canvas-empty-line")
        self.body.append(self.mixed)

    @property
    def row(self) -> StackPaintRow | None:
        """The top paint's row (older callers know one row)."""
        return self.rows[0] if self.rows else None

    def stacks(self, selection: list[LayoutObject]) -> tuple[bool, list[dict[str, Any]]]:
        values = [paints.paint_stack(item, self.target) for item in selection]
        if not values:
            return False, []
        mixed = any(value != values[0] for value in values[1:])
        return mixed, ([] if mixed else values[0])

    def update(self, selection: list[LayoutObject]) -> None:
        mixed, stack = self.stacks(selection)
        self.count = len(stack)
        self.mixed.set_visible(mixed)
        self.empty.set_visible(not mixed and not stack)
        while len(self.rows) > len(stack):
            self.stack_rows.remove(self.rows.pop())
        while len(self.rows) < len(stack):
            row = StackPaintRow(self)
            self.rows.append(row)
            self.stack_rows.append(row)
        # Top of the stack first, as the canvas draws it on top.
        for position, row in enumerate(self.rows):
            index = len(stack) - 1 - position
            row.set_stack_paint(index, stack[index])

    # Edits: one undo step each; scrubs and typing coalesce.

    def _ids(self) -> list[str]:
        return [item.id for item in self.targets if not item.locked]

    def _add(self) -> None:
        paint = paints.solid(paints.parse_color(str(self.host.recall(f"default-{self.target}", self.default_paint)))
                             or paints.Color(0.85, 0.85, 0.85))
        mixed, _stack = self.stacks(self.targets)

        def change() -> None:
            if mixed:
                self.host.editor.set_property(self._ids(), f"{self.target}s", [paint])
            else:
                self.host.editor.add_stack_paint(self._ids(), self.target, paint)
            self.after_add()
        self.edit(f"Add {self.noun.lower()}", change)

    def after_add(self) -> None:
        pass

    def replace(self, index: int, paint: dict[str, Any], *, coalesce: bool = False) -> None:
        self.edit(f"Change {self.noun.lower()}",
                  lambda: self.host.editor.set_stack_paint(self._ids(), self.target, index, paint), coalesce=coalesce)

    def set_visible(self, index: int, visible: bool) -> None:
        self.edit(f"Show {self.noun.lower()}" if visible else f"Hide {self.noun.lower()}",
                  lambda: self.host.editor.update_stack_paint(self._ids(), self.target, index, visible=visible))

    def set_blend(self, index: int, blend: str) -> None:
        self.edit("Change blend mode",
                  lambda: self.host.editor.update_stack_paint(self._ids(), self.target, index, blend=blend))

    def remove(self, index: int) -> None:
        self.edit(f"Remove {self.noun.lower()}",
                  lambda: self.host.editor.set_stack_paint(self._ids(), self.target, index, None))

    def move(self, source: int, destination: int) -> None:
        if source == destination or not (0 <= destination < self.count):
            return
        self.edit(f"Reorder {self.noun.lower()}s",
                  lambda: self.host.editor.move_stack_paint(self._ids(), self.target, source, destination))


class FillSection(PaintStackSection):
    title, key = "Fill", "fill"
    target, noun, default_paint = "fill", "Fill", "#d9d9d9"


class StrokeSection(PaintStackSection):
    title, key = "Stroke", "stroke"
    target, noun, default_paint = "stroke", "Stroke", "#000000"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        self.details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.weight = _glyph_field("luma-canvas-line-symbolic", "Stroke weight", minimum=0)
        self.weight.connect("value-changed", lambda _f, value: self.edit(
            "Change stroke weight", lambda: self.host.editor.set_property(self.ids, "stroke_width", max(0.0, value)),
            coalesce=True))
        self.details.append(self.weight)
        self.align = _dropdown(STROKE_ALIGNS, lambda value: self.edit(
            "Change stroke position", lambda: self.host.editor.set_property(self.ids, "stroke_align", value)),
            "Stroke position")
        self.align.set_hexpand(True)
        self.details.append(self.align)
        settings = Gtk.MenuButton()
        settings.set_child(Gtk.Image(icon_name="luma-canvas-settings-symbolic", pixel_size=14))
        settings.add_css_class("luma-icon-button")
        settings.add_css_class("quiet")
        settings.add_css_class("canvas-row-action")
        settings.set_tooltip_text("Stroke settings")
        settings.update_property([Gtk.AccessibleProperty.LABEL], ["Stroke settings"])
        settings.set_popover(self._settings_popover())
        self.details.append(settings)
        # Weight and position sit under the paints, above the empty line.
        self.body.append(self.details)
        self.body.reorder_child_after(self.details, self.stack_rows)

    def _settings_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        popover.add_css_class("canvas-settings-sheet")
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_margin_start(6); grid.set_margin_end(6); grid.set_margin_top(6); grid.set_margin_bottom(6)
        self.cap = _dropdown(STROKE_CAPS, lambda value: self.edit(
            "Change stroke caps", lambda: self.host.editor.set_property(self.ids, "stroke_cap", value)), "Caps")
        self.join = _dropdown(STROKE_JOINS, lambda value: self.edit(
            "Change stroke joins", lambda: self.host.editor.set_property(self.ids, "stroke_join", value)), "Joins")
        self.dash = Gtk.Entry(placeholder_text="Dash and gap, like 6 4")
        self.dash.add_css_class("canvas-text-field")
        self.dash.connect("activate", lambda entry: self._set_dash(entry.get_text()))
        for row, (caption, control) in enumerate((("Caps", self.cap), ("Joins", self.join), ("Dashes", self.dash))):
            label = Gtk.Label(label=caption, xalign=0)
            label.add_css_class("canvas-field-label")
            grid.attach(label, 0, row, 1, 1)
            grid.attach(control, 1, row, 1, 1)
        popover.set_child(grid)
        return popover

    def update(self, selection: list[LayoutObject]) -> None:
        super().update(selection)
        has = any(paints.paint_stack(item, "stroke") for item in selection)
        self.details.set_visible(has)
        if has:
            _set_number(self.weight, [item.stroke_width for item in selection])
            for control, attribute in ((self.align, "stroke_align"), (self.cap, "stroke_cap"), (self.join, "stroke_join")):
                mixed_value, value = _mixed(getattr(item, attribute) for item in selection)
                if not mixed_value:
                    _select(control, value)
            if not _focused(self.dash):
                self.dash.set_text(" ".join(f"{value:g}" for value in selection[0].stroke_dash))

    def after_add(self) -> None:
        for item in self.targets:
            if not item.locked and not item.stroke_width:
                self.host.editor.set_property([item.id], "stroke_width", 1.0)

    def _set_dash(self, text: str) -> None:
        try:
            values = [float(part) for part in text.replace(",", " ").split()]
        except ValueError:
            return
        self.edit("Change dashes", lambda: self.host.editor.set_property(self.ids, "stroke_dash", values))


def _effect_kind(effect: dict[str, Any]) -> str:
    kind = str(effect.get("type") or effect.get("kind") or "drop_shadow").replace("-", "_")
    return kind if kind in dict(EFFECT_KINDS) else "drop_shadow"


def _effect_visible(effect: dict[str, Any]) -> bool:
    return bool(effect.get("visible", effect.get("enabled", True)))


class EffectsSection(Section):
    """Effects belong to the layer they are on, a group included (Figma): a
    group's shadow or blur is cast by its layers together."""

    title, key = "Effects", "effects"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        self.add_action("luma-canvas-plus-symbolic", "Add effect", self._add)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.body.append(self.rows)
        self.empty = Gtk.Label(label="Click + to add a shadow or blur.", xalign=0)
        self.empty.add_css_class("canvas-empty-line")
        self.body.append(self.empty)
        self._row_widgets: list[dict] = []

    def _effects(self) -> list[dict[str, Any]]:
        selection = self.targets
        return [dict(effect) for effect in selection[0].effects] if selection else []

    def update(self, selection: list[LayoutObject]) -> None:
        # Several selected layers show their effects when they agree.
        same = bool(selection) and all(item.effects == selection[0].effects for item in selection)
        effects = [dict(effect) for effect in selection[0].effects] if same else []
        self.empty.set_visible(not effects)
        while len(self._row_widgets) > len(effects):
            self.rows.remove(self._row_widgets.pop()["row"])
        while len(self._row_widgets) < len(effects):
            self._row_widgets.append(self._build_row(len(self._row_widgets)))
        for widgets, effect in zip(self._row_widgets, effects):
            kind = _effect_kind(effect)
            _select(widgets["kind"], kind)
            visible = _effect_visible(effect)
            _set_toggle(widgets["eye"], not visible)
            widgets["eye"].get_child().set_from_icon_name(
                "luma-canvas-eye-off-symbolic" if not visible else "luma-canvas-eye-symbolic")
            widgets["glyph"].get_child().set_from_icon_name(
                "luma-canvas-blur-symbolic" if kind.endswith("blur") else "luma-canvas-shadow-symbolic")

    def _build_row(self, index: int) -> dict:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.add_css_class("canvas-paint-row")
        glyph = Gtk.MenuButton()
        glyph.set_child(Gtk.Image(icon_name="luma-canvas-shadow-symbolic", pixel_size=14))
        glyph.add_css_class("luma-icon-button")
        glyph.add_css_class("quiet")
        glyph.add_css_class("canvas-row-action")
        glyph.set_tooltip_text("Effect settings")
        glyph.update_property([Gtk.AccessibleProperty.LABEL], ["Effect settings"])
        glyph.set_create_popup_func(lambda button, position=index: button.set_popover(self._settings(position)))
        row.append(glyph)
        kind = _dropdown(EFFECT_KINDS, lambda value, position=index: self._change(position, type=value), "Effect")
        kind.set_hexpand(True)
        row.append(kind)
        eye = _icon_toggle("luma-canvas-eye-symbolic", "Hide effect")
        eye.connect("toggled", lambda control, position=index: None if control.canvas_quiet
                    else self._change(position, visible=not control.get_active()))
        row.append(eye)
        remove = IconButton("luma-canvas-minus-symbolic", "Remove effect", context=self.host.context, quiet=True)
        remove.add_css_class("canvas-row-action")
        remove.connect("clicked", lambda *_: self._remove(index))
        row.append(remove)
        self.rows.append(row)
        return {"row": row, "kind": kind, "eye": eye, "glyph": glyph}

    def _settings(self, index: int) -> Gtk.Popover:
        effects = self._effects()
        effect = effects[index] if index < len(effects) else {}
        popover = Gtk.Popover()
        popover.add_css_class("canvas-settings-sheet")
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_margin_start(6); grid.set_margin_end(6); grid.set_margin_top(6); grid.set_margin_bottom(6)
        kind = _effect_kind(effect)
        fields = (("radius", "Blur", 8.0),) if kind.endswith("blur") else (
            ("x", "X", 0.0), ("y", "Y", 4.0), ("blur", "Blur", 12.0), ("spread", "Spread", 0.0))
        for position, (key, label, default) in enumerate(fields):
            field = _number(label, minimum=0 if key in {"blur", "radius"} else None)
            field.set_value(float(effect.get(key, default)))
            field.set_size_request(92, -1)
            field.connect("value-changed", lambda _f, value, name=key: self._change(index, coalesce=True, **{name: value}))
            grid.attach(field, position % 2, position // 2, 1, 1)
        if not kind.endswith("blur"):
            color = str(effect.get("color") or effect.get("colour") or "#00000040")
            swatch = PaintRow(self, "Shadow", on_color=lambda value: self._change(index, coalesce=True, color=value),
                              on_visible=lambda visible: self._change(index, visible=visible),
                              on_remove=lambda: (popover.popdown(), self._remove(index)))
            swatch.set_paint(color, _effect_visible(effect))
            grid.attach(swatch, 0, 2, 2, 1)
        popover.set_child(grid)
        return popover

    def _add(self) -> None:
        def change() -> None:
            for item in self.targets:
                effects = [dict(effect) for effect in item.effects]
                effects.append({"type": "drop_shadow", "x": 0, "y": 4, "blur": 12, "spread": 0,
                                "color": "#00000040", "visible": True, "blend": "normal"})
                self.host.editor.set_property([item.id], "effects", effects)
        self.edit("Add effect", change)

    def _change(self, index: int, coalesce: bool = False, **values: Any) -> None:
        def change() -> None:
            for item in self.targets:
                effects = [dict(effect) for effect in item.effects]
                if index < len(effects):
                    effects[index].update(values)
                    effects[index].pop("kind", None)
                    effects[index].pop("enabled", None)
                    self.host.editor.set_property([item.id], "effects", effects)
        self.edit("Change effect", change, coalesce=coalesce)

    def _remove(self, index: int) -> None:
        def change() -> None:
            for item in self.targets:
                effects = [dict(effect) for effect in item.effects]
                if index < len(effects):
                    effects.pop(index)
                    self.host.editor.set_property([item.id], "effects", effects)
        self.edit("Remove effect", change)


TEXT_ALIGN_H = (("left", "Align left", "luma-canvas-text-align-left-symbolic"),
                ("center", "Align center", "luma-canvas-text-align-center-symbolic"),
                ("right", "Align right", "luma-canvas-text-align-right-symbolic"),
                ("justify", "Justify", "luma-canvas-text-align-justify-symbolic"))
TEXT_ALIGN_V = (("top", "Align top", "luma-canvas-text-align-top-symbolic"),
                ("middle", "Align middle", "luma-canvas-text-align-middle-symbolic"),
                ("bottom", "Align bottom", "luma-canvas-text-align-bottom-symbolic"))
TEXT_SIZING = (("auto_width", "Auto width", "luma-canvas-text-auto-width-symbolic"),
               ("auto_height", "Auto height", "luma-canvas-text-auto-height-symbolic"),
               ("fixed", "Fixed size", "luma-canvas-text-fixed-size-symbolic"))
TEXT_DECORATION = (("none", "None", "luma-canvas-text-none-symbolic"),
                   ("underline", "Underline", "luma-canvas-text-underline-symbolic"),
                   ("strikethrough", "Strikethrough", "luma-canvas-text-strikethrough-symbolic"))
TEXT_CASE = (("none", "As typed", "–"), ("upper", "Uppercase", "AG"), ("lower", "Lowercase", "ag"),
             ("title", "Title case", "Ag"), ("small_caps", "Small caps", "Aɢ"))
TEXT_LIST = (("none", "No list", "luma-canvas-text-none-symbolic"),
             ("bullets", "Bulleted list", "luma-canvas-list-bullets-symbolic"),
             ("numbers", "Numbered list", "luma-canvas-list-numbers-symbolic"))
TEXT_TRIM = (("standard", "Standard"), ("cap_height", "Cap height to baseline"))
FONT_STYLES = tuple(((weight, italic), f"{name} italic" if italic and name != "Regular" else ("Italic" if italic else name))
                    for weight, name in ((300, "Light"), (400, "Regular"), (500, "Medium"), (600, "Semibold"),
                                         (700, "Bold"), (800, "Extra bold"), (900, "Black"))
                    for italic in (False, True))
OPENTYPE_FEATURES = (("liga", "Ligatures"), ("calt", "Contextual alternates"), ("dlig", "Discretionary ligatures"),
                     ("tnum", "Tabular numbers"), ("onum", "Old-style numbers"), ("frac", "Fractions"),
                     ("zero", "Slashed zero"), ("case", "Case-sensitive forms"), ("ss01", "Stylistic set 1"))
# Keys that change an auto-sized text box's size, so it is refitted.
TEXT_FIT_KEYS = {"sizing", "font_family", "font_size", "font_weight", "italic", "letter_spacing", "line_height",
                 "paragraph_spacing", "paragraph_indent", "list_style", "case"}


def _type_settings(item: LayoutObject) -> dict[str, Any]:
    """Every type setting of a text layer with defaults (the interaction
    stream's typography module when present)."""
    try:
        from .typography import type_settings
        return type_settings(item.metadata)
    except ImportError:
        metadata = item.metadata or {}
        return {"font_family": metadata.get("font_family", "Figtree"), "font_size": metadata.get("font_size", 16),
                "font_weight": metadata.get("font_weight", 400), "italic": metadata.get("italic", False),
                "line_height": metadata.get("line_height", {"unit": "auto"}),
                "letter_spacing": metadata.get("letter_spacing", {"unit": "percent", "value": 0}),
                "align_h": metadata.get("align_h", metadata.get("align", "left")),
                **{key: metadata.get(key, default) for key, default in (
                    ("align_v", "top"), ("sizing", "fixed"), ("decoration", "none"), ("case", "none"),
                    ("list_style", "none"), ("vertical_trim", "standard"), ("paragraph_spacing", 0),
                    ("paragraph_indent", 0), ("truncate", False), ("max_lines", None),
                    ("hanging_punctuation", False), ("features", {}))}}


class ChoiceGroup(Gtk.Box):
    """A compact connected group of mutually exclusive choices (icons or short text)."""

    def __init__(self, choices, on_change: Callable[[str], None], label: str, *, text: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, homogeneous=True)
        self.add_css_class("luma-connected-buttons")
        self.add_css_class("compact")
        self.add_css_class("canvas-choice-group")
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.buttons: dict[str, Gtk.ToggleButton] = {}
        self.quiet = False
        first = None
        for value, name, face in choices:
            button = Gtk.ToggleButton()
            if text:
                button.set_label(face)
            else:
                button.set_child(Gtk.Image(icon_name=face, pixel_size=14))
            button.set_tooltip_text(name)
            button.update_property([Gtk.AccessibleProperty.LABEL], [name])
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", lambda widget, choice=value: None if self.quiet or not widget.get_active()
                           else on_change(choice))
            self.append(button)
            self.buttons[value] = button

    def select(self, value: str | None) -> None:
        self.quiet = True
        if value in self.buttons:
            self.buttons[value].set_active(True)
        else:
            for button in self.buttons.values():
                button.set_active(False)
        self.quiet = False


def _set_unit(field: NumericField, unit: str) -> None:
    child = field.get_last_child()
    if isinstance(child, Gtk.Label) and "luma-numeric-unit" in child.get_css_classes():
        child.set_label(unit)


class SpacingField:
    """A line height or letter spacing number that keeps its unit (`auto`,
    `px`, `percent`) and accepts "auto" typed in."""

    def __init__(self, section: "TextSection", key: str, icon: str, label: str, *,
                 allow_auto: bool, default_unit: str) -> None:
        self.section, self.key, self.allow_auto, self.default_unit = section, key, allow_auto, default_unit
        self.unit = default_unit
        self.field = _glyph_field(icon, label, unit="%", minimum=-100 if key == "letter_spacing" else 0,
                                  maximum=1000, step=0.5 if key == "letter_spacing" else 1,
                                  digits=1 if key == "letter_spacing" else 0)
        self.field.connect("value-changed", lambda _f, value: self.section._meta(
            key, {"unit": "percent" if self.unit == "auto" else self.unit, "value": value}, f"Change {label.lower()}", True))
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._typed)
        self.field.entry.add_controller(keys)

    def _typed(self, _controller, keyval, _code, _state) -> bool:
        if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        text = self.field.entry.get_text().strip().casefold()
        if self.allow_auto and text == "auto":
            self.section._meta(self.key, {"unit": "auto"}, "Change line height")
            return True
        if text.endswith("px"):
            try:
                value = float(text[:-2])
            except ValueError:
                return False
            self.section._meta(self.key, {"unit": "px", "value": value}, "Change spacing")
            return True
        return False

    def update(self, values: list[dict[str, Any]]) -> None:
        if _focused(self.field):
            return
        mixed, first = _mixed(values)
        if mixed or first is None:
            self.field.set_mixed()
            return
        self.unit = str(first.get("unit", self.default_unit))
        if self.unit == "auto":
            self.field.set_value(0)
            self.field.entry.set_text("Auto")
            _set_unit(self.field, "")
        else:
            self.field.set_value(float(first.get("value", 0)))
            _set_unit(self.field, "px" if self.unit == "px" else "%")


class TextSection(Section):
    """Font, style, size, spacing, alignment and resizing, with Type settings
    for the rest (CANVAS-SPEC 9.3). Keys are the interaction stream's type
    settings in the text layer's metadata (docs/batch1-interfaces.md)."""

    title, key = "Text", "text"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        self.add_action("luma-canvas-settings-symbolic", "Type settings", self._open_settings)
        self.family = Gtk.Button()
        self.family.add_css_class("canvas-font-button")
        family_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.family_label = Gtk.Label(xalign=0, hexpand=True)
        self.family_label.set_ellipsize(Pango.EllipsizeMode.END)
        family_row.append(self.family_label)
        self.family_warning = Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=14, visible=False)
        self.family_warning.add_css_class("canvas-font-missing")
        family_row.append(self.family_warning)
        family_row.append(Gtk.Image(icon_name="luma-canvas-chevron-down-symbolic", pixel_size=12))
        self.family.set_child(family_row)
        self.family.set_hexpand(True)
        self.family.update_property([Gtk.AccessibleProperty.LABEL], ["Font"])
        self.family.connect("clicked", lambda *_: self._open_fonts())
        self.body.append(self.family)
        self._style_family: str | None = None

        grid = self.grid()
        self.style = _dropdown(FONT_STYLES, self._set_style, "Style")
        self.style.set_hexpand(True)
        self.size = _glyph_field("luma-canvas-font-size-symbolic", "Font size", minimum=1, maximum=10000)
        self.size.connect("value-changed", lambda _f, value: self._meta("font_size", value, "Change font size", True))
        grid.attach(self.style, 0, 0, 1, 1)
        grid.attach(self.size, 1, 0, 1, 1)
        self.line_height = SpacingField(self, "line_height", "luma-canvas-line-height-symbolic", "Line height",
                                        allow_auto=True, default_unit="auto")
        self.letter_spacing = SpacingField(self, "letter_spacing", "luma-canvas-letter-spacing-symbolic",
                                           "Letter spacing", allow_auto=False, default_unit="percent")
        grid.attach(self.line_height.field, 0, 1, 1, 1)
        grid.attach(self.letter_spacing.field, 1, 1, 1, 1)
        self.align_h = ChoiceGroup(TEXT_ALIGN_H, lambda value: self._meta("align_h", value, "Align text"), "Text alignment")
        self.align_v = ChoiceGroup(TEXT_ALIGN_V, lambda value: self._meta("align_v", value, "Align text vertically"),
                                   "Vertical alignment")
        grid.attach(self.align_h, 0, 2, 1, 1)
        grid.attach(self.align_v, 1, 2, 1, 1)
        self.sizing = ChoiceGroup(TEXT_SIZING, lambda value: self._meta("sizing", value, "Change resizing"), "Resizing")
        grid.attach(self.sizing, 0, 3, 1, 1)

        self.content = None
        if not self.host.supports("text-editing-on-canvas"):
            self.content = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
            self.content.add_css_class("canvas-text-content")
            self.content.update_property([Gtk.AccessibleProperty.LABEL], ["Text"])
            self.content.get_buffer().connect("changed", self._content_changed)
            frame = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=56,
                                       max_content_height=160, propagate_natural_height=True)
            frame.add_css_class("canvas-text-content-frame")
            frame.set_child(self.content)
            self.body.append(frame)
        self._quiet = False
        self._settings = None

    def _texts(self) -> list[LayoutObject]:
        return [item for item in self.host.document.selection if item.kind == "text"]

    def update(self, selection: list[LayoutObject]) -> None:
        texts = [item for item in selection if item.kind == "text"]
        if not texts:
            return
        settings = [_type_settings(item) for item in texts]
        mixed, family = _mixed(str(value["font_family"]) for value in settings)
        self.family_label.set_label("Mixed" if mixed else family)
        from .font_picker import find_family, styles_for
        missing = not mixed and find_family(family) is None
        self.family_warning.set_visible(missing)
        self.family.set_tooltip_text(f"“{family}” isn’t installed. Canvas shows a similar font." if missing else "Font")
        if not mixed and family != self._style_family:
            self._style_family = family
            choices = styles_for(family)
            self.style.canvas_quiet = True
            self.style.set_model(Gtk.StringList.new([label for _value, label in choices]))
            self.style.canvas_choices = [value for value, _label in choices]
            self.style.canvas_quiet = False
        mixed, style = _mixed((int(float(value["font_weight"])), bool(value["italic"])) for value in settings)
        if not mixed and style is not None:
            weight, italic = style
            candidates = [choice for choice in self.style.canvas_choices if choice[1] == italic] or self.style.canvas_choices
            if candidates:
                _select(self.style, min(candidates, key=lambda choice: abs(choice[0] - weight)))
        _set_number(self.size, [float(value["font_size"]) for value in settings])
        self.line_height.update([dict(value["line_height"]) for value in settings])
        self.letter_spacing.update([dict(value["letter_spacing"]) for value in settings])
        for group, key in ((self.align_h, "align_h"), (self.align_v, "align_v"), (self.sizing, "sizing")):
            mixed, value = _mixed(entry[key] for entry in settings)
            group.select(None if mixed else value)
        if self._settings is not None:
            self._settings.update(texts, settings)
        if self.content is not None and not _focused(self.content) and len(texts) == 1:
            buffer = self.content.get_buffer()
            start, end = buffer.get_bounds()
            if buffer.get_text(start, end, True) != texts[0].text:
                self._quiet = True
                buffer.set_text(texts[0].text)
                self._quiet = False

    def _open_fonts(self) -> None:
        from .font_picker import FontPicker
        texts = self._texts()
        current = _type_settings(texts[0])["font_family"] if texts else "Figtree"
        document_fonts = {str(_type_settings(item)["font_family"]) for item in self.host.document.objects
                          if item.kind == "text"}
        picker = FontPicker(str(current), document_fonts, lambda name: self._meta("font_family", name, "Change font"))
        picker.set_parent(self.family)
        picker.connect("closed", lambda *_: GLib.idle_add(lambda: (picker.unparent(), False)[1]))
        self.font_picker = picker
        picker.popup()

    def _set_style(self, value: tuple[int, bool]) -> None:
        weight, italic = value
        self._meta_many({"font_weight": weight, "italic": italic}, "Change font style")

    def _meta(self, key: str, value: Any, label: str, coalesce: bool = False) -> None:
        self._meta_many({key: value}, label, coalesce)

    def _meta_many(self, values: dict[str, Any], label: str, coalesce: bool = False) -> None:
        canvas = getattr(self.host, "canvas", None)

        def change() -> None:
            for item in self.host.document.selection:
                if item.kind != "text":
                    continue
                extra = {}
                if "align_v" in values and values["align_v"] != "top" and _type_settings(item)["sizing"] == "auto_height":
                    # A box that grows to its text has no room to align in; the
                    # box a person dragged out keeps its height and becomes fixed
                    # so the choice shows, as it does in Figma.
                    extra["sizing"] = "fixed"
                self.host.editor.set_property([item.id], "metadata", {**item.metadata, **values, **extra})
                refreshed = self.host.document.object(item.id)
                sizing = _type_settings(refreshed)["sizing"]
                if sizing != "fixed" and TEXT_FIT_KEYS.intersection(values) and hasattr(canvas, "fit_text"):
                    canvas.fit_text(refreshed)
        self.edit(label, change, coalesce=coalesce)

    def _content_changed(self, buffer) -> None:
        if self._quiet:
            return
        start, end = buffer.get_bounds()
        value = buffer.get_text(start, end, True)
        texts = [item.id for item in self.host.document.selection if item.kind == "text"]
        if len(texts) == 1:
            self.edit("Edit text", lambda: self.host.editor.set_property(texts, "text", value), coalesce=True)

    def _open_settings(self) -> None:
        button = self.head.get_last_child()
        popover = TypeSettings(self)
        popover.set_parent(button)
        popover.connect("closed", lambda *_: (setattr(self, "_settings", None),
                                              GLib.idle_add(lambda: (popover.unparent(), False)[1])))
        self._settings = popover
        texts = self._texts()
        popover.update(texts, [_type_settings(item) for item in texts])
        popover.popup()


def _labeled_grid(rows) -> Gtk.Grid:
    grid = Gtk.Grid(column_spacing=8, row_spacing=8)
    for row, (caption, control) in enumerate(rows):
        label = Gtk.Label(label=caption, xalign=0)
        label.add_css_class("canvas-field-label")
        label.set_size_request(96, -1)
        grid.attach(label, 0, row, 1, 1)
        control.set_hexpand(True)
        grid.attach(control, 1, row, 1, 1)
    return grid


def _switch(on_change: Callable[[bool], None], label: str) -> Gtk.Switch:
    switch = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
    switch.update_property([Gtk.AccessibleProperty.LABEL], [label])
    switch.canvas_quiet = False
    switch.connect("notify::active", lambda widget, _p: None if widget.canvas_quiet else on_change(widget.get_active()))
    return switch


def _set_switch(switch: Gtk.Switch, values) -> None:
    mixed, value = _mixed(bool(entry) for entry in values)
    if not mixed and switch.get_active() != bool(value):
        switch.canvas_quiet = True
        switch.set_active(bool(value))
        switch.canvas_quiet = False


class TypeSettings(Gtk.Popover):
    """Type settings: Basics and Details, in the inspector's row heights."""

    def __init__(self, section: TextSection) -> None:
        super().__init__(position=Gtk.PositionType.LEFT)
        self.add_css_class("canvas-type-settings")
        self.section = section
        meta = section._meta
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_size_request(280, -1)
        title = Gtk.Label(label="Type settings", xalign=0)
        title.add_css_class("canvas-sheet-title")
        box.append(title)
        self.tabs = ChoiceGroup((("basics", "Basics", "Basics"), ("details", "Details", "Details")),
                                lambda value: self.stack.set_visible_child_name(value), "Type settings pages", text=True)
        self.tabs.add_css_class("canvas-tabs")
        box.append(self.tabs)
        self.stack = Gtk.Stack(vhomogeneous=False, transition_type=Gtk.StackTransitionType.NONE)
        box.append(self.stack)
        self.tabs.select("basics")

        self.sizing = ChoiceGroup(TEXT_SIZING, lambda value: meta("sizing", value, "Change resizing"), "Resizing")
        self.align_h = ChoiceGroup(TEXT_ALIGN_H, lambda value: meta("align_h", value, "Align text"), "Alignment")
        self.align_v = ChoiceGroup(TEXT_ALIGN_V, lambda value: meta("align_v", value, "Align text vertically"),
                                   "Vertical alignment")
        alignment = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        alignment.append(self.align_h)
        alignment.append(self.align_v)
        self.decoration = ChoiceGroup(TEXT_DECORATION, lambda value: meta("decoration", value, "Change decoration"),
                                      "Decoration")
        self.case = ChoiceGroup(TEXT_CASE, lambda value: meta("case", value, "Change case"), "Case", text=True)
        self.trim = _dropdown(TEXT_TRIM, lambda value: meta("vertical_trim", value, "Change vertical trim"),
                              "Vertical trim")
        self.list_style = ChoiceGroup(TEXT_LIST, lambda value: meta("list_style", value, "Change list style"),
                                      "List style")
        self.paragraph_spacing = _glyph_field("luma-canvas-line-height-symbolic", "Paragraph spacing", minimum=0,
                                              maximum=1000)
        self.paragraph_spacing.connect("value-changed", lambda _f, value: meta(
            "paragraph_spacing", value, "Change paragraph spacing", True))
        truncate_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.truncate = _switch(lambda active: meta("truncate", active, "Truncate text"), "Truncate text")
        self.max_lines = _number("", minimum=1, maximum=1000, tooltip="Maximum lines")
        self.max_lines.label.set_visible(False)
        self.max_lines.update_property([Gtk.AccessibleProperty.LABEL], ["Maximum lines"])
        _set_unit(self.max_lines, "")
        self.max_lines.connect("value-changed", lambda _f, value: meta("max_lines", int(value), "Change maximum lines"))
        truncate_row.append(self.truncate)
        truncate_row.append(self.max_lines)
        self.stack.add_named(_labeled_grid((
            ("Resizing", self.sizing), ("Alignment", alignment), ("Decoration", self.decoration), ("Case", self.case),
            ("Vertical trim", self.trim), ("List style", self.list_style),
            ("Paragraph spacing", self.paragraph_spacing), ("Truncate text", truncate_row),
        )), "basics")

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.letter_spacing = SpacingField(section, "letter_spacing", "luma-canvas-letter-spacing-symbolic",
                                           "Letter spacing", allow_auto=False, default_unit="percent")
        self.line_height = SpacingField(section, "line_height", "luma-canvas-line-height-symbolic", "Line height",
                                        allow_auto=True, default_unit="auto")
        self.indent = _glyph_field("luma-canvas-text-align-left-symbolic", "Paragraph indent", minimum=0, maximum=1000)
        self.indent.connect("value-changed", lambda _f, value: meta("paragraph_indent", value, "Change paragraph indent", True))
        self.hanging = _switch(lambda active: meta("hanging_punctuation", active, "Hanging punctuation"),
                               "Hanging punctuation")
        details.append(_labeled_grid((("Letter spacing", self.letter_spacing.field),
                                      ("Line height", self.line_height.field),
                                      ("Paragraph indent", self.indent), ("Hanging punctuation", self.hanging))))
        heading = Gtk.Label(label="OpenType features", xalign=0)
        heading.add_css_class("canvas-group-label")
        details.append(heading)
        self.features: dict[str, Gtk.Switch] = {}
        rows = []
        for tag, name in OPENTYPE_FEATURES:
            switch = _switch(lambda active, feature=tag: self._set_feature(feature, active), name)
            self.features[tag] = switch
            rows.append((name, switch))
        details.append(_labeled_grid(rows))
        self.stack.add_named(details, "details")
        self.set_child(box)

    def _set_feature(self, tag: str, active: bool) -> None:
        texts = self.section._texts()
        if not texts:
            return

        def change() -> None:
            for item in texts:
                features = dict(_type_settings(item)["features"] or {})
                features[tag] = active
                self.section.host.editor.set_property([item.id], "metadata", {**item.metadata, "features": features})
        self.section.edit("Change OpenType features", change)

    def update(self, texts: list[LayoutObject], settings: list[dict[str, Any]]) -> None:
        if not texts:
            return
        for group, key in ((self.sizing, "sizing"), (self.align_h, "align_h"), (self.align_v, "align_v"),
                           (self.decoration, "decoration"), (self.case, "case"), (self.list_style, "list_style")):
            mixed, value = _mixed(entry[key] for entry in settings)
            group.select(None if mixed else value)
        mixed, trim = _mixed(entry["vertical_trim"] for entry in settings)
        if not mixed:
            _select(self.trim, trim)
        _set_number(self.paragraph_spacing, [float(entry["paragraph_spacing"] or 0) for entry in settings])
        _set_number(self.indent, [float(entry["paragraph_indent"] or 0) for entry in settings])
        _set_switch(self.truncate, [entry["truncate"] for entry in settings])
        lines = [entry["max_lines"] for entry in settings]
        self.max_lines.set_visible(all(entry["truncate"] for entry in settings))
        if all(value is not None for value in lines):
            _set_number(self.max_lines, [float(value) for value in lines])
        _set_switch(self.hanging, [entry["hanging_punctuation"] for entry in settings])
        self.letter_spacing.update([dict(entry["letter_spacing"]) for entry in settings])
        self.line_height.update([dict(entry["line_height"]) for entry in settings])
        for tag, switch in self.features.items():
            defaults = tag in {"liga", "calt"}
            _set_switch(switch, [bool((entry["features"] or {}).get(tag, defaults)) for entry in settings])


MASK_TYPES = (("alpha", "Alpha"), ("outline", "Vector outline"), ("luminance", "Luminance"))


def _mask_layers(document: Document, selection: list[LayoutObject]) -> list[LayoutObject]:
    """The masks a selection stands for: selected masks, or the masks inside selected mask groups."""
    from .geometry import mask_mode
    found: list[LayoutObject] = []
    for item in selection:
        if mask_mode(item):
            found.append(item)
        elif item.metadata.get("mask_group"):
            found.extend(child for child in document.children(item.id) if mask_mode(child))
    return found


class MaskSection(Section):
    """A mask's type (Figma: alpha, vector outline, luminance) and Release."""

    title, key = "Mask", "mask"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.kind = _dropdown(MASK_TYPES, self._set_kind, "Mask type")
        self.kind.set_hexpand(True)
        row.append(self.kind)
        release = Gtk.Button(label="Release")
        release.add_css_class("luma-button")
        release.add_css_class("small")
        release.add_css_class("canvas-quiet-button")
        release.set_tooltip_text("Release mask · Ctrl+Alt+M")
        release.connect("clicked", lambda *_: self.host.canvas.toggle_mask_selection())
        row.append(release)
        self.body.append(row)

    def update(self, selection: list[LayoutObject]) -> None:
        from .geometry import mask_mode
        masks = _mask_layers(self.host.document, selection)
        mixed, first = _mixed(mask_mode(item) for item in masks)
        if masks and not mixed:
            _select(self.kind, first)

    def _set_kind(self, value: str) -> None:
        ids = [item.id for item in _mask_layers(self.host.document, self.host.document.selection)]
        if ids:
            self.edit("Change mask type", lambda: self.host.editor.set_mask_mode(ids, value))


class BooleanSection(Section):
    """A boolean's operation, changed in place, and Flatten (Ctrl+E)."""

    title, key = "Boolean", "boolean"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.operation = ChoiceGroup(
            (("union", "Union · Alt+Shift+U", "luma-canvas-boolean-union-symbolic"),
             ("subtract", "Subtract · Alt+Shift+S", "luma-canvas-boolean-subtract-symbolic"),
             ("intersect", "Intersect · Alt+Shift+I", "luma-canvas-boolean-intersect-symbolic"),
             ("exclude", "Exclude · Alt+Shift+E", "luma-canvas-boolean-exclude-symbolic")),
            self._set_operation, "Boolean operation")
        self.operation.set_hexpand(True)
        row.append(self.operation)
        flatten = Gtk.Button(label="Flatten")
        flatten.add_css_class("canvas-quiet-button")
        flatten.set_tooltip_text("Flatten · Ctrl+E")
        flatten.connect("clicked", lambda *_: self.host.canvas.flatten_selection())
        row.append(flatten)
        self.body.append(row)

    def update(self, selection: list[LayoutObject]) -> None:
        from .booleans import operation_of
        mixed, first = _mixed(operation_of(item) for item in selection if item.kind == "boolean")
        self.operation.select(None if mixed else first)

    def _set_operation(self, value: str) -> None:
        ids = [item.id for item in self.host.document.selection if item.kind == "boolean"]
        if ids:
            self.edit("Change boolean", lambda: self.host.editor.set_boolean_operation(ids, value))
            self.host.canvas.queue_draw()


POINT_TYPES = (("corner", "Corner"), ("none", "No mirroring"), ("angle", "Mirror angle"),
               ("length", "Mirror angle and length"))


class VectorSection(Section):
    """Vector edit mode: the selected points' place and type, and closing the path."""

    title, key = "Vector", "vector"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        grid = self.grid()
        self.x = _number("X", tooltip="Drag to move the points")
        self.y = _number("Y", tooltip="Drag to move the points")
        self.x.connect("value-changed", lambda _f, value: self._move(x=value))
        self.y.connect("value-changed", lambda _f, value: self._move(y=value))
        grid.attach(self.x, 0, 0, 1, 1)
        grid.attach(self.y, 1, 0, 1, 1)
        self.kind = _dropdown(POINT_TYPES, self._set_kind, "Point type")
        grid.attach(self.kind, 0, 1, 2, 1)
        self.closed = Gtk.CheckButton(label="Closed path")
        self.closed.add_css_class("canvas-clip-content")
        self.closed.connect("toggled", lambda button: None if getattr(button, "canvas_quiet", False)
                            else self.host.canvas.set_vector_closed(button.get_active()))
        grid.attach(self.closed, 0, 2, 2, 1)
        self.note = Gtk.Label(label="Click a point to edit it, or a segment to add one.", xalign=0, wrap=True)
        self.note.add_css_class("canvas-empty-line")
        self.body.append(self.note)

    def update(self, selection: list[LayoutObject]) -> None:
        from . import geometry, nodes
        canvas = self.host.canvas
        item = self.host.document.object(canvas.vector_editing) if canvas.vector_editing else None
        if item is None:
            return
        path = canvas.vector_nodes(item)
        chosen = canvas.vector_selection
        has = bool(chosen)
        for widget in (self.x, self.y, self.kind):
            widget.set_sensitive(has)
        self.note.set_visible(not has)
        if has:
            points = [geometry.to_world(item, *nodes.point(path[index])) for index in chosen]
            _set_number(self.x, [points[0][0]] if len(points) == 1 else [value[0] for value in points])
            _set_number(self.y, [points[0][1]] if len(points) == 1 else [value[1] for value in points])
            kinds = ["corner" if nodes.mirror_of(path[index]) == "none" and nodes.handle(path[index], "in") is None
                     and nodes.handle(path[index], "out") is None else nodes.mirror_of(path[index]) for index in chosen]
            mixed, first = _mixed(kinds)
            if not mixed:
                _select(self.kind, first)
        index = chosen[0] if chosen else 0
        start, end = nodes.subpath_of(path, index) if path else (0, 0)
        self.closed.canvas_quiet = True
        self.closed.set_active(bool(path) and nodes.is_closed(path, start, end))
        self.closed.canvas_quiet = False

    def _move(self, x: float | None = None, y: float | None = None) -> None:
        self.host.canvas.move_vector_nodes_to(x, y)

    def _set_kind(self, value: str) -> None:
        self.host.canvas.set_vector_mirror(value)


class NothingSelectedSection(Section):
    key = "empty"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        note = Gtk.Label(label="Select a layer to see its properties.", xalign=0, wrap=True)
        note.add_css_class("canvas-inspector-note")
        self.body.append(note)
        self.widget.remove_css_class("canvas-section")


EXPORT_FRAME_KINDS = {"frame", "component", "section", "page"}


class ExportRow(Gtk.Box):
    """One export setting: size (1x, 512w), suffix, format, remove (Figma)."""

    def __init__(self, section: "ExportSection") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        from . import export as exports
        self.add_css_class("canvas-export-row")
        self.section = section
        self.index = 0
        self.setting: dict[str, Any] = exports.default_setting()

        from luma_appkit import TextField, PropertyChoice, BarAction
        from luma_appkit.action_center import make_control
        self.set_orientation(Gtk.Orientation.VERTICAL)
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        size_field = TextField("Scale", value="1x")
        self.size = size_field.entry
        self.size.set_width_chars(3)
        self.size.set_max_width_chars(5)
        self.size.set_tooltip_text("Scale such as 2x, or size such as 512w")
        self.size.connect("activate", lambda *_: self._commit_size())
        self._on_leave(self.size, self._commit_size)
        fields.append(size_field)
        suffix_field = TextField("Suffix", placeholder="Optional")
        suffix_field.set_hexpand(True)
        self.suffix = suffix_field.entry
        self.suffix.set_width_chars(4)
        self.suffix.connect("activate", lambda *_: self._commit_suffix())
        self._on_leave(self.suffix, self._commit_suffix)
        fields.append(suffix_field)
        self.append(fields)
        actions = Gtk.Box(spacing=8)
        self.format = PropertyChoice("Format", [(fmt, exports.FORMAT_NAMES[fmt]) for fmt in ("png", "jpeg", "svg", "pdf")],
            current="png", on_change=lambda value: section.change_row(self.index, format=value))
        self.format.set_hexpand(True)
        actions.append(self.format)
        self.remove_button = make_control(BarAction("minus", None,
            lambda: section.remove_row(self.index), tooltip="Remove export setting"), size="tool")
        actions.append(self.remove_button)
        self.append(actions)

        self.presets = None

    @staticmethod
    def _on_leave(entry: Gtk.Entry, callback: Callable[[], None]) -> None:
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: callback())
        entry.add_controller(focus)

    def set_setting(self, index: int, setting: dict[str, Any]) -> None:
        from . import export as exports
        self.index, self.setting = index, dict(setting)
        if not _focused(self.size):
            self.size.set_text(exports.constraint_label(setting))
        if not _focused(self.suffix):
            self.suffix.set_text(setting.get("suffix", ""))
        self.format.set_current(setting.get("format", "png"))
        raster = setting.get("format") in exports.RASTER_FORMATS
        self.size.set_sensitive(raster)
        if not raster and not _focused(self.size):
            self.size.set_text("1x")

    def _show_presets(self) -> None:
        """Figma's sizes, as the app's own menu (checked on the current size)."""
        from luma_appkit import Command, CommandGroup, CommandRegistry, command_popover
        from . import export as exports
        if not self.size.get_sensitive():
            return
        current = exports.constraint_label(self.setting)
        commands = tuple(Command(f"export.size.{text}", text, lambda value=text: self._apply_size(value),
                                 checked=lambda value=text: value == current)
                         for text in exports.SCALE_PRESETS)
        self.presets = command_popover(CommandRegistry((CommandGroup(None, commands),)))
        self.presets.present_for(self.size)

    def _commit_size(self) -> None:
        self._apply_size(self.size.get_text())

    def _apply_size(self, text: str) -> None:
        from . import export as exports
        parsed = exports.parse_constraint(text)
        if parsed is None:
            self.size.set_text(exports.constraint_label(self.setting))
            return
        constraint, value = parsed
        if (constraint, value) == (self.setting.get("constraint"), self.setting.get("value")):
            self.size.set_text(exports.constraint_label(self.setting))
            return
        self.section.change_row(self.index, constraint=constraint, value=value)

    def _commit_suffix(self) -> None:
        text = self.suffix.get_text()
        if text != self.setting.get("suffix", ""):
            self.section.change_row(self.index, suffix=text)


class ExportSection(Section):
    """Figma's Export section: each layer's export rows, and the button that
    writes one file per row per layer. With nothing selected, the page."""

    title, key = "Export", "export"
    PAGE_SETTINGS = "export-page-settings"

    def __init__(self, inspector: "Inspector") -> None:
        super().__init__(inspector)
        self.add_action("luma-canvas-plus-symbolic", "Add export setting", self.add_row)
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.rows_box.add_css_class("canvas-export-rows")
        self.body.append(self.rows_box)
        self.rows: list[ExportRow] = []
        self.empty = Gtk.Label(label="Click + to add an export setting.", xalign=0, wrap=True)
        self.empty.add_css_class("canvas-empty-line")
        self.body.append(self.empty)
        self.mixed = Gtk.Label(label="Click + to replace mixed export settings.", xalign=0, wrap=True)
        self.mixed.add_css_class("canvas-empty-line")
        self.body.append(self.mixed)
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        actions.add_css_class("canvas-export-actions")
        from luma_appkit import BarAction
        from luma_appkit.action_center import make_control
        self.button = make_control(BarAction("share", "Export", self.export, primary=True), size="tool")
        self.button.add_css_class("canvas-export-button")
        actions.append(self.button)
        self.frames_button = Gtk.Button(label="Export all frames")
        self.frames_button.add_css_class("canvas-quiet-button")
        self.frames_button.add_css_class("canvas-export-frames")
        self.frames_button.set_tooltip_text("One file per export setting for every top-level frame on this page")
        self.frames_button.connect("clicked", lambda *_: self.host.export_frames())
        actions.append(self.frames_button)
        self.body.append(actions)
        self.selection: list[LayoutObject] = []
        self.settings: list[dict[str, Any]] = []
        self.is_mixed = False

    # What the rows show.

    def page_settings(self) -> list[dict[str, Any]]:
        from . import export as exports
        stored = self.host.recall(self.PAGE_SETTINGS, None)
        if not isinstance(stored, list):
            return [exports.default_setting()]
        return [row for row in (exports.normalize_setting(value) for value in stored) if row is not None]

    def current(self, selection: list[LayoutObject]) -> tuple[bool, list[dict[str, Any]]]:
        from . import export as exports
        if not selection:
            return False, self.page_settings()
        lists = [exports.export_settings(item) for item in selection]
        mixed = any(value != lists[0] for value in lists[1:])
        return mixed, ([] if mixed else lists[0])

    def update(self, selection: list[LayoutObject]) -> None:
        from . import export as exports
        self.selection = list(selection)
        self.is_mixed, self.settings = self.current(selection)
        while len(self.rows) > len(self.settings):
            self.rows_box.remove(self.rows.pop())
        while len(self.rows) < len(self.settings):
            row = ExportRow(self)
            self.rows.append(row)
            self.rows_box.append(row)
        for index, (row, setting) in enumerate(zip(self.rows, self.settings)):
            row.set_setting(index, setting)
        self.rows_box.set_visible(bool(self.rows))
        self.mixed.set_visible(self.is_mixed)
        self.empty.set_visible(not self.is_mixed and not self.settings)
        has_rows = bool(self.settings) or (self.is_mixed and any(exports.export_settings(item) for item in selection))
        self.button.set_visible(has_rows)
        if len(selection) == 1:
            label = f"Export {selection[0].name}"
        elif selection:
            label = f"Export {len(selection)} layers"
        else:
            label = f"Export {self.page_name()}"
        self.button.set_label(label)
        self.button.set_tooltip_text(label)
        child = self.button.get_child()
        if isinstance(child, Gtk.Label):
            child.set_ellipsize(Pango.EllipsizeMode.END)
        document = self.host.document
        self.frames_button.set_visible(not selection and any(
            item.parent_id is None and item.visible and item.kind in EXPORT_FRAME_KINDS for item in document.objects))

    def page_name(self) -> str:
        document = self.host.document
        page = document.current_page() if hasattr(document, "current_page") else None
        return page.name if page is not None else "page"

    # Edits: one undo step each for layers; the page's rows are a preference.

    def write(self, label: str, settings: list[dict[str, Any]]) -> None:
        from . import export as exports
        if not self.selection:
            self.host.remember(self.PAGE_SETTINGS, [exports.normalize_setting(row) for row in settings])
            self.update([])
            return
        ids = [item.id for item in self.selection]

        def change() -> None:
            document = self.host.document
            for object_id in ids:
                item = document.object(object_id)
                if item is not None:
                    item.metadata = exports.with_export_settings(item.metadata, settings)
        self.edit(label, change)

    def add_row(self) -> None:
        from . import export as exports
        base = [] if self.is_mixed else list(self.settings)
        self.write("Add export setting", [*base, exports.next_setting(base)])

    def remove_row(self, index: int) -> None:
        if not 0 <= index < len(self.settings):
            return
        self.write("Remove export setting", [row for position, row in enumerate(self.settings) if position != index])

    def change_row(self, index: int, **changes: Any) -> None:
        if not 0 <= index < len(self.settings):
            return
        rows = [dict(row) for row in self.settings]
        rows[index].update(changes)
        if rows == self.settings:
            return
        self.write("Change export setting", rows)

    def export(self) -> None:
        if self.selection:
            self.host.export_layers([item.id for item in self.selection])
        else:
            self.host.export_layers(None)


class Inspector(Gtk.Box):
    """Design and Export tabs over a scrolling column of sections."""

    __gtype_name__ = "CanvasInspector"

    def __init__(self, host: Host, segments: Callable) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("canvas-inspector")
        self.host = host
        stored = host.recall("inspector-collapsed", [])
        self.collapsed: set[str] = set(stored) if isinstance(stored, list) else set()
        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        tabs.add_css_class("canvas-panel-tabs-row")
        self.mode = "design"
        self.tabs, self.tab_buttons = segments((("design", "Design"), ("export", "Export")), "design", self._tab_changed)
        tabs.append(self.tabs)
        self.append(tabs)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.scroll.add_css_class("canvas-inspector-scroll")
        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.column.add_css_class("canvas-inspector-column")
        self.scroll.set_child(self.column)
        self.append(self.scroll)
        self.sections: list[Section] = []
        self._signature: tuple | None = None

    def remember_collapsed(self, key: str, collapsed: bool) -> None:
        (self.collapsed.add if collapsed else self.collapsed.discard)(key)
        self.host.remember("inspector-collapsed", sorted(self.collapsed))

    def _tab_changed(self, value: str) -> None:
        self.mode = value
        self.refresh()

    def refresh(self, *, rebuild: bool = False) -> None:
        document = self.host.document
        selection = document.selection
        kinds = tuple(sorted({item.kind for item in selection}))
        masking = bool(selection) and len(_mask_layers(document, selection)) >= len(selection)
        vector = getattr(getattr(self.host, "canvas", None), "vector_editing", None)
        signature = (self.mode, kinds, len(selection) > 1, bool(selection), masking, vector)
        if rebuild or signature != self._signature:
            self._build(selection)
            self._signature = signature
        targets = paint_targets(document, selection)
        for section in self.sections:
            section.update(targets if getattr(section, "paint_section", False) else selection)

    def _build(self, selection: list[LayoutObject]) -> None:
        # Keep keyboard focus out of harm's way: a rebuild only happens when
        # the kinds of selected layers change, never while typing a value.
        while (child := self.column.get_first_child()) is not None:
            self.column.remove(child)
        sections: list[Section] = []
        if self.mode == "export":
            sections.append(ExportSection(self))
        elif not selection:
            sections.append(NothingSelectedSection(self))
        else:
            kinds = {item.kind for item in selection}
            sections += ([] if getattr(self.host, "_creative_active", False) else [HeaderSection(self)])
            sections += [AlignSection(self), PositionSection(self), LayoutSection(self),
                         AppearanceSection(self, radius=bool(kinds & RADIUS_KINDS)),
                         FillSection(self), StrokeSection(self), EffectsSection(self)]
            if "text" in kinds:
                sections.append(TextSection(self))
            if len(_mask_layers(self.host.document, selection)) >= len(selection):
                sections.insert(5, MaskSection(self))
            if kinds == {"boolean"}:
                sections.insert(5, BooleanSection(self))
            if getattr(getattr(self.host, "canvas", None), "vector_editing", None):
                sections.insert(1, VectorSection(self))
        for section in sections:
            self.column.append(section.widget)
        self.sections = sections

    def section(self, key: str) -> Section | None:
        return next((section for section in self.sections if section.key == key), None)
