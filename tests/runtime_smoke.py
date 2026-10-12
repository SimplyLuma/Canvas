# SPDX-License-Identifier: Apache-2.0
"""Runtime contract for the one responsive Canvas binary."""
from __future__ import annotations

import os
import sys
import traceback
import math

import cairo
import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk
from luma_appkit import AppWindow, InputMode, PresentationMode

from luma_canvas.application import CanvasApplication
from luma_canvas.model import Document
from luma_canvas.editing import Editor


callback_errors: list[BaseException] = []


def callback_exception(exc_type, value, tb) -> None:
    callback_errors.append(value)
    traceback.print_exception(exc_type, value, tb)
    application.quit()


sys.excepthook = callback_exception


def inspect() -> bool:
    window = application.get_active_window()
    assert isinstance(window, AppWindow)
    assert window.canvas is not None
    if not window._initial_fit_done:
        # The fit runs on the first frame after allocation; wait for it
        # rather than racing the frame clock (the 10 s timeout still holds).
        return GLib.SOURCE_CONTINUE
    # A manual Fit after allocation must preserve the initial view. Very short
    # mobile windows can legitimately fit below 10%; compare the view itself.
    initial_view = (window.document.zoom, window.document.canvas_x, window.document.canvas_y)
    window._fit_active_artboard()
    allocated_view = (window.document.zoom, window.document.canvas_x, window.document.canvas_y)
    assert all(math.isclose(initial, allocated, abs_tol=1e-6)
               for initial, allocated in zip(initial_view, allocated_view)), (
                   "initial fit used a different allocation", initial_view, allocated_view)
    assert window.document.objects
    assert window.navigation_pane is not None and window.inspector_pane is not None
    assert window.canvas.tool == "select"
    assert window.document.name == "Brand"
    assert window.nav_segment_buttons["layers"].get_active() is True
    assert window.inspector_segment_buttons["design"].get_active() is True
    assert window.body.get_spacing() == 9
    assert window.workspace.get_spacing() == 9
    # Islands are separated by the kit's 9px gutter and nothing else: no
    # Gtk.Paned handle, and the rail and gutters are exactly measured.
    assert window.navigation_gutter.get_width() == 9, window.navigation_gutter.get_width()
    assert window.inspector_gutter.get_width() == 9, window.inspector_gutter.get_width()
    ok, left = window.center_pane.compute_bounds(window)
    ok2, gutter = window.navigation_gutter.compute_bounds(window)
    assert ok and ok2 and left.get_x() == gutter.get_x() + 9
    def walk(widget):
        child = widget.get_first_child()
        while child is not None:
            yield child
            yield from walk(child)
            child = child.get_next_sibling()
    assert not any(isinstance(widget, Gtk.Paned) for widget in walk(window)), "a Gtk.Paned is back"
    assert window.measure(Gtk.Orientation.VERTICAL, -1)[0] <= 360, "Canvas no longer fits a short tile"
    expected_presentation = os.environ.get("LUMA_PRESENTATION_MODE", "windowed")
    expected_input = os.environ.get("LUMA_INPUT_MODE", "pointer")
    assert window.context.presentation == PresentationMode(expected_presentation)
    assert window.context.input_mode == InputMode(expected_input)
    assert window.commands.get("document.save-version").label == "Save a version"
    assert window.commands.get("edit.delete").destructive is True
    assert window.semantic_root.get_children().get_n_items() == 1
    assert window.semantic_document.get_actions().get_n_items() == 3

    grid_before = window.canvas.show_grid
    assert window.commands.invoke("view.grid") is True
    assert window.canvas.show_grid is not grid_before
    assert window.commands.invoke("view.grid") is True

    text = next(item for item in window.document.objects if item.kind == "text")
    zoom, canvas_x, canvas_y = (
        window.document.zoom,
        window.document.canvas_x,
        window.document.canvas_y,
    )
    window.document.zoom, window.document.canvas_x, window.document.canvas_y = 1.0, 0.0, 0.0
    surface_width = math.ceil(text.x + text.width + 4)
    surface_height = math.ceil(text.y + text.height + 4)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, surface_width, surface_height)
    window.canvas._draw_item(cairo.Context(surface), text)
    surface.flush()
    pixels = surface.get_data()
    stride = surface.get_stride()
    x0, y0 = int(text.x), int(text.y)
    x1, y1 = int(text.x + text.width - 2), int(text.y + text.height - 2)
    alphas = [
        pixels[y * stride + x * 4 + 3]
        for y in range(y0, int(text.y + text.height))
        for x in range(x0, int(text.x + text.width))
    ]
    assert any(alphas), "text object produced no glyph pixels"
    assert pixels[y1 * stride + x1 * 4 + 3] == 0, "text object painted its bounding rectangle"
    # Rounded shapes must preserve transparent corners in the actual renderer.
    from luma_canvas.model import LayoutObject
    rounded = LayoutObject("rounded-visual-check", "rectangle", "Rounded",
                           x=0, y=0, width=60, height=60, radius=16, fill=text.fill)
    rounded_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 64, 64)
    window.canvas._draw_item(cairo.Context(rounded_surface), rounded)
    rounded_surface.flush()
    rounded_pixels = rounded_surface.get_data()
    assert rounded_pixels[3] == 0, "rounded object painted its square corner"
    assert rounded_pixels[30 * rounded_surface.get_stride() + 30 * 4 + 3] > 0

    # Numeric X/Y present parent-relative coordinates and edit the same geometry.
    window.document.select([text.id])
    window._selection_changed()
    position = window.inspector.section("position")
    parent = window.document.object(text.parent_id)
    assert position.x.get_value() == round(text.x - parent.x, 2)
    old_x = text.x
    position.x.entry.set_text(str(text.x - parent.x + 12))
    position.x.entry.emit("activate")
    assert window.document.object(text.id).x == old_x + 12
    window._undo()
    assert window.document.object(text.id).x == old_x

    # The inspector updates in place: a change made elsewhere keeps the same
    # widgets, and never overwrites the field that has focus.
    field_before = position.x
    position.x.entry.grab_focus()
    position.x.entry.set_text("typing")
    window.edit("Move", lambda: window.editor.move([text.id], 5, 0))
    assert window.inspector.section("position") is position and position.x is field_before
    assert position.x.entry.get_text() == "typing", "an update overwrote the focused field"
    window._undo()
    window.canvas.grab_focus()

    # Layer rows are never rebuilt for a property change (AT-L6).
    rows_before = dict(window.layers.rows_by_id)
    window.edit("Change fill", lambda: window.editor.set_property([text.id], "fill", "#123456"))
    assert all(window.layers.rows_by_id.get(key) is row for key, row in rows_before.items()), "layer rows were rebuilt"
    window._undo()

    # A scrub is one undo entry, however many values it passes through.
    depth = len(window.editor.undo_stack)
    for value in range(5):
        position.y.emit("value-changed", float(value))
    assert len(window.editor.undo_stack) == depth + 1, (depth, len(window.editor.undo_stack))
    window._undo()

    window.document.zoom, window.document.canvas_x, window.document.canvas_y = zoom, canvas_x, canvas_y
    window.canvas.fit_all()
    window.destroy()
    application.quit()
    return GLib.SOURCE_REMOVE


application = CanvasApplication()
def prepare_fixture(app):
    window = app.get_active_window()
    window.editor = Editor(Document.demonstration())
    window._reveal_editor()
application.connect_after("activate", prepare_fixture)
GLib.timeout_add(250, inspect)
GLib.timeout_add_seconds(
    10,
    lambda: (_ for _ in ()).throw(RuntimeError("runtime smoke timed out")),
)
status = application.run([])
if callback_errors:
    raise SystemExit(1)
raise SystemExit(status)
