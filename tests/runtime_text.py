# SPDX-License-Identifier: MPL-2.0
"""Text controls change what the canvas draws (real toolkit, real renderer).

A fixed 300 x 200 text box with one line: Align middle moves the ink to the
vertical middle, Align bottom to the bottom; case, decoration and alignment
from Type settings change the rendered pixels; resizing a box's height makes
it fixed size.
"""
from __future__ import annotations

import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk

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


def ink_columns(window, item) -> tuple[int, int]:
    return _ink(window, item, columns=True)


def ink_rows(window, item) -> tuple[int, int]:
    return _ink(window, item, columns=False)


def _ink(window, item, *, columns: bool) -> tuple[int, int]:
    """First and last pixel rows inside the text box that hold dark ink."""
    canvas = window.canvas
    settle()
    width, height = canvas.get_width(), canvas.get_height()
    snapshot = Gtk.Snapshot()
    type(canvas).do_snapshot(canvas, snapshot)
    node = snapshot.to_node()
    assert node is not None, "the canvas drew nothing"
    renderer = window.get_native().get_renderer()
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
    stride = texture.get_width() * 4
    downloader = __import__("gi.repository.Gdk", fromlist=["Gdk"]).TextureDownloader.new(texture)
    downloader.set_format(__import__("gi.repository.Gdk", fromlist=["Gdk"]).MemoryFormat.R8G8B8A8)
    pixels, stride = downloader.download_bytes()
    pixels = pixels.get_data()
    x0, y0 = canvas.world_to_view(item.x, item.y)
    x1, y1 = canvas.world_to_view(item.x + item.width, item.y + item.height)
    rows = []
    cols = []
    for y in range(max(0, int(y0) + 3), min(texture.get_height(), int(y1) - 3)):
        base = y * stride
        for x in range(max(0, int(x0) + 3), min(texture.get_width(), int(x1) - 3)):
            r, g, b = pixels[base + x * 4], pixels[base + x * 4 + 1], pixels[base + x * 4 + 2]
            if r < 90 and g < 90 and b < 90:
                rows.append(y - y0)
                cols.append(x - x0)
    if columns:
        assert cols, "no ink inside the text box"
        return min(cols), max(cols)
    assert rows, "no ink inside the text box"
    return min(rows), max(rows)


app = CanvasApplication()
assert app.register(None)
window = CanvasWindow(app, show_welcome=False)
window.set_default_size(1440, 900)
window.present()
settle(lambda: window._initial_fit_done)

document = new_document()
text = LayoutObject(new_id("text"), "text", "Label", None, 100, 100, 300, 200, fill="#000000", text="Hello",
                    metadata={"font_size": 24, "sizing": "fixed"})
document.objects.append(text)   # on the white Frame 1, so ink stands out
window.editor = Editor(document)
window._refresh_all()
window.canvas.zoom_to_100() if hasattr(window.canvas, "zoom_to_100") else None
window.document.canvas_x, window.document.canvas_y = 40.0, 40.0
window.document.select([text.id])
window._selection_changed()
settle()
section = window.inspector.section("text")
top = ink_rows(window, window.document.object(text.id))
section.align_v.buttons["middle"].set_active(True)
middle = ink_rows(window, window.document.object(text.id))
section.align_v.buttons["bottom"].set_active(True)
bottom = ink_rows(window, window.document.object(text.id))
zoom = window.document.zoom
print("ink rows top/middle/bottom:", top, middle, bottom, "zoom", zoom)
box = 200 * zoom
assert top[1] - top[0] < box * 0.4, ("ink detection found the background", top)
assert top[0] < box * 0.25, top
assert abs((middle[0] + middle[1]) / 2 - box / 2) < box * 0.12, (middle, box)
assert bottom[1] > box * 0.8, (bottom, box)
assert section.align_v.buttons["bottom"].get_active()
print("PASS: vertical alignment moves the text inside a fixed box")

before = ink_rows(window, window.document.object(text.id))
section._meta("case", "upper", "Change case")
assert window.document.object(text.id).metadata["case"] == "upper"
section._meta("decoration", "underline", "Change decoration")
after = ink_rows(window, window.document.object(text.id))
assert after != before, (before, after)
print("PASS: case and decoration re-render the text", before, after)

