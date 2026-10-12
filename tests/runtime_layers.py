# SPDX-License-Identifier: MPL-2.0
"""Layers panel behavior on the real toolkit (CANVAS-SPEC 11.3)."""
from __future__ import annotations

import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

from luma_canvas.application import CanvasApplication
from luma_canvas.documents import new_document
from luma_canvas.editing import Editor
from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.window import CanvasWindow


def settle(predicate=lambda: True, seconds: float = 6) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("did not settle")


def selected_ids(window):
    return window.layers.selected_ids()


app = CanvasApplication()
assert app.register(None)
window = CanvasWindow(app, show_welcome=False)
window.set_default_size(1440, 900)
window.present()
settle(lambda: window._initial_fit_done)

# Frame 1 selected on the canvas is visible and selected in Layers.
frame = window.document.objects[0]
window.document.select([frame.id])
window.canvas.emit("selection-changed")
settle(lambda: frame.id in window.layers.rows_by_id)
assert selected_ids(window) == [frame.id], selected_ids(window)
assert window.layers.rows_by_id[frame.id].get_mapped()
print("PASS: selecting Frame 1 on the canvas selects its row")

# AT-L2: a layer deep inside a collapsed frame is revealed and selected.
window.editor = Editor(Document.demonstration())
window._refresh_all()
settle()
deep = next(item for item in window.document.objects if item.name == "Button label")
window.document.select([deep.id])
window.canvas.emit("selection-changed")
settle(lambda: deep.id in window.layers.rows_by_id)
assert selected_ids(window) == [deep.id]
root = window.layers.row_widgets[deep.id]["row"]
assert "in-scope" in root.get_css_classes()
unrelated = next(item for item in window.document.objects if item.parent_id is None and item.name == "Brand launch poster")
assert "out-of-scope" in window.layers.row_widgets[unrelated.id]["row"].get_css_classes()
print("PASS: AT-L2 deep selection expands ancestors, selects and scopes the row")

# Selecting in Layers selects on the canvas.
window.layers.on_select([unrelated.id])
assert window.document.selected_ids == [unrelated.id]
print("PASS: row selection selects on the canvas")

# AT-L4: rename commits on Enter and Escape cancels.
window.layers.begin_rename(unrelated.id)
settle()
stack = window.layers.row_widgets[unrelated.id]["stack"]
field = stack.get_child_by_name("rename")
field.set_text("Hero")
field.emit("activate")
settle()
assert window.document.object(unrelated.id).name == "Hero"
window.layers.begin_rename(unrelated.id)
settle()
field = stack.get_child_by_name("rename")
field.set_text("Nope")
field._cancel()
settle()
assert window.document.object(unrelated.id).name == "Hero"
print("PASS: AT-L4 inline rename")

# AT-L5: a sweep across four eyes hides all four in one undo entry.
tops = [item.id for item in reversed(window.document.objects) if item.parent_id is None][:4]
depth = len(window.editor.undo_stack)
window._layers_toggled("visible", tops, False)
assert all(not window.document.object(object_id).visible for object_id in tops)
assert len(window.editor.undo_stack) == depth + 1
window._undo()
print("PASS: AT-L5 sweep hides four layers in one undo entry")

# AT-L6: changing a fill keeps every row widget.
before = dict(window.layers.rows_by_id)
accent = next(item for item in window.document.objects if item.name == "Accent")
window.edit("Change fill", lambda: window.editor.set_property([accent.id], "fill", "#ff0000"))
settle()
changed = [window.document.object(key).name for key, row in before.items() if window.layers.rows_by_id.get(key) is not row]
assert not changed, (changed, len(before), len(window.layers.rows_by_id))
print("PASS: AT-L6 rows are updated in place")

# AT-L7: W shows Mixed for widths 100 and 200; Mixed*2 doubles each.
from gi.repository import Gdk
window.editor = Editor(Document.demonstration())
window._refresh_all()
a = next(item for item in window.document.objects if item.name == "Accent")
b = next(item for item in window.document.objects if item.name == "Plate")
window.edit("Size", lambda: (window.editor.set_geometry(a.id, width=100), window.editor.set_geometry(b.id, width=200)))
window.document.select([a.id, b.id])
window._selection_changed()
settle()
width = window.inspector.section("layout").width
assert width.entry.get_text() == "Mixed", width.entry.get_text()
width.entry.set_text("Mixed*2")
from luma_canvas.inspector import _mixed_math
assert _mixed_math(width, Gdk.KEY_Return)
assert (window.document.object(a.id).width, window.document.object(b.id).width) == (200, 400)
print("PASS: AT-L7 Mixed*2 doubles each width")

# AT-L1: 10,000 layers in 100 frames sync quickly.
big = Document.empty()
for index in range(100):
    frame = LayoutObject(new_id("frame"), "frame", f"Frame {index}", None, 0, 0, 100, 100)
    big.objects.append(frame)
    big.objects.extend(LayoutObject(new_id("rectangle"), "rectangle", "Rectangle", frame.id, 0, 0, 1, 1)
                       for _ in range(99))
window.editor = Editor(big)
start = time.perf_counter()
window.layers.sync(window.document)
elapsed = time.perf_counter() - start
settle()
assert elapsed < 0.3, elapsed
print(f"PASS: AT-L1 10,000 layers synced in {elapsed * 1000:.0f} ms")
window.destroy()
