# SPDX-License-Identifier: MPL-2.0
"""Interaction tests on real GTK widgets (spec 18.2).

These drive a real `LayoutCanvas` and the real picker in a mapped window,
injecting events through the router's test hook. They need a display and
Luma's toolkit; without them they are skipped.
"""

from __future__ import annotations

import math
import os
import time
import unittest

# The test accessibility backend converts accessible values, so type errors in
# update_property surface here as they do on a desktop with a screen reader.
if os.environ.get("GTK_A11Y", "none") == "none":
    os.environ["GTK_A11Y"] = "test"

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
    HAVE_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None
except (ImportError, ValueError):  # pragma: no cover - no GTK here
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    from luma_canvas.canvas import LayoutCanvas
    from luma_canvas.color_picker import ColorPicker, PaintBinding, pick_screen_color
    from luma_canvas.editing import Editor
    from luma_canvas.model import Document, LayoutObject, new_id
    from luma_canvas.paint import Color, parse_color, solid, to_hex

SHIFT = 1 << 0
CONTROL = 1 << 2
ALT = 1 << 3


def pump(seconds: float = 0.05) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


@unittest.skipUnless(HAVE_DISPLAY, "needs a display and GTK 4")
class CanvasCase(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.empty("Interaction")
        self.editor = Editor(self.document)
        self.canvas = LayoutCanvas(self.editor)
        self.canvas.reduced_motion = True
        self.window = Gtk.Window()
        self.window.set_default_size(1000, 800)
        self.window.set_child(self.canvas)
        self.window.present()
        for _ in range(100):
            pump(0.02)
            if self.canvas.get_width() > 0:
                break
        self.document.zoom, self.document.canvas_x, self.document.canvas_y = 1.0, 0.0, 0.0

    def tearDown(self) -> None:
        self.window.destroy()
        pump(0.02)

    @property
    def doc(self) -> Document:
        return self.editor.document

    def add(self, kind: str, x: float, y: float, w: float, h: float, **fields) -> LayoutObject:
        item = LayoutObject(new_id(kind), kind, fields.pop("name", kind.title()), fields.pop("parent", None),
                            x, y, w, h, **fields)
        self.doc.objects.append(item)
        return item

    def inject(self, kind: str, **event) -> None:
        self.canvas.test_inject({"type": kind, **event})

    def drag(self, start, end, *, state: int = 0, button: int = 1, steps: int = 8) -> None:
        self.inject("press", x=start[0], y=start[1], state=state, button=button)
        for index in range(1, steps + 1):
            t = index / steps
            self.inject("motion", x=start[0] + (end[0] - start[0]) * t,
                        y=start[1] + (end[1] - start[1]) * t, state=state)
        self.inject("release", x=end[0], y=end[1], state=state)
        pump(0.01)


class NavigationTests(CanvasCase):
    def test_AT_N1_ctrl_wheel_zooms_at_pointer(self):
        self.inject("motion", x=500, y=300)
        for _ in range(3):
            self.inject("scroll", dx=0, dy=-1, smooth=False, state=CONTROL)
        self.assertAlmostEqual(self.doc.zoom, 1.728, places=6)
        wx, wy = self.canvas.view_to_world(500, 300)
        self.assertAlmostEqual(wx, 500, delta=0.5)
        self.assertAlmostEqual(wy, 300, delta=0.5)

    def test_AT_N2_pinch_doubles_about_center(self):
        before = self.canvas.view_to_world(400, 400)
        self.inject("zoom", scale=2.0, x=400, y=400)
        self.assertAlmostEqual(self.doc.zoom, 2.0)
        after = self.canvas.view_to_world(400, 400)
        self.assertAlmostEqual(before[0], after[0])
        self.assertAlmostEqual(before[1], after[1])

    def test_AT_N3_middle_drag_pans_with_pen_in_progress(self):
        self.canvas.set_tool("path")
        self.inject("press", x=100, y=100)
        self.inject("release", x=100, y=100)
        self.inject("press", x=200, y=100)
        self.inject("release", x=200, y=100)
        self.drag((300, 300), (420, 260), button=2)
        self.assertEqual((self.doc.canvas_x, self.doc.canvas_y), (120, -40))
        self.assertEqual(len(self.canvas._pen_path), 2)

    def test_AT_N4_space_drag_pans_and_keeps_the_tool(self):
        self.canvas.set_tool("rectangle")
        self.inject("key", name="space")
        self.drag((500, 500), (550, 550))
        self.inject("key", name="space", release=True)
        self.assertEqual((self.doc.canvas_x, self.doc.canvas_y), (50, 50))
        self.assertEqual(self.canvas.tool, "rectangle")
        self.assertEqual(self.doc.objects, [])

    def test_space_released_in_another_window_does_not_keep_panning(self):
        self.canvas.set_tool("rectangle")
        self.inject("key", name="space")
        self.inject("press", x=500, y=500)
        self.inject("focus", entered=False)
        self.assertFalse(self.canvas._space_held)
        self.assertIsNone(self.canvas._press)
        self.inject("focus", entered=True)
        self.drag((500, 500), (550, 550))
        self.assertEqual((self.doc.canvas_x, self.doc.canvas_y), (0, 0))
        self.assertEqual(len(self.doc.objects), 1)

    def test_unfocused_hover_reads_modifiers_from_each_event(self):
        # What the compositor now reports to a hovered window without focus.
        self.inject("focus", entered=False)
        self.inject("motion", x=500, y=300)
        self.inject("scroll", dx=0, dy=-1, smooth=False, state=CONTROL)
        self.assertAlmostEqual(self.doc.zoom, 1.2, places=6)
        before_x, before_y = self.doc.canvas_x, self.doc.canvas_y
        self.inject("scroll", dx=0, dy=1, smooth=False, state=SHIFT)
        self.assertEqual(self.doc.canvas_y, before_y)
        self.assertEqual(self.doc.canvas_x, before_x - 50)

    def test_AT_N5_N6_N7_scroll_axes(self):
        self.inject("scroll", dx=30, dy=0, smooth=True)
        self.assertEqual((self.doc.canvas_x, self.doc.canvas_y), (-30, 0))
        self.inject("scroll", dx=0, dy=1, smooth=False, state=SHIFT)
        self.assertEqual((self.doc.canvas_x, self.doc.canvas_y), (-80, 0))
        self.inject("scroll", dx=1, dy=0, smooth=False)
        self.assertEqual((self.doc.canvas_x, self.doc.canvas_y), (-130, 0))

    def test_AT_N10_shift_zero_is_exactly_one(self):
        self.inject("scroll", dx=0, dy=-2.5, smooth=True, state=CONTROL)
        self.inject("key", name="0", state=SHIFT)
        self.assertEqual(self.doc.zoom, 1.0)

    def test_AT_N8_fit_all(self):
        self.add("rectangle", 0, 0, 4000, 3000)
        self.canvas.zoom_to_fit()
        width, height = self.canvas.get_width(), self.canvas.get_height()
        left, top = self.canvas.world_to_view(0, 0)
        right, bottom = self.canvas.world_to_view(4000, 3000)
        self.assertGreaterEqual(left, 47.5)
        self.assertLessEqual(right, width - 47.5)
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(bottom, height)

    def test_AT_N9_zoom_to_selection_centers(self):
        item = self.add("rectangle", 1200, 900, 200, 100)
        self.doc.select([item.id])
        self.canvas.zoom_to_selection()
        cx, cy = self.canvas.world_to_view(1300, 950)
        self.assertAlmostEqual(cx, self.canvas.get_width() / 2, delta=0.5)
        self.assertAlmostEqual(cy, self.canvas.get_height() / 2, delta=0.5)

    def test_AT_N11_zoom_limits(self):
        for _ in range(80):
            self.inject("scroll", dx=0, dy=-1, smooth=False, state=CONTROL)
        self.assertEqual(self.doc.zoom, 256.0)
        x = self.doc.canvas_x
        self.inject("scroll", dx=0, dy=-1, smooth=False, state=CONTROL)
        self.assertEqual(self.doc.canvas_x, x)

    def test_arrow_keys_pan_with_nothing_selected(self):
        self.inject("key", name="Right")
        self.assertEqual(self.doc.canvas_x, -32)
        self.inject("key", name="Down", state=SHIFT)
        self.assertEqual(self.doc.canvas_y, -320)


class SelectionTests(CanvasCase):
    def test_AT_S1_S2_click_scope_and_escape(self):
        frame = self.add("frame", 0, 0, 400, 600, name="Phone")
        card = self.add("group", 20, 20, 200, 100, parent=frame.id, name="Card")
        label = self.add("text", 30, 30, 120, 30, parent=card.id, name="Label", text="Hi")
        self.inject("press", x=40, y=40)
        self.inject("release", x=40, y=40)
        self.assertEqual(self.doc.selected_ids, [card.id])
        self.canvas._last_click = (0, 0, 0, 0)
        self.inject("press", x=40, y=40)
        self.inject("release", x=40, y=40)
        self.inject("press", x=40, y=40)
        self.inject("release", x=40, y=40)
        self.assertEqual(self.doc.selected_ids, [label.id])
        self.assertIsNone(self.canvas._text)
        self.inject("key", name="Escape")
        self.assertEqual(self.doc.selected_ids, [card.id])
        self.inject("key", name="Escape")
        self.assertEqual(self.doc.selected_ids, [frame.id])
        self.inject("key", name="Escape")
        self.assertEqual(self.doc.selected_ids, [])
        time.sleep(0.5)
        self.inject("press", x=40, y=40, state=CONTROL)
        self.inject("release", x=40, y=40, state=CONTROL)
        self.assertEqual(self.doc.selected_ids, [label.id])

    def test_AT_S4_marquee_touch_and_alt(self):
        frame = self.add("frame", 0, 0, 900, 500)
        a = self.add("rectangle", 100, 100, 100, 100, parent=frame.id)
        b = self.add("rectangle", 300, 100, 100, 100, parent=frame.id)
        self.drag((950, 150), (150, 160))
        self.assertEqual(set(self.doc.selected_ids), {a.id, b.id})
        self.doc.select([])
        self.drag((950, 150), (150, 160), state=ALT)
        self.assertEqual(self.doc.selected_ids, [])

    def test_AT_S5_shift_constrains_move(self):
        item = self.add("rectangle", 100, 100, 100, 100)
        self.canvas.snap = False
        self.drag((150, 150), (150 + 100 * math.cos(math.radians(30)), 150 + 100 * math.sin(math.radians(30))),
                  state=SHIFT)
        moved = self.doc.object(item.id)
        self.assertEqual(moved.y, 100)
        self.assertGreater(moved.x, 100)

    def test_AT_S6_alt_drag_duplicates_in_one_step(self):
        item = self.add("rectangle", 100, 100, 100, 100)
        self.drag((150, 150), (350, 150), state=ALT)
        self.assertEqual(len(self.doc.objects), 2)
        self.assertEqual(self.doc.object(item.id).x, 100)
        self.editor.undo()
        self.assertEqual(len(self.editor.document.objects), 1)
        self.assertEqual(self.editor.document.objects[0].x, 100)

    def test_AT_S7_resize_edge_snaps_and_ctrl_suspends(self):
        self.add("rectangle", 0, 300, 100, 100, name="Sibling")
        item = self.add("rectangle", 200, 300, 100, 100)
        self.doc.select([item.id])
        self.drag((200, 350), (103, 350))
        self.assertAlmostEqual(self.doc.object(item.id).x, 100)
        self.editor.undo()
        item = self.editor.document.object(item.id)
        self.editor.document.select([item.id])
        self.drag((200, 350), (103, 350), state=CONTROL)
        self.assertAlmostEqual(self.editor.document.object(item.id).x, 103)

    def test_snapping_follows_the_shape_not_the_cursor(self):
        self.add("rectangle", 0, 0, 100, 100, name="Anchor")
        item = self.add("rectangle", 0, 300, 300, 100, name="Wide")
        self.doc.select([item.id])
        # Grab far from the center (near the right end) and move so the
        # shape's left edge lands 3px from x=100 while the cursor is far away.
        self.drag((290, 350), (393, 350))
        self.assertAlmostEqual(self.doc.object(item.id).x, 100)

    def test_AT_S9_shift_corner_keeps_ratio_and_alt_center(self):
        item = self.add("rectangle", 100, 100, 200, 100)
        self.doc.select([item.id])
        self.canvas.snap = False
        self.drag((300, 200), (431, 222), state=SHIFT)
        moved = self.doc.object(item.id)
        self.assertAlmostEqual(moved.width / moved.height, 2.0, places=2)
        self.editor.undo()
        item = self.editor.document.object(item.id)
        self.editor.document.select([item.id])
        self.drag((300, 200), (350, 250), state=ALT)
        moved = self.editor.document.object(item.id)
        self.assertAlmostEqual(moved.x + moved.width / 2, 200)
        self.assertAlmostEqual(moved.y + moved.height / 2, 150)

    def test_AT_S10_rotate_with_shift_snaps_to_15(self):
        item = self.add("rectangle", 100, 100, 200, 100)
        self.doc.select([item.id])
        self.drag((310, 90), (330, 180), state=SHIFT)
        rotation = self.doc.object(item.id).rotation
        self.assertNotEqual(rotation, 0)
        self.assertAlmostEqual(rotation % 15, 0)

    def test_AT_S12_nudges_coalesce(self):
        item = self.add("rectangle", 0, 0, 10, 10)
        self.doc.select([item.id])
        self.inject("key", name="Right")
        self.assertEqual(self.doc.object(item.id).x, 1)
        self.inject("key", name="Right", state=SHIFT)
        self.assertEqual(self.doc.object(item.id).x, 11)
        for _ in range(18):
            self.inject("key", name="Right")
        self.assertEqual(len(self.editor.undo_stack), 1)

    def test_AT_S13_drag_into_a_frame_reparents(self):
        frame = self.add("frame", 400, 0, 400, 400)
        item = self.add("rectangle", 50, 50, 100, 100)
        self.canvas.snap = False
        self.drag((100, 100), (600, 200))
        self.assertEqual(self.doc.object(item.id).parent_id, frame.id)
        self.assertEqual((self.doc.object(item.id).x, self.doc.object(item.id).y), (550, 150))

    def test_hit_testing_honors_rotation(self):
        bar = self.add("rectangle", 0, 45, 100, 10, rotation=45)
        self.inject("press", x=20, y=80)
        self.inject("release", x=20, y=80)
        self.assertEqual(self.doc.selected_ids, [])
        self.inject("press", x=50, y=50)
        self.inject("release", x=50, y=50)
        self.assertEqual(self.doc.selected_ids, [bar.id])

    def test_AT_S11_alt_hover_measures(self):
        a = self.add("rectangle", 0, 0, 100, 100)
        self.add("rectangle", 140, 0, 100, 100)
        self.doc.select([a.id])
        self.inject("motion", x=180, y=50, state=ALT)
        self.assertEqual([m.label for m in self.canvas._hover_measurements], ["40"])
        self.inject("motion", x=180, y=50, state=CONTROL)
        self.assertEqual(self.canvas._hover_measurements, [])

    def test_alt_hover_measurement_ends_when_focus_leaves(self):
        a = self.add("rectangle", 0, 0, 100, 100)
        self.add("rectangle", 140, 0, 100, 100)
        self.doc.select([a.id])
        self.inject("motion", x=180, y=50, state=ALT)
        self.assertEqual([m.label for m in self.canvas._hover_measurements], ["40"])
        self.inject("focus", entered=False)
        self.assertEqual(self.canvas._hover_measurements, [])

    def test_frame_label_selects_and_drags_the_frame(self):
        frame = self.add("frame", 100, 100, 400, 300, name="Frame 1")
        self.add("rectangle", 100, 100, 400, 300, parent=frame.id)
        labels = self.canvas.frame_labels()
        area = labels[0][1]
        x, y = area.get_x() + 5, area.get_y() + area.get_height() / 2
        self.assertLess(y, 100)
        self.canvas.snap = False
        self.drag((x, y), (x + 50, y + 20))
        self.assertEqual(self.doc.selected_ids, [frame.id])
        self.assertEqual((self.doc.object(frame.id).x, self.doc.object(frame.id).y), (150, 120))

    def test_frame_label_size_is_zoom_independent(self):
        self.add("frame", 100, 100, 400, 300, name="Frame 1")
        heights = []
        for zoom in (0.5, 1.0, 4.0):
            self.doc.zoom = zoom
            area = self.canvas.frame_labels()[0][1]
            heights.append(area.get_height())
            top_left = self.canvas.world_to_view(100, 100)
            self.assertAlmostEqual(area.get_y() + area.get_height(), round(top_left[1]) - 4, delta=1)
        self.assertEqual(len(set(heights)), 1)


class ShapeTests(CanvasCase):
    def test_AT_O1_shift_draws_a_square_in_the_current_style(self):
        style = self.doc.current_style
        style["shape"]["fill"] = "#4f7df3"
        self.canvas.snap = False
        self.canvas.set_tool("rectangle")
        self.drag((100, 100), (300, 200), state=SHIFT)
        item = self.doc.objects[-1]
        self.assertEqual((item.x, item.y, item.width, item.height), (100, 100, 200, 200))
        self.assertEqual(item.fill, "#4f7df3")
        self.assertEqual(self.canvas.tool, "select")
        self.assertEqual(self.doc.selected_ids, [item.id])
        self.assertEqual(len(self.editor.undo_stack), 1)

    def test_AT_O2_alt_draws_from_center(self):
        self.canvas.snap = False
        self.canvas.set_tool("ellipse")
        self.drag((400, 400), (450, 430), state=ALT)
        item = self.doc.objects[-1]
        self.assertEqual((item.x, item.y, item.x + item.width, item.y + item.height), (350, 370, 450, 430))

    def test_AT_O3_click_creates_100_square(self):
        self.canvas.snap = False
        self.canvas.set_tool("rectangle")
        self.inject("press", x=120, y=130)
        self.inject("release", x=120, y=130)
        item = self.doc.objects[-1]
        self.assertEqual((item.x, item.y, item.width, item.height), (120, 130, 100, 100))

    def test_AT_O4_drawing_inside_a_frame_parents_it(self):
        frame = self.add("frame", 0, 0, 500, 500)
        self.canvas.set_tool("rectangle")
        self.drag((100, 100), (200, 200))
        self.assertEqual(self.doc.objects[-1].parent_id, frame.id)

    def test_AT_O5_radius_handles(self):
        item = self.add("rectangle", 100, 100, 200, 200)
        self.doc.select([item.id])
        self.inject("motion", x=200, y=200)
        handle = self.canvas._radius_handle_points(item, self.canvas.view)[0]
        self.drag(handle, (handle[0] + 20, handle[1] + 20))
        radius = self.doc.object(item.id).radius
        self.assertGreater(radius, 20)
        self.assertEqual(self.doc.object(item.id).corner_radii, [])
        self.inject("motion", x=200, y=200)
        other = self.canvas._radius_handle_points(self.doc.object(item.id), self.canvas.view)[2]
        self.drag(other, (other[0] - 10, other[1] - 10), state=ALT)
        radii = self.doc.object(item.id).corner_radii
        self.assertEqual(len(radii), 4)
        self.assertNotEqual(radii[2], radii[0])

    def test_shift_line_snaps_along_its_ray(self):
        self.add("rectangle", 400, 50, 100, 200, name="Target")
        self.canvas.set_tool("line")
        self.drag((100, 150), (396, 153), state=SHIFT)
        line = self.doc.objects[-1]
        start = (line.x + line.path[0]["x"], line.y + line.path[0]["y"])
        end = (line.x + line.path[1]["x"], line.y + line.path[1]["y"])
        self.assertEqual(start[1], end[1])
        self.assertEqual(max(start[0], end[0]), 400)

    def test_rotate_a_line_from_beyond_its_end(self):
        self.canvas.set_tool("line")
        self.drag((200, 300), (300, 300))
        line = self.doc.objects[-1]
        self.assertEqual(self.doc.selected_ids, [line.id])
        self.inject("motion", x=312, y=300)
        self.assertEqual(self.canvas._hover_handle, ("rotate", "p1"))
        self.drag((312, 300), (250, 362), state=SHIFT)
        moved = self.editor.document.object(line.id)
        ends = [(moved.x + node["x"], moved.y + node["y"]) for node in moved.path]
        self.assertAlmostEqual(ends[0][0], ends[1][0], places=6)
        self.assertAlmostEqual((ends[0][1] + ends[1][1]) / 2, 300, places=6)
        self.assertAlmostEqual((ends[0][0] + ends[1][0]) / 2, 250, places=6)
        self.assertEqual(self.editor.undo_label, "Rotate Line")

    def test_thin_lines_are_easy_to_grab(self):
        line = self.add("line", 100, 100, 200, 0, stroke="#000000", stroke_width=1, fill=None,
                        path=[{"x": 0, "y": 0}, {"x": 200, "y": 0}])
        self.inject("press", x=180, y=105)
        self.inject("release", x=180, y=105)
        self.assertEqual(self.doc.selected_ids, [line.id])

    def test_AT_O6_arrow(self):
        self.canvas.set_tool("arrow")
        self.drag((100, 100), (300, 100))
        item = self.doc.objects[-1]
        self.assertEqual((item.kind, item.end_cap, item.stroke_align), ("line", "line_arrow", "center"))

    def test_AT_R2_rendered_curve_passes_its_midpoint(self):
        path = self.add("path", 100, 100, 100, 100, fill=None, stroke="#000000", stroke_width=4,
                        path=[{"x": 0, "y": 0, "out_x": 0, "out_y": 100},
                              {"x": 100, "y": 0, "in_x": 100, "in_y": 100}])
        pixels = render(self.canvas)
        stride, data = pixels
        def alpha_at(x, y):
            return data[y * stride + x * 4 + 3]
        self.assertGreater(alpha_at(150, 175), 0)       # the curve at t=0.5
        self.assertEqual(alpha_at(150, 100), 0)         # the chord midpoint
        del path

    def test_AT_V7_closed_pen_path_renders_closed(self):
        self.canvas.set_tool("path")
        for x, y in ((100, 100), (300, 100), (300, 300)):
            self.inject("press", x=x, y=y)
            self.inject("release", x=x, y=y)
            time.sleep(0.45)
        self.inject("press", x=100, y=100)
        self.inject("release", x=100, y=100)
        item = self.doc.objects[-1]
        self.assertTrue(item.path[-1].get("closed"))
        self.assertEqual((item.x, item.y), (100, 100))
        self.assertEqual(self.canvas.tool, "select")


def render(canvas):
    from luma_canvas.scene import rect
    snapshot = Gtk.Snapshot()
    canvas._snapshot_scene(snapshot, canvas.get_width(), canvas.get_height(), canvas.view)
    node = snapshot.to_node()
    renderer = canvas.get_native().get_renderer()
    texture = renderer.render_texture(node, rect(0, 0, canvas.get_width(), canvas.get_height()))
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = downloader.download_bytes()
    return stride, data.get_data()


def render_scaled(canvas, scale):
    from luma_canvas.scene import rect
    width, height = canvas.get_width(), canvas.get_height()
    snapshot = Gtk.Snapshot()
    snapshot.scale(scale, scale)
    canvas._scene_dirty = True
    canvas._snapshot_scene(snapshot, width, height, canvas.view, scale=scale)
    node = snapshot.to_node()
    renderer = canvas.get_native().get_renderer()
    texture = renderer.render_texture(node, rect(0, 0, width * scale, height * scale))
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = downloader.download_bytes()
    return stride, data.get_data(), texture.get_width(), texture.get_height()


class SceneCacheTests(CanvasCase):
    """The canvas composes each top-level layer once and rebuilds only what
    changed, and what is drawn always matches the document."""

    WHITE, RED, BLUE, GREEN = (255, 255, 255), (0xe0, 0x20, 0x80), (0x20, 0x60, 0xe0), (0x20, 0xa0, 0x40)

    def setUp(self):
        super().setUp()
        self.frames = []
        self.children = []
        for index, color in enumerate(("#e02080", "#2060e0", "#20a040")):
            frame = self.add("frame", index * 300, 0, 200, 200, fill="#ffffff", name=f"Frame {index}")
            child = self.add("rectangle", index * 300 + 20, 20, 40, 40, parent=frame.id, fill=color,
                             name=f"Child {index}")
            self.frames.append(frame)
            self.children.append(child)
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        self.canvas.queue_draw()

    def pixel(self, rendered, x, y):
        stride, data, _w, _h = rendered
        offset = y * stride + x * 4
        return tuple(data[offset:offset + 3])

    def entries(self):
        return {frame.id: self.canvas.renderer._roots.get(frame.id) for frame in self.frames}

    def test_an_edit_rebuilds_only_its_top_level_layer(self):
        render_scaled(self.canvas, 1.0)
        before = self.entries()
        self.assertTrue(all(before.values()))
        self.editor.move([self.children[1].id], 100, 0)
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        after = self.entries()
        self.assertIs(after[self.frames[0].id], before[self.frames[0].id])
        self.assertIs(after[self.frames[2].id], before[self.frames[2].id])
        self.assertIsNot(after[self.frames[1].id], before[self.frames[1].id])
        self.assertEqual(self.pixel(rendered, 440, 40), self.BLUE)
        self.assertEqual(self.pixel(rendered, 340, 40), self.WHITE)

    def test_undo_and_redo_redraw_at_once(self):
        render_scaled(self.canvas, 1.0)
        self.editor.move([self.children[0].id], 100, 0)
        self.canvas.queue_draw()
        self.assertEqual(self.pixel(render_scaled(self.canvas, 1.0), 140, 40), self.RED)
        self.editor.undo()
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 40, 40), self.RED)
        self.assertEqual(self.pixel(rendered, 140, 40), self.WHITE)
        self.editor.redo()
        self.canvas.queue_draw()
        self.assertEqual(self.pixel(render_scaled(self.canvas, 1.0), 140, 40), self.RED)

    def test_new_and_reparented_layers_show_at_once(self):
        render_scaled(self.canvas, 1.0)
        extra = self.add("rectangle", 120, 120, 40, 40, parent=self.frames[0].id, fill="#2060e0", name="Extra")
        self.canvas.queue_draw()
        self.assertEqual(self.pixel(render_scaled(self.canvas, 1.0), 140, 140), self.BLUE)
        with self.editor.transaction("Reparent"):
            moved = self.editor.document.object(self.children[0].id)
            moved.parent_id = self.frames[2].id
            moved.x = 700
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 720, 40), self.RED)
        self.assertEqual(self.pixel(rendered, 40, 40), self.WHITE)
        self.assertEqual(self.pixel(rendered, 140, 140), self.BLUE, extra.name)

    def test_a_removed_layer_disappears(self):
        render_scaled(self.canvas, 1.0)
        with self.editor.transaction("Delete"):
            self.editor.document.objects = [item for item in self.editor.document.objects
                                            if item.id != self.children[2].id]
        self.canvas.queue_draw()
        self.assertEqual(self.pixel(render_scaled(self.canvas, 1.0), 640, 40), self.WHITE)

    def test_a_zoom_change_rebuilds_within_the_frame_budget_and_converges(self):
        render_scaled(self.canvas, 1.0)
        self.canvas.renderer.FRAME_BUDGET_MS = 0.0
        self.doc.zoom = 1.5
        stale_frames = 0
        for _ in range(10):
            render_scaled(self.canvas, 1.0)
            if not self.canvas.renderer.stale:
                break
            stale_frames += 1
        self.assertFalse(self.canvas.renderer.stale)
        grids = {entry.grid for entry in self.entries().values()}
        self.assertEqual(grids, {1.5})
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, round(320 * 1.5 + 10), round(20 * 1.5 + 10)), self.BLUE)


