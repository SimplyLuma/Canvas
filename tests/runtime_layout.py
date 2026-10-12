# SPDX-License-Identifier: MPL-2.0
"""Measure the window against CANVAS-SPEC 14.1 to 14.3 on the real toolkit.

Run under a display: `python3 tests/runtime_layout.py [width] [height]`.
"""
from __future__ import annotations

import sys
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from luma_canvas.application import CanvasApplication
from luma_canvas.editing import Editor
from luma_canvas.model import Document
from luma_canvas.window import CanvasWindow

WIDTH = int(sys.argv[1]) if len(sys.argv) > 1 else 1440
HEIGHT = int(sys.argv[2]) if len(sys.argv) > 2 else 900


def settle(predicate=lambda: True, seconds: float = 8) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("did not settle")


def bounds(widget, root):
    ok, rect = widget.compute_bounds(root)
    assert ok, widget
    return rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()


def walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


def font_px(label: Gtk.Label) -> float:
    description = label.get_pango_context().get_font_description()
    size = description.get_size() / Pango.SCALE
    return size if description.get_size_is_absolute() else size * 96 / 72


app = CanvasApplication()
assert app.register(None)
window = CanvasWindow(app, show_welcome=False)
window.set_default_size(WIDTH, HEIGHT)
window.present()
settle(lambda: window._initial_fit_done)
window.editor = Editor(Document.demonstration())
window._refresh_all()
plate = next(item for item in window.document.objects if item.name == "Plate")
window.document.select([plate.id])
window._selection_changed()
settle(lambda: window.inspector.section("fill") is not None and window.inspector.section("fill").widget.get_mapped())

failures = []


def check(name, actual, expected, tolerance=0.5):
    if abs(actual - expected) > tolerance:
        failures.append(f"{name}: {actual} (expected {expected})")


if WIDTH >= 1100:
    rail = bounds(window.tool_pane, window)
    layers = bounds(window.layers_slot, window)
    left_gutter = bounds(window.navigation_gutter, window)
    center = bounds(window.center_pane, window)
    right_gutter = bounds(window.inspector_gutter, window)
    inspector = bounds(window.inspector_pane, window)
    check("rail width", rail[2], 40)
    check("rail to layers island", layers[0] - (rail[0] + rail[2]), 9)
    check("layers width", layers[2], 200)
    check("gutter before canvas", center[0] - (layers[0] + layers[2]), 9)
    check("gutter before inspector", inspector[0] - (center[0] + center[2]), 9)
    check("left gutter zone", left_gutter[2], 9)
    check("right gutter zone", right_gutter[2], 9)
    check("inspector width", inspector[2], 248)
    check("rail top aligns with the islands", rail[1], center[1])
    # The rail is on the window ground; Layers, the canvas and the inspector
    # are the three islands.
    assert not any("luma-island" in widget.get_css_classes() for widget in walk(window.tool_pane)), \
        "the tool rail must not be in an island"
    assert "luma-island" in window.layers_slot.get_css_classes(), "Layers must be an island"
islands = [widget for widget in walk(window) if "luma-island" in widget.get_css_classes() and widget.get_mapped()]
if WIDTH >= 1100:
    assert len(islands) == 3, [type(widget).__name__ for widget in islands]
elif WIDTH >= 720:
    assert len(islands) == 2, [type(widget).__name__ for widget in islands]
check("toolbar height", bounds(window.context_bar, window)[3], 40)
assert not any(isinstance(widget, Gtk.Paned) for widget in walk(window)), "Gtk.Paned is back"
assert not any("luma-status-bar" in widget.get_css_classes() for widget in walk(window)), "status bar is back"

# Type scale: nothing above 13px in chrome except empty-state titles; no uppercase.
for widget in walk(window):
    if isinstance(widget, Gtk.Label) and widget.get_mapped() and widget.get_label():
        size = font_px(widget)
        if size > 13.01:
            failures.append(f"label {widget.get_label()!r} is {size:.1f}px")
        text = widget.get_label()
        if len(text) > 3 and text.isupper():
            failures.append(f"label {text!r} is uppercase")
section = window.inspector.section("fill").head.get_first_child().get_child()
check("section title size", font_px(section), 11)

print(f"layout {WIDTH}x{HEIGHT}: {'PASS' if not failures else 'FAIL'}")
for failure in failures:
    print("  " + failure)
window.destroy()
raise SystemExit(1 if failures else 0)