# A dragged-out auto height box taller than its text: aligning to the middle
# makes it fixed so the text moves.
section._meta("align_v", "top", "Align text vertically")
section._meta("sizing", "auto_height", "Change resizing")
item = window.document.object(text.id)
window.edit("Grow", lambda: window.editor.set_geometry(item.id, height=200))
section.align_v.buttons["middle"].set_active(True)
assert window.document.object(text.id).metadata["sizing"] == "fixed"
middle_again = ink_rows(window, window.document.object(text.id))
assert middle_again[0] > 40, middle_again
print("PASS: middle alignment on a tall auto height box makes it fixed and moves the text", middle_again)

# Weight: Regular to Bold through the Style menu makes the ink wider.
section._meta_many({"sizing": "fixed", "decoration": "none", "case": "none", "align_v": "top"}, "Reset")
regular = ink_columns(window, window.document.object(text.id))
bold_index = next(index for index, value in enumerate(section.style.canvas_choices) if value == (700, False))
section.style.set_selected(bold_index)
settle()
assert window.document.object(text.id).metadata["font_weight"] == 700
bold = ink_columns(window, window.document.object(text.id))
print("ink columns regular/bold:", regular, bold)
if bold[1] - bold[0] <= regular[1] - regular[0]:
    print("FAIL: font_weight 700 renders no wider than 400 (renderer)")
    WEIGHT_FAILED = True
else:
    WEIGHT_FAILED = False
    print("PASS: bold text is wider than regular")

# Sizing: auto width fits the box to the words, auto height keeps the width
# and fits the height, fixed leaves the box alone.
section.sizing.buttons["auto_width"].set_active(True)
settle()
item = window.document.object(text.id)
print("auto width box:", item.width, item.height)
assert item.width < 200 and item.height < 60, (item.width, item.height)
window.edit("Widen", lambda: window.editor.set_geometry(item.id, width=300))
section.sizing.buttons["auto_height"].set_active(True)
settle()
item = window.document.object(text.id)
assert abs(item.width - 300) < 0.5 and item.height < 60, (item.width, item.height)
section.sizing.buttons["fixed"].set_active(True)
window.edit("Tall", lambda: window.editor.set_geometry(item.id, height=200))
assert window.document.object(text.id).height == 200
print("PASS: auto width, auto height and fixed size change the box")

# Fonts: the picker lists every installed family, and choosing another face
# changes the ink.
from gi.repository import PangoCairo
from luma_canvas.font_picker import FontPicker, styles_for
installed = PangoCairo.FontMap.get_default().list_families()
picker = FontPicker("Figtree", [], lambda name: None)
all_rows = [picker.store.get_item(index) for index in range(picker.store.get_n_items())]
listed = [entry.name for entry in all_rows if entry.header == "All fonts" or all_rows.index(entry) > 0]
all_section = all_rows[next(index for index, entry in enumerate(all_rows) if entry.header == "All fonts"):]
assert len(all_section) == len(installed), (len(all_section), len(installed))
print(f"PASS: the font picker lists all {len(installed)} installed families")
section._meta_many({"font_family": "Figtree", "font_weight": 400, "italic": False}, "Reset font")
figtree = ink_columns(window, window.document.object(text.id))
other = next((family.get_name() for family in installed
              if "mono" in family.get_name().casefold()), None)
if other:
    section._meta("font_family", other, "Change font")
    changed = ink_columns(window, window.document.object(text.id))
    assert changed != figtree, (figtree, changed)
    print(f"PASS: choosing {other} changes the ink", figtree, changed)
faces = styles_for("Figtree")
assert any(value == (700, False) for value, _label in faces), faces
print("PASS: Figtree offers its real faces:", ", ".join(label for _value, label in faces[:6]), "…")

# Double-click editing still starts with the inspector showing the layer.
if hasattr(window.canvas, "begin_text_edit"):
    window.canvas.begin_text_edit(window.document.object(text.id))
    settle()
    window.inspector.refresh()
    assert window.inspector.section("text") is not None
    window.canvas.end_text_edit(commit=True)
    print("PASS: editing in place with the inspector open")
window.destroy()
raise SystemExit(1 if WEIGHT_FAILED else 0)