class PixelTests(CanvasCase):
    """Abutting edges and frame edges show only their own colors (WYSIWYG)."""

    RED = (0xe0, 0x20, 0x80)
    BLUE = (0x20, 0x60, 0xe0)

    def pixel(self, rendered, x, y):
        stride, data, _w, _h = rendered
        offset = y * stride + x * 4
        return tuple(data[offset:offset + 3]), data[offset + 3]

    def test_clip_content_hides_or_shows_overflow(self):
        frame = self.add("frame", 100, 100, 200, 200, fill="#ffffff")
        self.add("rectangle", 250, 150, 200, 50, parent=frame.id, fill="#e02080", name="Overflow")
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        clipped = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(clipped, 350, 175)[1], 0)
        self.inject("press", x=350, y=175)
        self.inject("release", x=350, y=175)
        self.assertEqual(self.doc.selected_ids, [])
        self.editor.set_property([frame.id], "clip_content", False)
        shown = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(shown, 350, 175), (self.RED, 255))
        self.inject("press", x=350, y=175)
        self.inject("release", x=350, y=175)
        self.assertEqual(self.editor.document.selection[0].name, "Overflow")

    def test_group_stroke_draws_on_every_child(self):
        rects = [self.add("rectangle", 100 + index * 150, 100, 80, 80, fill="#2060e0") for index in range(3)]
        self.doc.select([rect.id for rect in rects])
        self.canvas.group_selection()
        group = self.editor.document.selection[0]
        self.editor.set_property([group.id], "stroke", "#e02080")
        self.editor.set_property([group.id], "stroke_width", 14)
        self.editor.set_property([group.id], "stroke_align", "outside")
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        rendered = render_scaled(self.canvas, 1.0)
        for index in range(3):
            left = 100 + index * 150
            self.assertEqual(self.pixel(rendered, left - 7, 140), (self.RED, 255), index)
            self.assertEqual(self.pixel(rendered, left + 10, 140), (self.BLUE, 255), index)

    def test_frame_stroke_renders_in_every_alignment(self):
        frame = self.add("frame", 100, 100, 200, 200, fill="#ffffff", stroke="#e02080", stroke_width=10)
        self.add("rectangle", 100, 100, 200, 200, parent=frame.id, fill="#2060e0")
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        for clip in (True, False):
            for align, inside, outside in (("inside", (self.RED, 255), None), ("center", (self.RED, 255), (self.RED, 255)),
                                           ("outside", (self.BLUE, 255), (self.RED, 255))):
                item = self.editor.document.object(frame.id)
                item.stroke_align = align
                item.clip_content = clip
                rendered = render_scaled(self.canvas, 1.0)
                self.assertEqual(self.pixel(rendered, 103, 200), inside, (clip, align))
                if outside is not None:
                    self.assertEqual(self.pixel(rendered, 96, 200), outside, (clip, align))
                else:
                    self.assertEqual(self.pixel(rendered, 96, 200)[1], 0, (clip, align))

    def test_clip_content_clips_child_strokes_and_shadows_and_updates_at_once(self):
        frame = self.add("frame", 100, 100, 200, 200, fill="#ffffff", stroke="#2060e0", stroke_width=4,
                         stroke_align="inside")
        self.add("rectangle", 250, 150, 50, 50, parent=frame.id, fill="#e02080", stroke="#e02080",
                 stroke_width=14, stroke_align="outside",
                 effects=[{"type": "drop_shadow", "x": 40, "y": 0, "blur": 0, "spread": 0, "color": "#e02080"}])
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        self.canvas.queue_draw()
        clipped = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(clipped, 310, 175)[1], 0)          # child stroke past the edge
        self.assertEqual(self.pixel(clipped, 330, 175)[1], 0)          # child shadow past the edge
        self.assertEqual(self.pixel(clipped, 298, 250), (self.BLUE, 255))   # frame's own stroke on top
        self.editor.set_property([frame.id], "clip_content", False)
        self.canvas.queue_draw()
        shown = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(shown, 310, 175), (self.RED, 255))
        self.assertEqual(self.pixel(shown, 330, 175), (self.RED, 255))
        # Through the live widget path too: a redraw after the toggle, no rebuild forced.
        self.canvas._scene_dirty = False
        self.editor.set_property([frame.id], "clip_content", True)
        self.canvas.queue_draw()
        snapshot = Gtk.Snapshot()
        self.canvas.do_snapshot(snapshot)
        from luma_canvas.scene import rect as gr
        texture = self.canvas.get_native().get_renderer().render_texture(snapshot.to_node(), gr(0, 0, 400, 400))
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = downloader.download_bytes()
        self.assertEqual(data.get_data()[175 * stride + 310 * 4 + 3], 0)

    def test_clip_content_matrix_on_this_renderer(self):
        """Every child kind crossing a clipping frame's edge, the frame's own stroke in
        every alignment, square and rounded, at 1x and a fractional scale: nothing
        paints past the frame except the frame's own stroke, which draws over the
        children (Figma). Run under GSK_RENDERER=opengl and vulkan as well."""
        pink = "#e02080"
        for radius in (0.0, 30.0):
            for align, reach in (("inside", 0), ("center", 5), ("outside", 10)):
                self.doc.objects.clear()
                frame = self.add("frame", 100, 100, 300, 200, fill="#ffffff", stroke="#2060e0", stroke_width=10,
                                 stroke_align=align, radius=radius)
                self.add("rectangle", 330, 120, 150, 40, parent=frame.id, fill=pink, stroke=pink, stroke_width=16,
                         stroke_align="outside",
                         effects=[{"type": "drop_shadow", "x": 0, "y": 30, "blur": 4, "spread": 0, "color": pink}])
                self.add("line", 350, 200, 150, 0, parent=frame.id, fill=None, stroke=pink, stroke_width=12,
                         path=[{"x": 0, "y": 0}, {"x": 150, "y": 0}])
                self.add("text", 60, 240, 200, 50, parent=frame.id, fill=pink, text="WWWW",
                         metadata={"font_size": 40, "font_weight": 800, "sizing": "fixed"})
                nested = self.add("frame", 250, 260, 120, 90, parent=frame.id, fill=pink, stroke=pink,
                                  stroke_width=14, stroke_align="outside", clip_content=False)
                self.add("rectangle", 300, 300, 150, 80, parent=nested.id, fill=pink)
                group = self.add("group", 60, 120, 80, 80, parent=frame.id, fill=None)
                self.add("ellipse", 60, 120, 80, 80, parent=group.id, fill=pink, stroke=pink, stroke_width=20,
                         stroke_align="center")
                self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
                for scale in (1.0, 1.25):
                    rendered = render_scaled(self.canvas, scale)
                    stride, data, width, height = rendered
                    leaks = []
                    for wy in range(60, 420, 3):
                        for wx in range(20, 540, 3):
                            if 100 - reach - 1 <= wx <= 400 + reach + 1 and 100 - reach - 1 <= wy <= 300 + reach + 1:
                                continue
                            px, py = int(wx * scale), int(wy * scale)
                            if px >= width or py >= height:
                                continue
                            offset = py * stride + px * 4
                            if data[offset + 3] != 0:
                                leaks.append((wx, wy))
                    self.assertEqual(leaks[:5], [], (radius, align, scale))
                    # The frame's stroke draws over the children where they cross its edge.
                    if align != "outside":
                        band = 400 - (5 if align == "inside" else 0)
                        offset = int(140 * scale) * stride + int(band * scale) * 4
                        self.assertEqual(tuple(data[offset:offset + 3]), self.BLUE, (radius, align, scale))

    def test_frame_stroke_stays_on_top_while_moving_a_child_and_after_undo(self):
        frame = self.add("frame", 100, 100, 200, 200, fill="#ffffff", stroke="#2060e0", stroke_width=10,
                         stroke_align="inside")
        child = self.add("rectangle", 150, 150, 50, 50, parent=frame.id, fill="#e02080")
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        self.doc.select([child.id])
        self.inject("press", x=175, y=175)
        for step in range(1, 9):
            self.inject("motion", x=175 + step * 15, y=175)
        # Mid-drag (the pointer is still over the frame): clipped, stroke on top.
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 296, 175), (self.BLUE, 255))
        self.assertEqual(self.pixel(rendered, 310, 175)[1], 0)
        self.inject("release", x=295, y=175)
        self.assertEqual(self.editor.document.object(child.id).parent_id, frame.id)
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 310, 175)[1], 0)
        self.editor.undo()
        self.canvas.replace_editor(self.editor)
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 175, 175), (self.RED, 255))
        self.assertEqual(self.pixel(rendered, 104, 175), (self.BLUE, 255))

    def test_inspector_clip_toggle_writes_every_selected_frame(self):
        a = self.add("frame", 0, 0, 100, 100)
        b = self.add("frame", 200, 0, 100, 100)
        self.doc.select([a.id, b.id])
        self.editor.set_property([a.id, b.id], "clip_content", False)
        self.assertEqual([self.editor.document.object(i).clip_content for i in (a.id, b.id)], [False, False])

    def test_abutting_rectangles_have_no_seam(self):
        self.add("rectangle", 0, 0, 100, 60, fill="#e02080")
        self.add("rectangle", 100, 0, 100, 60, fill="#2060e0")
        for zoom in (1.0, 1.37, 2.5):
            for scale in (1.0, 1.25):
                self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = zoom, 10.3, 20.6
                rendered = render_scaled(self.canvas, scale)
                grid = zoom * scale
                seam = round((100 * zoom + round(10.3 * scale) / scale) * scale)
                top = round((20.6 * scale)) + 2
                bottom = round((20.6 * scale) + 60 * grid) - 2
                for x in (seam - 1, seam):
                    for y in range(top, bottom, 3):
                        color, alpha = self.pixel(rendered, x, y)
                        self.assertIn(color, (self.RED, self.BLUE), (zoom, scale, x, y, color))
                        self.assertEqual(alpha, 255)

    def test_frame_edges_show_no_ground(self):
        frame = self.add("frame", 50.4, 40.2, 300, 200, fill="#ffffff")
        self.add("rectangle", 50.4, 40.2, 300, 30, parent=frame.id, fill="#e02080")
        for zoom in (1.0, 0.73, 1.9):
            self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = zoom, 7.7, 3.1
            rendered = render_scaled(self.canvas, 1.0)
            left = round(round(50.4 * zoom) + 8)
            top = round(round(40.2 * zoom) + 3)
            right = round((50.4 + 300) * zoom) + 8 - 1
            bottom = round((40.2 + 200) * zoom) + 3 - 1
            # Top edge row belongs to the child bar; left, right and bottom to the frame.
            for x in range(left, right, 7):
                color, alpha = self.pixel(rendered, x, top)
                self.assertEqual((color, alpha), (self.RED, 255), (zoom, x))
            for y in range(top + round(30 * zoom) + 2, bottom, 7):
                for x in (left, right):
                    color, alpha = self.pixel(rendered, x, y)
                    self.assertEqual((color, alpha), ((255, 255, 255), 255), (zoom, x, y))


