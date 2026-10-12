# SPDX-License-Identifier: MPL-2.0
"""Layers on a nested page, checked against the document on the real toolkit.

Builds frames holding groups, text, a line, an image and nested frames, then
checks the tree row by row (every layer, depth, top-most first), expand and
collapse, selection both ways (multi and deep), focus dimming, rename,
visibility and lock, reorder and reparent through the panel's drop handler,
and that grouping, framing and pasting show up immediately.
"""
from __future__ import annotations

import sys
import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from luma_canvas.application import CanvasApplication
from luma_canvas.editing import Editor
from luma_canvas.layers import layer_rows
from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.window import CanvasWindow

HOLD = "--hold" in sys.argv


def settle(seconds: float = 0.3) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(0.01)


def obj(kind, name, parent=None, x=0, y=0, w=100, h=100, **fields):
    return LayoutObject(new_id(kind), kind, name, parent.id if parent else None, x, y, w, h, **fields)


document = Document.empty("Nested")
if hasattr(document, "ensure_pages"):
    document.ensure_pages()
home = obj("frame", "Home", x=0, y=0, w=390, h=844, fill="#ffffff")
header = obj("group", "Header", home, 0, 0, 390, 80, fill=None)
logo = obj("image", "Logo", header, 16, 16, 48, 48, fill=None)
title = obj("text", "Title", header, 80, 24, 200, 32, fill="#000000", text="Home")
card = obj("frame", "Card", home, 16, 120, 358, 200, fill="#eeeeee")
card_title = obj("text", "Card title", card, 32, 136, 200, 24, fill="#000000", text="Hello")
divider = obj("line", "Divider", card, 32, 180, 300, 0, fill=None, stroke="#999999", stroke_width=1)
settings = obj("frame", "Settings", x=500, y=0, w=390, h=844, fill="#ffffff")
toggle = obj("rectangle", "Toggle", settings, 520, 100, 60, 32, fill="#4f7df3", radius=16)
loose = obj("ellipse", "Badge", x=950, y=0, w=40, h=40, fill="#ff0000")
document.objects.extend([home, header, logo, title, card, card_title, divider, settings, toggle, loose])
document.validate()

app = CanvasApplication()
assert app.register(None)
window = CanvasWindow(app, show_welcome=False)
window.set_default_size(1440, 900)
window.present()
settle(1.2)
window.editor = Editor(document)
window._refresh_all()
settle()
layers = window.layers
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("PASS: " if condition else "FAIL: ") + message)
    if not condition:
        failures.append(message)


def tree_rows(expand_all: bool = False) -> list[tuple[str, int]]:
    tree = layers.model.tree
    if expand_all:
        position = 0
        while position < tree.get_n_items():
            row = tree.get_row(position)
            if row.is_expandable() and not row.get_expanded():
                row.set_expanded(True)
            position += 1
    return [(tree.get_row(i).get_item().props.name, tree.get_row(i).get_depth()) for i in range(tree.get_n_items())]


names = {item.id: item.name for item in window.document.objects}
expected = [(names[object_id], depth) for object_id, depth in layer_rows(window.document)]
check(tree_rows(expand_all=True) == expected, "every layer is listed top-most first at its depth")
print("  ", tree_rows())
check(tree_rows()[0][0] == "Badge" and tree_rows()[1][0] == "Settings", "the last layer in paint order is the first row")

# Collapse and expand.
tree = layers.model.tree
home_row = next(tree.get_row(i) for i in range(tree.get_n_items()) if tree.get_row(i).get_item().props.name == "Home")
home_row.set_expanded(False)
check("Title" not in [name for name, _ in tree_rows()], "collapsing Home hides its layers")
home_row.set_expanded(True)
check("Card" in [name for name, _ in tree_rows()], "expanding Home shows them again")

# Canvas → layers: deep and multi selection.
for node in list(tree_rows()):
    pass
