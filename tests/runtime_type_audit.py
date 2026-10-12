# SPDX-License-Identifier: MPL-2.0
"""Measure every piece of text Canvas draws in its chrome, on the real toolkit.

Prints a table of role, text, font size, weight and family, and fails when
any value, label or row is larger than CANVAS-SPEC 14.2 allows (13px; 11px for
section titles, field labels and units) or not in Figtree.
Run: `python3 tests/runtime_type_audit.py [--table]`.
"""
from __future__ import annotations

import sys
import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from luma_canvas.application import CanvasApplication
from luma_canvas.editing import Editor
from luma_canvas.model import Document
from luma_canvas.window import CanvasWindow

SHOW = "--table" in sys.argv


def settle(seconds: float = 0.4) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(0.01)


def walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


def font(widget) -> tuple[float, int, str]:
    description = widget.get_pango_context().get_font_description()
    size = description.get_size() / Pango.SCALE
    if not description.get_size_is_absolute():
        size = size * 96 / 72
    return round(size, 1), int(description.get_weight()), description.get_family() or ""


def text_of(widget) -> str | None:
    if isinstance(widget, Gtk.Label):
        return widget.get_label()
    if isinstance(widget, Gtk.Text):
        return widget.get_text() or widget.get_placeholder_text() or "(empty)"
    return None


def role(widget) -> str:
    names = []
    cursor = widget
    for _ in range(6):
        if cursor is None:
            break
        classes = [css for css in cursor.get_css_classes() if css.startswith(("canvas-", "luma-"))]
        if classes:
            names.append(classes[0])
        cursor = cursor.get_parent()
    return " < ".join(names[:3]) or type(widget).__name__


def collect(root, area: str, rows: list) -> None:
    for widget in walk(root):
        text = text_of(widget)
        if text is None or not widget.get_mapped() or not text.strip():
            continue
        size, weight, family = font(widget)
        rows.append((area, role(widget), text[:28], size, weight, family))


SMALL = ("luma-numeric-label", "luma-numeric-unit", "canvas-section-title", "canvas-field-label",
         "canvas-group-label", "canvas-empty-line", "canvas-row-meta", "luma-save-status", "canvas-choice-group")
BODY = ("canvas-layer-row", "canvas-page-row", "canvas-document-button")


def limit_for(area: str, where: str, text: str) -> float:
    """CANVAS-SPEC 14.2 as tightened by the owner: layer and page rows and the
    document name 13px; values, dropdowns, tabs and the zoom number 12px;
    labels, units and section titles 11px."""
    if "luma-empty-title" in where:
        return 19
    if any(name in where for name in SMALL):
        return 11
    if any(name in where for name in BODY) or area == "layers" and "luma-connected-buttons" not in where:
        return 13
    if area == "toolbar" and text not in {"Page 1"} and not text.endswith("%"):
        return 13
    return 12


app = CanvasApplication()
assert app.register(None)
window = CanvasWindow(app, show_welcome=False)
window.set_default_size(1440, 900)
window.present()
settle(1.2)
window.editor = Editor(Document.demonstration())
window._refresh_all()
rows: list = []
for name in ("Plate", "Headline"):
    item = next(obj for obj in window.document.objects if obj.name == name)
    window.document.select([item.id])
    window._selection_changed()
    if name == "Plate":
        window.edit("Stroke", lambda: (window.editor.set_property([item.id], "stroke", "#000000"),
                                        window.editor.set_property([item.id], "stroke_width", 14.0)))
    settle()
    collect(window.inspector, f"inspector:{name}", rows)
collect(window.context_bar, "toolbar", rows)
collect(window.layers, "layers", rows)
section = window.inspector.section("text")
if section is not None:
    section._open_settings()
    settle()
    collect(section._settings, "type settings", rows)
    section._settings.popdown()
    section._open_fonts()
    settle()
    collect(section.font_picker, "font picker", rows)
    section.font_picker.popdown()
try:
    fill = next(obj for obj in window.document.objects if obj.name == "Plate")
    window.document.select([fill.id])
    window._selection_changed()
    settle()
    row = window.inspector.section("fill").row
    from luma_canvas import color_picker
    popover = color_picker.open_color_popover(row.swatch, None, lambda *_: None, canvas=window.canvas)
    settle()
    collect(popover, "color picker", rows)
    popover.popdown()
except Exception as error:  # the picker is the interaction stream's; report, do not stop
    print("color picker not measured:", error)

failures = []
for area, where, text, size, weight, family in rows:
    limit = limit_for(area, where, text)
    if size > limit + 0.05:
        failures.append((area, where, text, size, weight, family))
    elif "Figtree" not in family and area not in {"font picker"} and "canvas-font" not in where:
        failures.append((area, where, text, size, weight, family))
if SHOW or failures:
    print(f"{'area':16} {'widget':52} {'text':28} {'px':>5} {'wt':>4} family")
    for area, where, text, size, weight, family in (rows if SHOW else failures):
        print(f"{area:16} {where[:52]:52} {text:28} {size:5} {weight:4} {family}")
print(f"type audit: {len(rows)} strings, {len(failures)} over the scale")
window.destroy()
raise SystemExit(1 if failures else 0)