def render_overlay(canvas):
    from luma_canvas.scene import rect
    snapshot = Gtk.Snapshot()
    canvas.do_snapshot(snapshot)
    node = snapshot.to_node()
    texture = canvas.get_native().get_renderer().render_texture(node, rect(0, 0, canvas.get_width(), canvas.get_height()))
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = downloader.download_bytes()
    return stride, data.get_data()


class PickerTests(CanvasCase):
    def make_picker(self, paint):
        self.events = []
        picker = ColorPicker(paint, lambda value, phase: self.events.append((phase, value)), canvas=self.canvas)
        return picker

    def test_AT_P3_hue_survives_black(self):
        picker = self.make_picker(solid(parse_color("#ff0000")))
        picker.set_hsv(brightness=0.0)
        picker.set_hsv(brightness=1.0)
        self.assertEqual(to_hex(picker.color()), "#ff0000")

    def test_AT_P4_rgb_text_in_the_hex_field(self):
        picker = self.make_picker(solid(parse_color("#ffffff")))
        picker._text_committed(picker.fields[0], "rgb(10 20 30 / 50%)")
        color = Color.from_dict(self.events[-1][1]["color"])
        self.assertEqual(color.bytes(), (10, 20, 30))
        self.assertAlmostEqual(color.a, 0.5, places=2)
        self.assertEqual([phase for phase, _ in self.events], ["begin", "change", "end"])

    def test_AT_P5_hsb_scrub_ninety_px_adds_45(self):
        picker = self.make_picker(solid(parse_color("#ff0000")))
        picker._model_chosen(picker.model_button, "HSB")
        hue = picker.hue
        picker._channel_scrubbed(picker.fields[0], "begin", hue, 0)
        picker._channel_scrubbed(picker.fields[0], "change", hue + round(90 / 2.0) * 1.0, 0)
        picker._channel_scrubbed(picker.fields[0], "end", hue, 0)
        self.assertAlmostEqual(picker.hue, hue + 45)

    def test_AT_P6_gradient_stops(self):
        picker = self.make_picker(solid(parse_color("#ff0000")))
        picker.type_buttons["linear"].set_active(True)
        self.assertEqual(len(picker.paint["stops"]), 2)
        picker.paint["stops"][1]["color"] = Color(0, 0, 1, 1).to_dict()
        index = picker.add_stop(0.5)
        middle = Color.from_dict(picker.paint["stops"][index]["color"])
        self.assertAlmostEqual(middle.r, 0.5, places=2)
        self.assertAlmostEqual(middle.b, 0.5, places=2)
        picker.remove_stop(index)
        self.assertEqual(len(picker.paint["stops"]), 2)

    def test_AT_P2_field_drag_is_one_undo_step(self):
        item = self.add("rectangle", 0, 0, 100, 100, fill="#d9d9d9")
        self.doc.select([item.id])
        binding = PaintBinding(self.canvas, "fill")
        picker = ColorPicker(binding.paint(), binding, canvas=self.canvas)
        picker.field.emit("interaction", "begin")
        for step in range(20):
            picker.set_hsv(saturation=step / 20, brightness=1 - step / 40)
            picker.field.emit("interaction", "change")
        picker.field.emit("interaction", "end")
        self.assertEqual(len(self.editor.undo_stack), 1)
        self.assertEqual(self.editor.undo_label, "Change fill")
        self.assertNotEqual(self.editor.document.object(item.id).fill, "#d9d9d9")
        self.editor.undo()
        self.assertEqual(self.editor.document.object(item.id).fill, "#d9d9d9")

    def test_picker_emits_no_toolkit_assertions(self):
        import warnings
        from luma_canvas.color_picker import open_paint_picker
        item = self.add("rectangle", 0, 0, 100, 100, fill="#336699")
        self.doc.select([item.id])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            popover = open_paint_picker(self.canvas, self.canvas, "fill")
            pump(0.2)
            picker = popover.picker
            for slider, values in ((picker.hue_slider, (10.0, 120.0, 300.0)), (picker.alpha_slider, (0.0, 90.0))):
                slider.emit("interaction", "begin")
                for value in values:
                    if slider.channel == "hue":
                        picker.set_hsv(hue=value)
                    else:
                        picker.set_alpha(value / 100)
                    slider.emit("interaction", "change")
                slider.emit("interaction", "end")
            picker.field.emit("interaction", "begin")
            picker.set_hsv(saturation=0.3, brightness=0.4)
            picker.field.emit("interaction", "change")
            picker.field.emit("interaction", "end")
            pump(0.1)
            popover.popdown()
            pump(0.1)
        problems = [str(entry.message) for entry in caught if "assertion" in str(entry.message)
                    or "critical" in str(entry.message).lower()]
        self.assertEqual(problems, [])

    def test_AT_C1_chip_picker_sets_the_next_rectangle(self):
        binding = PaintBinding(self.canvas, "fill")
        picker = ColorPicker(binding.paint(), binding, canvas=self.canvas)
        picker._text_committed(picker.fields[0], "4F7DF3")
        self.canvas.snap = False
        self.canvas.set_tool("rectangle")
        self.drag((100, 100), (200, 200))
        self.assertEqual(self.doc.objects[-1].fill, "#4f7df3")

    def test_AT_C7_d_resets_and_x_swaps(self):
        self.doc.current_style["shape"]["fill"] = "#123456"
        self.inject("key", name="d")
        self.assertEqual(self.editor.document.current_style["shape"]["fill"], "#d9d9d9")
        self.inject("key", name="x")
        self.assertEqual(self.editor.document.current_style["front"], "stroke")
        self.inject("key", name="X", state=SHIFT)
        style = self.editor.document.current_style
        self.assertEqual((style["shape"]["fill"], style["line"]["stroke"]), ("#000000", "#d9d9d9"))

    def test_AT_E1_E4_eyedropper_samples_the_rendered_canvas(self):
        self.add("rectangle", 100, 100, 200, 200, fill="#22aa44")
        self.canvas.queue_draw()
        pump(0.1)
        self.inject("key", name="Escape")
        self.canvas.set_tool("dropper")
        self.inject("press", x=150, y=150)
        self.inject("release", x=150, y=150)
        self.assertEqual(self.editor.document.current_style["shape"]["fill"], "#22aa44")
        self.assertEqual(self.canvas.tool, "select")
        self.canvas.set_tool("rectangle")
        self.drag((500, 500), (560, 560))
        self.assertEqual(self.editor.document.objects[-1].fill, "#22aa44")