collapse = [tree.get_row(i) for i in range(tree.get_n_items())]
for row in collapse:
    if row.get_expanded():
        row.set_expanded(False)
window.document.select([divider.id])
window.canvas.emit("selection-changed")
settle()
check(layers.selected_ids() == [divider.id], "a deep child selected on the canvas selects its row")
check("Divider" in [name for name, _ in tree_rows()], "its collapsed ancestors expand")
row_widget = layers.row_widgets.get(divider.id, {}).get("row")
check(row_widget is not None and row_widget.get_mapped(), "the row is on screen")
check("out-of-scope" in layers.row_widgets[loose.id]["row"].get_css_classes(), "unrelated layers are dimmed")
check("scope-root" in layers.row_widgets[card.id]["row"].get_css_classes(), "the containing frame is the focus")
window.document.select([title.id, toggle.id])
window.canvas.emit("selection-changed")
settle()
check(sorted(layers.selected_ids()) == sorted([title.id, toggle.id]), "multi-selection on the canvas selects both rows")

# Layers → canvas.
layers.on_select([card.id, loose.id])
check(sorted(window.document.selected_ids) == sorted([card.id, loose.id]), "selecting rows selects on the canvas")

# Rename, visibility, lock.
check(window.rename_layer(card.id, "Hero card"), "rename commits")
settle()
check(layers.row_widgets[card.id]["label"].get_label() == "Hero card", "the row shows the new name at once")
window._layers_toggled("visible", [toggle.id], False)
settle()
check(not window.document.object(toggle.id).visible and "hidden-layer" in layers.row_widgets.get(toggle.id, {"row": Gtk.Box()})["row"].get_css_classes(),
      "hiding dims the row")
window._layers_toggled("locked", [toggle.id], True)
settle()
check(window.document.object(toggle.id).locked, "locking locks the layer")
window._layers_toggled("locked", [toggle.id], False)
window._layers_toggled("visible", [toggle.id], True)

# Reorder and reparent through the drop handler.
window._layers_reordered([card_title.id], divider.id, "below")
settle()
order = [name for name, _ in tree_rows(expand_all=True)]
check(order.index("Card title") > order.index("Divider"), "dropping below a sibling paints it under")
window._layers_reordered([loose.id], settings.id, "into")
settle()
check(window.document.object(loose.id).parent_id == settings.id, "dropping into a frame reparents")
check(("Badge", 1) in tree_rows(expand_all=True), "the reparented layer shows inside the frame")

# Group, frame and paste appear at once.
window.document.select([logo.id, title.id])
window.canvas.emit("selection-changed")
before = len(window.document.objects)
window._group("group")
settle()
group = window.document.selection[0] if window.document.selection else None
check(group is not None and group.kind == "group" and group.id in layers.model.items, "a new group is in Layers immediately")
check(group is not None and layers.selected_ids() == [group.id], "and selected")
window._group("frame")
settle()
framed = window.document.selection[0] if window.document.selection else None
check(framed is not None and framed.kind == "frame" and framed.id in layers.model.items, "a new frame is in Layers immediately")
if hasattr(window.canvas, "copy_selection"):
    window.document.select([toggle.id])
    window.canvas.emit("selection-changed")
    window.canvas.copy_selection()
    settle(0.5)
    window.canvas.paste()
    settle(0.5)
    pasted = [item for item in window.document.objects if item.name.startswith("Toggle") and item.id != toggle.id]
    check(bool(pasted) and all(item.id in layers.model.items for item in pasted), "a pasted layer is in Layers immediately")

if HOLD:
    home_row = next((tree.get_row(i) for i in range(tree.get_n_items()) if tree.get_row(i).get_item().props.name == "Home"), None)
    tree_rows(expand_all=True)
    window.document.select([card_title.id])
    window.canvas.emit("selection-changed")
    settle(0.5)
    open("/out/ready-shot", "w").write("1")
    settle(4)
window.destroy()
print(f"layers nested: {len(failures)} failures")
raise SystemExit(1 if failures else 0)
