# SPDX-License-Identifier: Apache-2.0
"""Canvas's document values in the shared creative property parts.

The fields belong to LumaUI. This module only maps Canvas model values and
undoable edits to their semantic callbacks.
"""

from __future__ import annotations

from gi.repository import Gtk

from .inspector import TEXT_FIT_KEYS, hex_of
from .paint import parse_hex, solid


class CreativeInspector(Gtk.Box):
    def __init__(self, host) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        from luma_appkit import (
            AlignmentActions, PropertyChoice, PropertyColor, PropertyNumber, PropertySection,
        )

        self.host = host
        self.AlignmentActions = AlignmentActions
        self.PropertyChoice = PropertyChoice
        self.PropertyColor = PropertyColor
        self.PropertyNumber = PropertyNumber
        self.PropertySection = PropertySection
        self._signature = None
        self._updates: list[tuple[Gtk.Widget, object]] = []
        self.refresh()

    @staticmethod
    def _focused(widget: Gtk.Widget) -> bool:
        root = widget.get_root()
        current = root.get_focus() if root else None
        while current is not None:
            if current is widget:
                return True
            current = current.get_parent()
        return False

    def _number(self, label: str, value: float, getter, change, **options):
        field = self.PropertyNumber(label, value, on_change=change, **options)
        self._updates.append((field, lambda: field.set_value(getter())))
        return field

    def _choice(self, label: str, choices, current: str, getter, change, **options):
        field = self.PropertyChoice(label, choices, current=current, on_change=change, **options)
        self._updates.append((field, lambda: field.set_current(getter())))
        return field

    def _color(self, label: str, color: str, getter, change):
        palette = [value for style in self.host.document.styles if style.kind == "color"
                   if (value := str(style.value.get("value", ""))) and parse_hex(value) is not None]
        def pick(anchor):
            from .color_picker import open_paint_picker, open_color_popover
            if label in {"Fill", "Text", "Stroke"}:
                open_paint_picker(anchor, self.host.canvas, "stroke" if label == "Stroke" else "fill")
            else:
                def apply(paint, phase):
                    if phase == "change" and paint and paint.get("type", "solid") == "solid":
                        from .paint import Color
                        rgba = Color.from_dict(paint["color"])
                        from gi.repository import Gdk
                        change(Gdk.RGBA(red=rgba.r, green=rgba.g, blue=rgba.b, alpha=rgba.a))
                open_color_popover(anchor, solid(parse_hex(getter())), apply,
                                   canvas=self.host.canvas, title=label, allow_gradients=False)
            return True
        field = self.PropertyColor(label, color, palette=palette, on_change=change, on_pick=pick)
        def update():
            field.set_rgba(self._rgba(getter()))
            if label in {"Fill", "Text", "Stroke"} and len(self.host.document.selection) == 1:
                from .paint import base_paint
                item = self.host.document.selection[0]
                paint = base_paint(item, "stroke" if label == "Stroke" else "fill")
                if paint and paint.get("type") in {"linear", "radial"}:
                    field.set_word(paint["type"].title())
                    preview = Gtk.DrawingArea()
                    def draw(_area, cr, width, height):
                        import cairo
                        from .paint import Color
                        handles = paint.get("handles", [[.5, 0], [.5, 1]])
                        if paint["type"] == "linear":
                            pattern = cairo.LinearGradient(handles[0][0] * width, handles[0][1] * height,
                                handles[1][0] * width, handles[1][1] * height)
                        else:
                            pattern = cairo.RadialGradient(width / 2, height / 2, 0, width / 2, height / 2, max(width, height) / 2)
                        for stop in paint.get("stops", []):
                            color = Color.from_dict(stop["color"])
                            pattern.add_color_stop_rgba(stop.get("position", 0), color.r, color.g, color.b, color.a)
                        cr.set_source(pattern)
                        cr.paint()
                    preview.set_draw_func(draw)
                    field.set_preview(preview)
                else:
                    field.set_preview(None)
        self._updates.append((field, update))
        update()
        return field

    @staticmethod
    def _rgba(value: str):
        from gi.repository import Gdk

        color = Gdk.RGBA()
        color.parse(value or "#000000")
        return color

    def refresh(self) -> None:
        selected = self.host.document.selection
        signature = tuple((item.id, id(item), bool(item.stroke), tuple((e.get("type", e.get("kind")), e.get("visible", True)) for e in item.effects), bool(item.fill), item.kind, bool(item.layout.get('auto')), item.layout.get('direction'), item.layout.get('sizing'), repr(item.constraints), tuple((g.get("pattern"),g.get("axis"),g.get("type"),g.get("visible",True)) for g in item.metadata.get("layout_grids",[])), tuple((p.get('type'),p.get('visible',True)) for p in item.fills), tuple((p.get('type'),p.get('visible',True)) for p in item.strokes), item.component_source_id, tuple(item.style_ids), repr(item.metadata.get("variable_bindings", {})))
                          for item in selected)
        if signature != self._signature:
            self._signature = signature
            self._rebuild(selected)
        else:
            for field, update in self._updates:
                if not self._focused(field):
                    update()

    def _rebuild(self, selected) -> None:
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        self._updates = []
        if not selected:
            return
        align = lambda key: self.host.align({"center": "center-x", "middle": "center-y"}.get(key, key))
        if len(selected) > 1:
            self.append(self.PropertySection("Align", self.AlignmentActions(on_align=align)))
            from .design_controls import action, header_actions, auto_layout_selection, component_selection
            self.append(header_actions(self.PropertySection('Auto layout'), action('plus','Add auto layout',lambda:auto_layout_selection(self.host))))
            self.append(header_actions(self.PropertySection('Component'), action('diamond','Create component',lambda:component_selection(self.host))))
            return
        item = selected[0]
        parent = self.host.document.object(item.parent_id) if item.parent_id else None
        px, py = (parent.x, parent.y) if parent else (0, 0)
        if parent is not None:
            self.append(self.PropertySection("Align", self.AlignmentActions(on_align=align)))

        def geometry(key: str, value: float) -> None:
            offset = px if key == "x" else py if key == "y" else 0
            self.host.edit(f"Change {key.upper()}",
                           lambda: self.host.editor.set_geometry(item.id, **{key: value + offset}),
                           coalesce=True)

        fields = [
            self._number("X", item.x - px, lambda: item.x - px,
                         lambda value: geometry("x", value)),
            self._number("Y", item.y - py, lambda: item.y - py,
                         lambda value: geometry("y", value)),
            self._number("W", item.width, lambda: item.width,
                         lambda value: geometry("width", value), minimum=1),
            self._number("H", item.height, lambda: item.height,
                         lambda value: geometry("height", value), minimum=1),
            self._number("↻", item.rotation, lambda: item.rotation,
                         lambda value: geometry("rotation", value), unit="°", name="Rotation"),
        ]
        radius = None
        if item.kind in {"frame", "rectangle", "image", "component", "instance"}:
            radius = self._number("◜", item.radius, lambda: item.radius,
                                  lambda value: self.host.edit("Change radius", lambda: self.host.editor.set_property(
                                      [item.id], "radius", max(0, value)), coalesce=True),
                                  name="Corner radius", minimum=0)
        self.append(self.PropertySection("Layout", (fields[0], fields[1]),
                                         (fields[2], fields[3]), (fields[4], radius)))

        from .design_controls import layout_controls, component_controls, linked_controls
        layout_controls(self, item)
        component_controls(self, item)
        linked_controls(self, item)
        if item.kind == "text":
            self._text(item)
        self.append(self.PropertySection("Appearance", self._number(
            "Opacity", round(item.opacity * 100), lambda: round(item.opacity * 100),
            lambda value: self.host.edit("Change opacity", lambda: self.host.editor.set_property(
                [item.id], "opacity", max(0, min(1, value / 100))), coalesce=True),
            unit="%", minimum=0, maximum=100)))
        from .design_controls import paint_controls
        paint_controls(self, item, 'fill')
        paint_controls(self, item, 'stroke')
        effects_section = self.PropertySection("Effects", action=("plus", "Add effect", "win.creative-shadow"))
        self.effects_add_button = effects_section.get_first_child().get_last_child()
        self.append(effects_section)
        for index, effect in enumerate(item.effects):
            kind = effect.get("type", effect.get("kind", "drop_shadow"))
            title = {"drop_shadow": "Drop shadow", "inner_shadow": "Inner shadow",
                     "layer_blur": "Layer blur", "background_blur": "Background blur"}.get(kind, kind.replace("_", " ").title())
            def get(key, default, i=index):
                return item.effects[i].get(key, default) if i < len(item.effects) else default
            fields = []
            if kind in {"drop_shadow", "inner_shadow"}:
                fields.append(tuple(self._number(key.upper(), float(get(key, default)),
                    lambda k=key, d=default, g=get: float(g(k, d)),
                    lambda value, k=key, i=index: self._effect(item, i, **{k: value}))
                    for key, default in (("x", 0), ("y", 4))))
            blur = self._number("Blur", float(get("blur", 12)),
                lambda g=get: float(g("blur", 12)),
                lambda value, i=index: self._effect(item, i, blur=max(0, value)), minimum=0)
            if kind in {"drop_shadow", "inner_shadow"}:
                spread = self._number("Spread", float(get("spread", 0)),
                    lambda g=get: float(g("spread", 0)),
                    lambda value, i=index: self._effect(item, i, spread=value))
                fields.append((blur, spread))
                fields.append(self._color("Shadow", str(get("color", "#00000040")),
                    lambda g=get: str(g("color", "#00000040")),
                    lambda rgba, i=index: self._effect(item, i, color=hex_of(rgba, alpha=True))))
                fields.append(self._number("Opacity", (parse_hex(get("color", "#00000040")).a * 100),
                    lambda g=get: parse_hex(g("color", "#00000040")).a * 100,
                    lambda value, i=index: self._effect_opacity(item, i, value),
                    minimum=0, maximum=100, unit="%"))
            else:
                fields.append(blur)
            from luma_appkit import BarAction
            from luma_appkit.action_center import make_control
            section = self.PropertySection(title, *fields)
            header = section.get_first_child()
            shown = bool(get("visible", get("enabled", True)))
            visibility = make_control(BarAction("eye" if shown else "eye-off",
                tooltip="Hide effect" if shown else "Show effect",
                on_activate=lambda i=index, visible=shown: self._effect(item, i, visible=not visible)), size="tool")
            header.append(visibility)
            header.append(make_control(BarAction("trash-2", tooltip="Remove effect",
                on_activate=lambda i=index: self._remove_effect(item, i)), size="tool"))
            self.append(section)

    def _effect_opacity(self, item, index, value):
        color = parse_hex(item.effects[index].get("color", "#00000040"))
        alpha = round(max(0, min(100, value)) * 255 / 100)
        self._effect(item, index, color=f"#{round(color.r * 255):02x}{round(color.g * 255):02x}{round(color.b * 255):02x}{alpha:02x}")

    def _remove_effect(self, item, index):
        self.host.edit("Remove effect", lambda: self.host.editor.set_property(
            [item.id], "effects", [effect for i, effect in enumerate(item.effects) if i != index]))

    def _text(self, item) -> None:
        metadata = item.metadata or {}
        family = str(metadata.get("font_family", "Figtree"))
        from .font_picker import family_names
        families = family_names()
        if family not in families:
            families.append(family)

        def set_meta(key: str, value, label: str) -> None:
            updated = dict(item.metadata or {})
            updated[key] = value
            if key == "align_v" and value != "top" and updated.get("sizing") == "auto_height":
                updated["sizing"] = "fixed"

            def change() -> None:
                self.host.editor.set_property([item.id], "metadata", updated)
                refreshed = self.host.document.object(item.id)
                if (updated.get("sizing", "fixed") != "fixed" and key in TEXT_FIT_KEYS
                        and hasattr(self.host.canvas, "fit_text")):
                    self.host.canvas.fit_text(refreshed)
            self.host.edit(label, change, coalesce=True)

        font = self._choice("Font", [(name, name) for name in families], family,
                            lambda: str(item.metadata.get("font_family", "Figtree")),
                            lambda value: set_meta("font_family", value, "Change font"))
        size = self._number("Size", float(metadata.get("font_size", 32)),
                            lambda: float(item.metadata.get("font_size", 32)),
                            lambda value: set_meta("font_size", max(1, value), "Change font size"), minimum=1)
        weight = self._number("Weight", float(metadata.get("font_weight", 400)),
                              lambda: float(item.metadata.get("font_weight", 400)),
                              lambda value: set_meta("font_weight", int(value), "Change font weight"),
                              minimum=100, maximum=900)
        def preset_weight() -> str:
            weight_value = float(item.metadata.get("font_weight", 400))
            return str(min((400, 500, 600, 700), key=lambda candidate: abs(candidate - weight_value)))

        weight_preset = self._choice("Style", (("400", "Regular"), ("500", "Medium"),
                                               ("600", "Semibold"), ("700", "Bold")),
                                     preset_weight(), preset_weight,
                                     lambda value: set_meta("font_weight", int(value), "Change font weight"))
        weight_mode = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        weight_preset.set_hexpand(True)
        weight.set_hexpand(True)
        weight_mode.append(weight_preset)
        weight_mode.append(weight)
        numeric = Gtk.ToggleButton(label="123")
        numeric.add_css_class("lumaui-creative-panel-button")
        numeric.update_property([Gtk.AccessibleProperty.LABEL], ["Use numeric font weight"])
        numeric.set_tooltip_text("Switch between named and numeric font weights")
        weight_mode.append(numeric)

        def switch_weight(button) -> None:
            weight.set_visible(button.get_active())
            weight_preset.set_visible(not button.get_active())

        numeric.connect("toggled", switch_weight)
        numeric.set_active(int(float(metadata.get("font_weight", 400))) not in {400, 500, 600, 700})
        switch_weight(numeric)

        def pct(key: str, default: float) -> float:
            value = item.metadata.get(key, {})
            return float(value.get("value", default)) / 100 if isinstance(value, dict) else default / 100

        line = self._number("Line", pct("line_height", 120), lambda: pct("line_height", 120),
                            lambda value: set_meta("line_height", {"unit": "percent", "value": value * 100},
                                                   "Change line height"), step=.1, digits=2, minimum=.1)
        track = self._number("Track", pct("letter_spacing", 0), lambda: pct("letter_spacing", 0),
                             lambda value: set_meta("letter_spacing", {"unit": "percent", "value": value * 100},
                                                    "Change letter spacing"), step=.01, digits=3)
        align = self._choice("Align", (("left", "Left", "text-align-start"),
                                        ("center", "Centre", "text-align-center"),
                                        ("right", "Right", "text-align-end")),
                             str(metadata.get("align_h", "left")),
                             lambda: str(item.metadata.get("align_h", "left")),
                             lambda value: set_meta("align_h", value, "Align text"), segments=True)
        vertical = self._choice("Vertical", (("top", "Top"), ("middle", "Middle"), ("bottom", "Bottom")),
                                str(metadata.get("align_v", "top")),
                                lambda: str(item.metadata.get("align_v", "top")),
                                lambda value: set_meta("align_v", value, "Align text vertically"), segments=True)
        sizing = self._choice("Resizing", (("auto_width", "Auto width"),
                                           ("auto_height", "Auto height"), ("fixed", "Fixed size")),
                              str(metadata.get("sizing", "fixed")),
                              lambda: str(item.metadata.get("sizing", "fixed")),
                              lambda value: set_meta("sizing", value, "Change text resizing"))
        color = self._color("Text", item.fill or "#000000", lambda: item.fill or "#000000",
                            lambda rgba: self._paint(item, hex_of(rgba)))
        self.append(self.PropertySection("Text", font, (size, weight_mode),
                                         (line, track), sizing, align, vertical, color))

    def _stroke(self, item, value: str) -> None:
        if parse_hex(value) is None:
            return
        self.host.edit("Change stroke", lambda: self.host.editor.set_paint(
            [item.id], "stroke", solid(parse_hex(value))), coalesce=True)

    def _effect(self, item, index=0, **values) -> None:
        def change() -> None:
            effects = [dict(effect) for effect in item.effects]
            if effects:
                effects[index].update(values)
                self.host.editor.set_property([item.id], "effects", effects)
        self.host.edit("Change shadow", change, coalesce=True)

    def _paint(self, item, value: str) -> None:
        color = parse_hex(value)
        if color is None:
            return
        self.host.edit("Change fill", lambda: self.host.editor.set_paint(
            [item.id], "fill", solid(color)), coalesce=True)