class TextEditingTests(CanvasCase):
    def test_double_click_edits_types_and_commits_one_step(self):
        text = self.add("text", 100, 100, 300, 60, text="Hello", fill="#000000",
                        metadata={"font_size": 24, "font_family": "Figtree"})
        self.doc.select([text.id])
        self.inject("press", x=105, y=115)
        self.inject("release", x=105, y=115)
        self.inject("press", x=105, y=115)
        self.inject("release", x=105, y=115)
        self.assertIsNotNone(self.canvas._text)
        self.assertEqual(self.canvas._text.selected_text(), "Hello")
        self.inject("key", name="End")
        self.inject("type", text=" world")
        self.assertEqual(self.doc.object(text.id).text, "Hello world")
        self.inject("key", name="BackSpace")
        self.inject("key", name="Left", state=SHIFT)
        self.inject("type", text="D")
        self.assertEqual(self.doc.object(text.id).text, "Hello worD")
        self.inject("key", name="Escape")
        self.assertIsNone(self.canvas._text)
        self.assertEqual(len(self.editor.undo_stack), 1)
        self.editor.undo()
        self.assertEqual(self.editor.document.object(text.id).text, "Hello")

    def test_double_click_enters_editing_with_the_word_selected(self):
        text = self.add("text", 100, 100, 400, 60, text="Hello world", fill="#000000",
                        metadata={"font_size": 24, "sizing": "auto_width"})
        self.doc.select([text.id])
        layout = self.canvas.renderer.text_layout(text)
        from luma_canvas.text_edit import caret_rect
        x = 100 + caret_rect(layout, 8)[0]
        self.inject("press", x=x, y=115)
        self.inject("release", x=x, y=115)
        self.inject("press", x=x, y=115)
        self.inject("release", x=x, y=115)
        self.assertEqual(self.canvas._text.selected_text(), "world")
        self.inject("key", name="a", state=CONTROL)
        self.assertEqual(self.canvas._text.selected_text(), "Hello world")
        from luma_canvas.text_edit import selection_rects
        left, top, width, height = selection_rects(self.canvas._text_layout(text), 0, 11)[0]
        px, py = int(100 + left + width - 3), int(100 + top + 2)
        selected = render_overlay(self.canvas)
        self.inject("key", name="Right")
        collapsed = render_overlay(self.canvas)
        offset = py * selected[0] + px * 4
        self.assertNotEqual(tuple(selected[1][offset:offset + 4]), tuple(collapsed[1][offset:offset + 4]))

    def test_input_method_surrounding_text_uses_bytes(self):
        text = self.add("text", 100, 100, 400, 60, text="Grüße x", fill="#000000")
        self.canvas.begin_text_edit(text)
        self.canvas._text.set_cursor(5)

        class Recorder:
            def set_surrounding_with_selection(self, *args):
                self.args = args
        recorder = Recorder()
        self.assertTrue(self.canvas._im_surrounding(recorder))
        self.assertEqual(recorder.args, ("Grüße x", len("Grüße x".encode("utf-8")),
                                         len("Grüße".encode("utf-8")), len("Grüße".encode("utf-8"))))
        # The real context accepts exactly these arguments.
        self.assertTrue(self.canvas._im_surrounding(self.canvas._im))
        self.canvas._im_delete_surrounding(self.canvas._im, -1, 1)
        self.assertEqual(self.canvas._text.text, "Grüß x")
        self.canvas._im_commit(self.canvas._im, "e")
        self.assertEqual(self.canvas._text.text, "Grüße x")

    def test_shortcuts_do_not_fire_while_typing(self):
        text = self.add("text", 100, 100, 300, 60, text="", fill="#000000")
        self.canvas.begin_text_edit(text)
        for name in ("r", "x", "d", "space", "0"):
            self.inject("key", name=name)
        self.assertEqual(self.canvas.tool, "select")
        self.assertEqual(self.doc.object(text.id).text, "rxd 0")
        self.assertEqual(self.doc.current_style["front"], "fill")

    def test_text_tool_click_types_auto_width(self):
        self.canvas.set_tool("text")
        self.inject("press", x=200, y=200)
        self.inject("release", x=200, y=200)
        self.assertIsNotNone(self.canvas._text)
        item = self.doc.objects[-1]
        narrow = item.width
        self.inject("type", text="A longer line of text")
        self.assertGreater(self.doc.object(item.id).width, narrow + 50)
        self.inject("press", x=900, y=700)
        self.inject("release", x=900, y=700)
        self.assertIsNone(self.canvas._text)
        self.assertEqual(self.editor.undo_label, "Create Text")
        self.assertEqual(self.editor.document.object(item.id).text, "A longer line of text")

    def test_empty_new_text_is_discarded(self):
        self.canvas.set_tool("text")
        self.inject("press", x=200, y=200)
        self.inject("release", x=200, y=200)
        self.inject("key", name="Escape")
        self.assertEqual(self.editor.document.objects, [])
        self.assertEqual(self.editor.undo_stack, [])

    def test_click_places_the_caret_where_the_glyphs_are(self):
        text = self.add("text", 0, 0, 600, 60, text="abcdefghij", fill="#000000",
                        metadata={"font_size": 40, "sizing": "auto_width"})
        self.canvas.begin_text_edit(text)
        layout = self.canvas._text_layout(text)
        from luma_canvas.text_edit import caret_rect
        x5 = caret_rect(layout, 5)[0]
        self.inject("press", x=x5 + 1, y=20)
        self.inject("release", x=x5 + 1, y=20)
        self.assertEqual(self.canvas._text.cursor, 5)

    def test_enter_on_a_selected_text_layer_edits_it(self):
        text = self.add("text", 0, 0, 200, 40, text="Name", fill="#000000")
        self.doc.select([text.id])
        self.inject("key", name="Return")
        self.assertIsNotNone(self.canvas._text)
        self.assertEqual(self.canvas._text.selected_text(), "Name")


class GroupKeyTests(CanvasCase):
    def test_ctrl_g_groups_and_click_selects_the_group(self):
        a = self.add("rectangle", 100, 100, 50, 50)
        b = self.add("rectangle", 200, 100, 50, 50)
        self.doc.select([a.id, b.id])
        self.inject("key", name="g", state=CONTROL)
        group = self.editor.document.selection[0]
        self.assertEqual(group.kind, "group")
        self.editor.document.select([])
        self.inject("press", x=120, y=120)
        self.inject("release", x=120, y=120)
        self.assertEqual(self.editor.document.selected_ids, [group.id])
        self.canvas.snap = False
        self.canvas._last_click = (0, 0, 0, 0)
        self.drag((120, 120), (170, 140))
        self.assertEqual(self.editor.document.object(b.id).x, 250)
        self.assertEqual(self.editor.document.object(group.id).x, 150)
        self.inject("key", name="G", state=CONTROL | SHIFT)
        self.assertIsNone(self.editor.document.object(group.id))
        self.assertEqual(set(self.editor.document.selected_ids), {a.id, b.id})

    def test_resizing_a_group_scales_its_children(self):
        a = self.add("rectangle", 100, 100, 50, 50)
        b = self.add("rectangle", 150, 150, 50, 50)
        self.doc.select([a.id, b.id])
        self.canvas.group_selection()
        self.canvas.snap = False
        self.drag((200, 200), (300, 300))
        self.assertEqual((self.editor.document.object(b.id).x, self.editor.document.object(b.id).width), (200, 100))


class SelectionPriorityTests(CanvasCase):
    def setUp(self):
        super().setUp()
        self.frame = self.add("frame", 100, 100, 400, 300, name="Card")
        self.child = self.add("rectangle", 150, 150, 100, 100, parent=self.frame.id, name="Child")
        self.canvas.snap = False

    def test_drag_inside_a_selected_frame_moves_the_frame(self):
        self.doc.select([self.frame.id])
        self.drag((200, 200), (260, 230))
        self.assertEqual(self.doc.selected_ids, [self.frame.id])
        frame, child = self.doc.object(self.frame.id), self.doc.object(self.child.id)
        self.assertEqual((frame.x, frame.y), (160, 130))
        self.assertEqual((child.x - frame.x, child.y - frame.y), (50, 50))

    def test_click_inside_a_selected_frame_selects_the_child(self):
        self.doc.select([self.frame.id])
        self.inject("press", x=200, y=200)
        self.inject("release", x=200, y=200)
        self.assertEqual(self.doc.selected_ids, [self.child.id])

    def test_paste_then_drag_moves_the_pasted_frame(self):
        self.doc.select([self.frame.id])
        self.canvas.copy_selection()
        self.canvas.paste()
        pasted = self.editor.document.selection[0]
        self.canvas._last_click = (0, 0, 0, 0)
        self.drag((215, 215), (315, 215))
        moved = self.editor.document.object(pasted.id)
        self.assertEqual(moved.x, pasted.x)   # the same object moved in place
        self.assertEqual(self.editor.document.selected_ids, [pasted.id])
        self.assertEqual(moved.x, 210)


class TextResizeTests(CanvasCase):
    def test_text_resize_changes_the_box_not_the_font(self):
        text = self.add("text", 100, 100, 120, 30, text="Resize me please", fill="#000000",
                        metadata={"font_size": 20, "sizing": "auto_width"})
        self.doc.select([text.id])
        self.canvas.snap = False
        self.drag((220, 115), (180, 115))
        moved = self.doc.object(text.id)
        self.assertEqual(moved.metadata["sizing"], "auto_height")
        self.assertEqual(moved.metadata["font_size"], 20)
        self.assertGreater(moved.height, 30)
        self.drag((140, moved.y + moved.height), (140, moved.y + moved.height + 40))
        self.assertEqual(self.doc.object(text.id).metadata["sizing"], "fixed")

    def test_font_weight_renders(self):
        from luma_canvas.typography import build_layout
        text = self.add("text", 0, 0, 400, 40, text="Weight", metadata={"font_size": 30, "font_weight": 400,
                                                                          "sizing": "auto_width"})
        context = self.canvas.renderer._pango_context()
        regular = build_layout(context, text).width
        text.metadata = {**text.metadata, "font_weight": 800}
        heavy = build_layout(context, text)
        self.assertGreater(heavy.width, regular)
        self.assertEqual(int(heavy.layout.get_font_description().get_size() / 1024), 30)


class FrameBehaviorTests(CanvasCase):
    def test_double_click_label_renames_in_one_step(self):
        frame = self.add("frame", 100, 100, 300, 200, name="Frame 1")
        area = self.canvas.frame_labels()[0][1]
        x, y = area.get_x() + 4, area.get_y() + 7
        self.inject("press", x=x, y=y)
        self.inject("release", x=x, y=y)
        self.inject("press", x=x, y=y)
        self.inject("release", x=x, y=y)
        self.assertIsNotNone(self.canvas._rename)
        pump(0.05)
        self.canvas._rename.set_text("Home screen")
        self.canvas._rename.emit("activate")
        self.assertIsNone(self.canvas._rename)
        self.assertEqual(self.editor.document.object(frame.id).name, "Home screen")
        self.assertEqual(len(self.editor.undo_stack), 1)

    def test_escape_cancels_a_rename(self):
        frame = self.add("frame", 100, 100, 300, 200, name="Frame 1")
        self.canvas.begin_rename(frame)
        self.canvas._rename.set_text("Nope")
        self.inject("key", name="Escape")
        self.assertEqual(self.editor.document.object(frame.id).name, "Frame 1")

    def test_frame_resize_honors_constraints_and_ctrl_ignores_them(self):
        frame = self.add("frame", 100, 100, 400, 300)
        left = self.add("rectangle", 120, 120, 50, 50, parent=frame.id)
        right = self.add("rectangle", 430, 120, 50, 50, parent=frame.id, constraints={"horizontal": "right"})
        stretch = self.add("rectangle", 120, 300, 360, 20, parent=frame.id, constraints={"horizontal": "left_right"})
        self.doc.select([frame.id])
        self.canvas.snap = False
        self.drag((500, 250), (600, 250))
        document = self.editor.document
        self.assertEqual(document.object(frame.id).width, 500)
        self.assertEqual(document.object(left.id).x, 120)
        self.assertEqual(document.object(right.id).x, 530)
        self.assertEqual(document.object(stretch.id).width, 460)
        self.editor.undo()
        self.editor.document.select([frame.id])
        self.drag((500, 250), (600, 250), state=CONTROL)
        self.assertEqual(self.editor.document.object(right.id).x, 430)

    def test_tool_lock_keeps_drawing(self):
        self.canvas.set_tool("rectangle", locked=True)
        self.drag((100, 100), (150, 150))
        self.drag((200, 100), (250, 150))
        self.assertEqual(len(self.doc.objects), 2)
        self.assertEqual(self.canvas.tool, "rectangle")
        self.inject("key", name="Escape")
        self.assertEqual((self.canvas.tool, self.canvas.tool_locked), ("select", False))

    def test_scale_tool_scales_contents(self):
        text = self.add("text", 100, 100, 100, 40, text="Hi", metadata={"font_size": 20})
        box = self.add("rectangle", 100, 150, 100, 50, stroke="#000000", stroke_width=2, radius=8)
        self.doc.select([text.id, box.id])
        self.inject("key", name="k")
        self.assertEqual(self.canvas.tool, "scale")
        self.canvas.snap = False
        self.drag((200, 200), (300, 300))
        document = self.editor.document
        self.assertAlmostEqual(document.object(text.id).metadata["font_size"], 40)
        self.assertAlmostEqual(document.object(box.id).stroke_width, 4)
        self.assertAlmostEqual(document.object(box.id).radius, 16)

    def test_dragging_out_of_a_frame_moves_to_the_page(self):
        frame = self.add("frame", 100, 100, 300, 300)
        child = self.add("rectangle", 150, 150, 50, 50, parent=frame.id)
        self.doc.select([child.id])
        self.canvas.snap = False
        self.drag((175, 175), (700, 600))
        self.assertIsNone(self.editor.document.object(child.id).parent_id)

    def test_snap_holds_with_hysteresis(self):
        self.add("rectangle", 0, 0, 100, 100)
        moving = self.add("rectangle", 300, 300, 100, 100)
        self.doc.select([moving.id])
        self.inject("press", x=350, y=350)
        for x in (340, 300, 250, 155):          # left edge reaches 105: snaps to 100
            self.inject("motion", x=x, y=350)
        self.assertEqual(self.doc.object(moving.id).x, 100)
        self.inject("motion", x=158, y=350)     # 8px away: still held (within 1.5x)
        self.assertEqual(self.doc.object(moving.id).x, 100)
        self.inject("motion", x=175, y=350)     # far enough: released
        self.assertEqual(self.doc.object(moving.id).x, 125)
        self.inject("release", x=175, y=350)


class ClipboardTests(CanvasCase):
    def setUp(self):
        super().setUp()
        self.frame = self.add("frame", 100, 100, 300, 200, name="Card")
        self.label = self.add("text", 120, 120, 100, 30, parent=self.frame.id, name="Label", text="Hi")

    def test_copy_offers_layers_svg_and_png(self):
        self.doc.select([self.frame.id])
        self.inject("key", name="c", state=CONTROL)
        formats = self.canvas.get_clipboard().get_formats()
        self.assertTrue(formats.contain_mime_type("application/x-luma-canvas+json"))
        self.assertTrue(formats.contain_mime_type("image/svg+xml"))
        self.assertTrue(formats.contain_gtype(Gdk.Texture))

    def test_paste_after_copy_offsets_and_keeps_children(self):
        self.doc.select([self.frame.id])
        self.inject("key", name="c", state=CONTROL)
        self.inject("key", name="v", state=CONTROL)
        pasted = self.doc.selection[0]
        self.assertEqual((pasted.name, pasted.x, pasted.y), ("Card", 110, 110))
        self.assertEqual([child.text for child in self.doc.children(pasted.id)], ["Hi"])
        self.assertEqual(self.editor.undo_label, "Paste")

    def test_paste_reads_the_clipboard_asynchronously(self):
        self.doc.select([self.label.id])
        self.canvas.copy_selection()
        self.canvas._local_clipboard = None
        self.doc.select([self.frame.id])
        self.canvas.paste()
        for _ in range(50):
            pump(0.02)
            if len(self.editor.document.objects) == 3:
                break
        pasted = self.editor.document.objects[-1]
        self.assertEqual((pasted.text, pasted.parent_id), ("Hi", self.frame.id))

    def test_cut_removes_and_paste_restores(self):
        self.doc.select([self.label.id])
        self.inject("key", name="x", state=CONTROL)
        self.assertEqual(len(self.editor.document.objects), 1)
        self.editor.document.select([self.frame.id])
        self.inject("key", name="v", state=CONTROL)
        self.assertEqual(self.editor.document.objects[-1].text, "Hi")

    def test_pasting_plain_text_makes_a_text_layer(self):
        self.canvas._local_clipboard = None
        self.canvas.get_clipboard().set("Hello from elsewhere")
        self.canvas._local_clipboard = None
        self.doc.select([])
        board = self.canvas.get_clipboard()
        board.read_text_async(None, lambda source, result: self.canvas._paste_text(source, result, False))
        for _ in range(50):
            pump(0.02)
            if self.editor.document.objects[-1].kind == "text" and self.editor.document.objects[-1].text.startswith("Hello from"):
                break
        self.assertEqual(self.editor.document.objects[-1].text, "Hello from elsewhere")

    def test_ctrl_d_duplicates(self):
        self.doc.select([self.frame.id])
        self.inject("key", name="d", state=CONTROL)
        self.assertEqual(len(self.editor.document.objects), 4)
        self.assertEqual(self.editor.document.selection[0].name, "Card")


class MaskTests(CanvasCase):
    """Masks draw through GSK masks: alpha, luminance and outline."""

    PINK = (0xe0, 0x20, 0x80)

    def pixel(self, rendered, x, y):
        stride, data, _w, _h = rendered
        offset = y * stride + x * 4
        return tuple(data[offset:offset + 3]), data[offset + 3]

    def build(self, mode="alpha", mask_fill="#000000", **mask_fields):
        mask = self.add("ellipse", 100, 100, 200, 200, fill=mask_fill, name="Mask", **mask_fields)
        photo = self.add("rectangle", 50, 50, 400, 300, fill="#e02080", name="Photo")
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        self.doc.select([mask.id, photo.id])
        self.assertTrue(self.canvas.toggle_mask_selection())
        if mode != "alpha":
            self.editor.set_mask_mode([mask.id], mode)
        self.canvas.queue_draw()
        return mask, photo

    def test_alpha_mask_shows_content_only_inside_the_mask_and_hides_the_mask(self):
        self.build()
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 200, 200), (self.PINK, 255))     # inside the circle
        self.assertEqual(self.pixel(rendered, 70, 70)[1], 0)                  # photo outside the mask
        self.assertEqual(self.pixel(rendered, 110, 110)[1], 0)                # inside the box, outside the circle

    def test_alpha_follows_the_mask_opacity_and_gradient(self):
        from luma_canvas.paint import Color, gradient
        mask, _photo = self.build()
        item = self.editor.document.object(mask.id)
        item.fill_gradient = gradient("linear", [(0.0, Color(0, 0, 0, 1)), (1.0, Color(0, 0, 0, 0))],
                                      [[0.0, 0.5], [1.0, 0.5]])
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        left = self.pixel(rendered, 115, 200)[1]
        right = self.pixel(rendered, 285, 200)[1]
        self.assertGreater(left, 200)
        self.assertLess(right, 50)

    def test_outline_mask_ignores_alpha(self):
        mask, _photo = self.build("outline", mask_fill="#00000010")
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 200, 200), (self.PINK, 255))
        self.assertEqual(self.pixel(rendered, 70, 70)[1], 0)

    def test_luminance_mask_uses_brightness(self):
        self.build("luminance", mask_fill="#ffffff")
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 200, 200), (self.PINK, 255))
        next(item for item in self.editor.document.objects if item.name == "Mask").fill = "#000000"
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        self.assertLess(self.pixel(rendered, 200, 200)[1], 8)

    def test_ctrl_alt_m_toggles_and_undoes(self):
        mask = self.add("ellipse", 100, 100, 200, 200, fill="#000000", name="Mask")
        photo = self.add("rectangle", 50, 50, 400, 300, fill="#e02080", name="Photo")
        self.doc.select([mask.id, photo.id])
        self.inject("key", name="m", state=CONTROL | ALT)
        group = self.editor.document.selection[0]
        self.assertEqual(group.name, "Mask group")
        self.inject("key", name="m", state=CONTROL | ALT)
        self.assertIsNone(self.editor.document.object(group.id))
        self.assertEqual(self.editor.undo_label, "Release mask")
        self.editor.undo()
        self.editor.undo()
        self.assertIsNone(self.editor.document.object(mask.id).metadata.get("mask"))

    def test_clicks_outside_the_mask_do_not_select_masked_content(self):
        self.build()
        self.doc.select([])
        self.inject("press", x=70, y=70)
        self.inject("release", x=70, y=70)
        self.assertEqual(self.editor.document.selected_ids, [])
        self.inject("press", x=200, y=200)
        self.inject("release", x=200, y=200)
        self.assertEqual(self.editor.document.selection[0].name, "Mask group")

    def test_layers_rows_show_the_mask_glyph(self):
        from luma_canvas.layers import LayersModel
        self.build()
        model = LayersModel()
        model.sync(self.editor.document)
        mask = next(item for item in self.editor.document.objects if item.name == "Mask")
        photo = next(item for item in self.editor.document.objects if item.name == "Photo")
        self.assertTrue(model.items[mask.id].props.mask)
        self.assertTrue(model.items[photo.id].props.masked)
        self.assertFalse(model.items[photo.id].props.mask)

    def test_svg_and_png_exports_mask_like_the_canvas(self):
        import tempfile
        from pathlib import Path
        from luma_canvas.export import export, svg_text
        self.build()
        group = self.editor.document.selection[0]
        text = svg_text(self.editor.document, [group.id])
        self.assertIn("<mask", text)
        self.assertIn('mask="url(#', text)
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "mask.png"
            export(self.editor.document, destination, fmt="png", object_ids=[group.id])
            import cairo
            surface = cairo.ImageSurface.create_from_png(str(destination))
            self.assertEqual((surface.get_width(), surface.get_height()), (200, 200))
            data, stride = bytes(surface.get_data()), surface.get_stride()
            self.assertEqual(data[100 * stride + 100 * 4 + 3], 255)
            self.assertEqual(data[5 * stride + 5 * 4 + 3], 0)


class BooleanCanvasTests(CanvasCase):
    PINK = (0xe0, 0x20, 0x80)

    def pixel(self, rendered, x, y):
        stride, data, _w, _h = rendered
        offset = y * stride + x * 4
        return tuple(data[offset:offset + 3]), data[offset + 3]

    def setup_shapes(self):
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        self.square = self.add("rectangle", 100, 100, 200, 200, fill="#2060e0", name="Square")
        self.circle = self.add("ellipse", 220, 140, 160, 160, fill="#e02080", name="Circle")
        self.doc.select([self.square.id, self.circle.id])

    def test_keys_combine_and_the_result_is_drawn_with_curves(self):
        self.setup_shapes()
        self.inject("key", name="u", state=ALT | SHIFT)
        item = self.editor.document.selection[0]
        self.assertEqual(item.kind, "boolean")
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 150, 150), (self.PINK, 255))      # the square's area, in the top shape's paint
        self.assertEqual(self.pixel(rendered, 370, 220), (self.PINK, 255))      # the circle's area
        self.assertEqual(self.pixel(rendered, 372, 160)[1], 0)                  # outside the arc: a curve, not a box
        self.inject("key", name="s", state=ALT | SHIFT)
        item = self.editor.document.selection[0]
        self.assertEqual(item.metadata["boolean_operation"], "subtract")
        self.canvas.queue_draw()
        rendered = render_scaled(self.canvas, 1.0)
        self.assertEqual(self.pixel(rendered, 250, 250)[1], 0)                  # the circle is cut out
        # Changing the operation keeps the boolean's own paint.
        self.assertEqual(self.pixel(rendered, 150, 150), (self.PINK, 255))

    def test_dragging_an_operand_updates_the_result_live(self):
        self.setup_shapes()
        self.canvas.boolean_selection("intersect")
        item = self.editor.document.selection[0]
        # Enter the boolean and pick the circle, as a double-click does.
        self.doc.select([self.circle.id])
        self.inject("press", x=330, y=220)
        for step in range(1, 5):
            self.inject("motion", x=330 + step * 10, y=220)
        rendered = render_scaled(self.canvas, 1.0)
        # Mid-drag the overlap has moved right with the circle.
        self.assertEqual(self.pixel(rendered, 235, 220)[1], 0)
        self.assertEqual(self.pixel(rendered, 290, 220), (self.PINK, 255))
        self.inject("release", x=370, y=220)
        self.assertEqual(self.editor.undo_label, "Move Circle")
        self.assertEqual(self.editor.document.object(item.id).x, 260)
        self.editor.undo()
        self.canvas.replace_editor(self.editor)
        self.assertEqual(self.editor.document.object(item.id).x, 220)

    def test_ctrl_e_flattens(self):
        self.setup_shapes()
        self.canvas.boolean_selection("exclude")
        self.inject("key", name="e", state=CONTROL)
        item = self.editor.document.selection[0]
        self.assertEqual(item.kind, "path")
        self.assertEqual(len(self.editor.document.objects), 1)

    def test_boolean_exports_as_one_curved_path(self):
        from luma_canvas.export import svg_text
        self.setup_shapes()
        self.canvas.boolean_selection("union")
        item = self.editor.document.selection[0]
        text = svg_text(self.editor.document, [item.id])
        self.assertEqual(text.count("<path"), 1)
        self.assertIn("C", text[text.index('d="'):])
        self.assertNotIn('data-name="Square"', text)


class VectorEditTests(CanvasCase):
    """Vector edit mode (spec 8.2) through the input router."""

    def setUp(self):
        super().setUp()
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0
        self.canvas.snap = False

    def click(self, x, y, state=0):
        self.inject("press", x=x, y=y, state=state)
        self.inject("release", x=x, y=y, state=state)

    def world_nodes(self, item_id):
        from luma_canvas import geometry, nodes
        item = self.editor.document.object(item_id)
        return [tuple(round(v, 3) for v in geometry.to_world(item, *nodes.point(n))) for n in self.canvas.vector_nodes(item)]

    def test_double_click_a_vector_enters_and_escape_leaves(self):
        path = self.add("path", 100, 100, 200, 100, fill="#2060e0", name="Wave",
                        path=[{"x": 0, "y": 100}, {"x": 100, "y": 0}, {"x": 200, "y": 100, "closed": True}])
        self.click(200, 160)
        time.sleep(0.01)
        self.inject("press", x=200, y=160)
        self.inject("release", x=200, y=160)
        self.assertEqual(self.canvas.vector_editing, path.id)
        self.inject("key", name="Escape")
        self.assertIsNone(self.canvas.vector_editing)
        self.assertEqual(self.editor.document.selected_ids, [path.id])

    def test_dragging_a_rectangle_corner_makes_it_a_vector_and_undo_restores_it(self):
        rect = self.add("rectangle", 100, 100, 200, 100, fill="#2060e0")
        self.doc.select([rect.id])
        self.inject("key", name="Return")
        self.assertEqual(self.canvas.vector_editing, rect.id)
        self.drag((300, 100), (340, 60))
        item = self.editor.document.object(rect.id)
        self.assertEqual(item.kind, "path")
        self.assertIn((340.0, 60.0), self.world_nodes(rect.id))
        self.assertIn((100.0, 200.0), self.world_nodes(rect.id))       # the others stay put
        self.assertEqual((item.x, item.y, item.width, item.height), (100, 60, 240, 140))
        self.assertTrue(self.editor.undo_label.startswith("Edit"))
        self.editor.undo()
        self.assertEqual(self.editor.document.object(rect.id).kind, "rectangle")

    def test_click_on_a_segment_adds_a_point_and_delete_removes_it(self):
        path = self.add("path", 100, 100, 200, 100, fill="#2060e0", name="Triangle",
                        path=[{"x": 0, "y": 100}, {"x": 100, "y": 0}, {"x": 200, "y": 100, "closed": True}])
        self.canvas.begin_vector_edit(path)
        self.click(200, 200)          # the bottom (closing) segment
        self.assertEqual(len(self.editor.document.object(path.id).path), 4)
        self.assertEqual(self.canvas.vector_selection, [3])
        self.assertIn((200.0, 200.0), self.world_nodes(path.id))
        self.inject("key", name="Delete")
        self.assertEqual(len(self.editor.document.object(path.id).path), 3)
        self.assertEqual(self.editor.undo_label, "Delete points")

    def test_alt_drag_breaks_a_smooth_handle(self):
        path = self.add("path", 100, 100, 200, 100, fill=None, stroke="#000000", stroke_width=2, name="Curve",
                        path=[{"x": 0, "y": 100},
                              {"x": 100, "y": 0, "in_x": 50, "in_y": 0, "out_x": 150, "out_y": 0, "mirror": "length"},
                              {"x": 200, "y": 100}])
        self.canvas.begin_vector_edit(path)
        self.click(200, 100)          # select the middle node: its handles show
        self.assertEqual(self.canvas.vector_selection, [1])
        self.drag((250, 100), (250, 140))
        node = self.editor.document.object(path.id).path[1]
        item = self.editor.document.object(path.id)
        from luma_canvas import geometry
        self.assertEqual(tuple(round(v) for v in geometry.to_world(item, node["in_x"], node["in_y"])), (150, 60))
        self.drag((250, 140), (280, 140), state=ALT)
        node = self.editor.document.object(path.id).path[1]
        self.assertEqual(node["mirror"], "none")
        self.assertEqual(tuple(round(v) for v in geometry.to_world(self.editor.document.object(path.id), node["in_x"], node["in_y"])), (150, 60))

    def test_marquee_selects_points_and_nudges_move_them(self):
        rect = self.add("rectangle", 100, 100, 200, 100, fill="#2060e0")
        self.canvas.begin_vector_edit(rect)
        self.drag((80, 80), (320, 130))        # the top two corners
        self.assertEqual(len(self.canvas.vector_selection), 2)
        self.inject("key", name="Up", state=SHIFT)
        nodes_now = self.world_nodes(rect.id)
        self.assertIn((100.0, 90.0), nodes_now)
        self.assertIn((300.0, 90.0), nodes_now)
        self.assertIn((300.0, 200.0), nodes_now)
        self.assertIsNotNone(self.canvas.vector_editing)

    def test_points_snap_to_each_other(self):
        self.canvas.snap = True
        path = self.add("path", 100, 100, 200, 100, fill="#2060e0", name="Shape",
                        path=[{"x": 0, "y": 0}, {"x": 200, "y": 0}, {"x": 150, "y": 100, "closed": True}])
        self.canvas.begin_vector_edit(path)
        self.drag((250, 200), (103, 170))         # near x of the first node
        self.assertIn((100.0, 170.0), self.world_nodes(path.id))
        self.assertTrue(self.editor.undo_label.startswith("Edit"))

    def test_rotated_vectors_edit_in_their_own_frame(self):
        from luma_canvas import geometry
        path = self.add("path", 200, 200, 100, 100, fill="#2060e0", name="Turned", rotation=45,
                        path=[{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100, "closed": True}])
        before = self.world_nodes(path.id)
        self.canvas.begin_vector_edit(path)
        ax, ay = before[0]
        self.drag((ax, ay), (ax - 30, ay))
        after = self.world_nodes(path.id)
        self.assertAlmostEqual(after[0][0], ax - 30, delta=0.01)
        for index in (1, 2, 3):
            self.assertAlmostEqual(after[index][0], before[index][0], delta=0.01)
            self.assertAlmostEqual(after[index][1], before[index][1], delta=0.01)
        self.assertEqual(self.editor.document.object(path.id).rotation, 45)

    def test_double_click_a_point_switches_corner_and_smooth(self):
        rect = self.add("rectangle", 100, 100, 200, 100, fill="#2060e0")
        self.canvas.begin_vector_edit(rect)
        self.click(300, 200)
        time.sleep(0.01)
        self.click(300, 200)
        item = self.editor.document.object(rect.id)
        self.assertEqual(item.kind, "path")
        corner = next(node for node in item.path if "in_x" in node)
        self.assertNotEqual(corner["mirror"], "none")
        self.assertTrue(self.canvas.set_vector_closed(False))
        self.assertFalse(any(node.get("closed") for node in self.editor.document.object(rect.id).path))


class RulerGuideTests(CanvasCase):
    """Rulers (Shift+R), guides dragged from them, and auto-scroll at the edges."""

    def setUp(self):
        super().setUp()
        self.doc.zoom, self.doc.canvas_x, self.doc.canvas_y = 1.0, 0.0, 0.0

    def guides(self):
        return [item for item in self.editor.document.objects if item.kind == "guide"]

    def test_shift_r_toggles_rulers(self):
        self.assertFalse(self.canvas.show_rulers)
        self.inject("key", name="R", state=SHIFT)
        self.assertTrue(self.canvas.show_rulers)
        self.inject("key", name="R", state=SHIFT)
        self.assertFalse(self.canvas.show_rulers)

    def test_drag_a_guide_out_it_snaps_and_dragging_it_back_deletes_it(self):
        self.canvas.toggle_rulers(True)
        box = self.add("rectangle", 200, 300, 100, 100, fill="#2060e0")
        self.drag((400, 10), (400, 303))           # from the top ruler, near the box's top edge
        guides = self.guides()
        self.assertEqual(len(guides), 1)
        self.assertEqual((guides[0].metadata["axis"], guides[0].y), ("y", 300))
        self.assertEqual(self.editor.undo_label, "Add guide")
        self.drag((10, 400), (253.4, 400))         # from the left ruler: a vertical guide at the box's centre
        vertical = [g for g in self.guides() if g.metadata["axis"] == "x"][0]
        self.assertEqual(vertical.x, 250)
        # A layer dragged near the guide snaps to it.
        self.doc.select([box.id])
        self.drag((250, 350), (256, 350))
        self.assertEqual(self.editor.document.object(box.id).x, 200)
        # Drag the horizontal guide back onto the ruler: gone, in one undo step.
        self.drag((600, 300), (600, 8))
        self.assertEqual(len(self.guides()), 1)
        self.assertEqual(self.editor.undo_label, "Delete guide")
        self.editor.undo()
        self.assertEqual(len(self.guides()), 2)

    def test_a_new_guide_dropped_back_on_the_ruler_is_not_made(self):
        self.canvas.toggle_rulers(True)
        before = len(self.editor.undo_stack)
        self.drag((400, 10), (400, 200))
        self.assertEqual(len(self.guides()), 1)
        self.inject("press", x=600, y=10)
        for step in range(1, 6):
            self.inject("motion", x=600, y=10 + step * 30)
        self.inject("motion", x=600, y=12)
        self.inject("release", x=600, y=12)
        self.assertEqual(len(self.guides()), 1)
        self.assertEqual(len(self.editor.undo_stack), before + 1)

    def test_select_a_guide_and_delete_it(self):
        guide = self.add("guide", 0, 250, 0, 0, fill=None, metadata={"axis": "y"}, name="Guide")
        self.click_at(500, 250)
        self.assertEqual(self.canvas._guide_selected, guide.id)
        self.inject("key", name="Delete")
        self.assertEqual(self.guides(), [])

    def click_at(self, x, y):
        self.inject("press", x=x, y=y)
        self.inject("release", x=x, y=y)

    def test_rulers_draw_crisp_ticks_and_the_selection_extent(self):
        self.canvas.toggle_rulers(True)
        box = self.add("rectangle", 300, 200, 150, 100, fill="#2060e0")
        self.doc.select([box.id])
        snapshot = Gtk.Snapshot()
        self.canvas.do_snapshot(snapshot)
        from luma_canvas.scene import rect as gr
        width, height = self.canvas.get_width(), self.canvas.get_height()
        texture = self.canvas.get_native().get_renderer().render_texture(snapshot.to_node(), gr(0, 0, width, height))
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = downloader.download_bytes()
        data = data.get_data()

        def px(x, y):
            offset = y * stride + x * 4
            return tuple(data[offset:offset + 4])
        # A major tick at document 500 (x=500 at 1x) contrasts with its neighbours in either theme.
        tick, left, right = px(500, 17), px(499, 17), px(501, 17)
        self.assertGreater(abs(sum(tick[:3]) - sum(left[:3])), 30)
        self.assertEqual(left, right)
        # The selection's span is tinted on the top ruler and not beyond it.
        inside, outside = px(375, 4), px(600, 4)
        self.assertNotEqual(inside, outside)

    def test_marquee_auto_scrolls_at_the_edge(self):
        self.canvas.toggle_rulers(False)
        width = self.canvas.get_width()
        self.inject("press", x=400, y=300)
        for step in range(1, 6):
            self.inject("motion", x=400 + step * ((width + 20 - 400) / 5), y=300)
        self.assertIsNotNone(self.canvas._autoscroll)
        before = self.doc.canvas_x
        marquee_before = self.canvas._marquee[2]
        self.canvas.autoscroll_step(0.1)
        self.assertLess(self.doc.canvas_x, before)                 # the view moved to show more on the right
        self.assertGreater(self.canvas._marquee[2], marquee_before)  # and the marquee grew with it
        slow = abs(self.canvas._autoscroll["vx"])
        self.inject("motion", x=width + 100, y=300)
        self.assertGreater(abs(self.canvas._autoscroll["vx"]), slow)
        self.inject("motion", x=500, y=300)
        self.assertIsNone(self.canvas._autoscroll)
        self.inject("release", x=500, y=300)

    def test_moving_a_layer_to_the_edge_scrolls_and_it_follows(self):
        box = self.add("rectangle", 100, 100, 50, 50, fill="#2060e0")
        self.canvas.snap = False
        self.doc.select([box.id])
        height = self.canvas.get_height()
        self.inject("press", x=125, y=125)
        for step in range(1, 6):
            self.inject("motion", x=125, y=125 + step * ((height - 125) / 5))
        start_y = self.editor.document.object(box.id).y
        for _ in range(5):
            self.canvas.autoscroll_step(0.05)
        self.assertGreater(self.editor.document.object(box.id).y, start_y + 20)
        self.inject("release", x=125, y=height)
        self.assertIsNone(self.canvas._autoscroll)
        self.assertEqual(len(self.editor.undo_stack), 1)


@unittest.skipUnless(HAVE_DISPLAY, "needs a display and a session bus")
class PortalTests(unittest.TestCase):
    XML = """<node><interface name="org.freedesktop.portal.Screenshot">
      <method name="PickColor"><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/>
      <arg type="o" direction="out"/></method></interface></node>"""

    def test_AT_E3_portal_pick_color(self):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self.skipTest("no session bus")
        info = Gio.DBusNodeInfo.new_for_xml(self.XML)

        def called(connection, sender, path, interface, method, parameters, invocation):
            _parent, options = parameters.unpack()
            token = options["handle_token"]
            handle = f"/org/freedesktop/portal/desktop/request/{sender[1:].replace('.', '_')}/{token}"
            invocation.return_value(GLib.Variant("(o)", (handle,)))
            connection.emit_signal(sender, handle, "org.freedesktop.portal.Request", "Response",
                                   GLib.Variant("(ua{sv})", (0, {"color": GLib.Variant("(ddd)", (0.2, 0.4, 0.6))})))
        owner = Gio.bus_own_name_on_connection(bus, "org.freedesktop.portal.Desktop",
                                               Gio.BusNameOwnerFlags.REPLACE, None, None)
        registration = bus.register_object("/org/freedesktop/portal/desktop", info.interfaces[0], called, None, None)
        results = []
        pick_screen_color(None, results.append)
        for _ in range(100):
            pump(0.02)
            if results:
                break
        bus.unregister_object(registration)
        Gio.bus_unown_name(owner)
        self.assertTrue(results and results[0] is not None, results)
        self.assertEqual(to_hex(results[0]), "#336699")


if __name__ == "__main__":
    unittest.main()
