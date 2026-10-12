# SPDX-License-Identifier: MPL-2.0
"""Hit testing with rotation and hierarchy, click rules, marquee, transforms."""

import math
import unittest

from luma_canvas.geometry import (
    Tree, click_target, hit_chain, marquee_selection, radius_from_handle,
    resize_item_box, resize_local, rotation_delta, to_local, to_world, contains, outline,
    flatten_subpath,
)
from luma_canvas.model import Document, LayoutObject, new_id


def obj(kind, name, x, y, w, h, parent=None, **fields):
    return LayoutObject(new_id(kind), kind, name, parent, x, y, w, h, **fields)


class HitTests(unittest.TestCase):
    def test_AT_S3_rotated_bar_misses_outside_its_shape(self):
        bar = obj("rectangle", "Bar", 0, 45, 100, 10, rotation=45)
        tree = Tree([bar])
        # Inside the axis-aligned bounds of the rotated bar, far from the bar.
        self.assertEqual(hit_chain(tree, 20, 80), [])
        self.assertEqual(hit_chain(tree, 50, 50), [bar])

    def test_rotation_round_trip(self):
        item = obj("rectangle", "R", 10, 20, 80, 40, rotation=30, flip_h=True)
        for point in ((0, 0), (80, 40), (12.5, 33)):
            world = to_world(item, *point)
            back = to_local(item, *world)
            self.assertAlmostEqual(back[0], point[0])
            self.assertAlmostEqual(back[1], point[1])

    def test_AT_S14_ellipse_hits_its_shape_not_its_box(self):
        ellipse = obj("ellipse", "E", 0, 0, 100, 100)
        self.assertFalse(contains(ellipse, 3, 3, tolerance=0.5))
        self.assertTrue(contains(ellipse, 50, 50))

    def test_open_path_hits_along_the_curve(self):
        path = obj("path", "P", 0, 0, 100, 100, fill=None, stroke="#000", stroke_width=1,
                   path=[{"x": 0, "y": 100, "out_x": 0, "out_y": 0},
                         {"x": 100, "y": 100, "in_x": 100, "in_y": 0}])
        self.assertTrue(contains(path, 50, 25, tolerance=2))
        self.assertFalse(contains(path, 50, 100, tolerance=2))

    def test_AT_R2_curve_outline_passes_its_t_half_point(self):
        path = obj("path", "P", 0, 0, 100, 100, path=[
            {"x": 0, "y": 0, "out_x": 0, "out_y": 100},
            {"x": 100, "y": 0, "in_x": 100, "in_y": 100}])
        points = flatten_subpath(outline(path)[0], 16)
        self.assertAlmostEqual(max(p[1] for p in points), 75.0)

    def test_closed_key_and_old_close_key_both_close(self):
        for key in ("closed", "close"):
            path = obj("path", "P", 0, 0, 10, 10, path=[{"x": 0, "y": 0}, {"x": 10, "y": 0},
                                                         {"x": 10, "y": 10, key: True}])
            self.assertTrue(outline(path)[0].closed, key)

    def test_hidden_and_locked_layers_are_not_hit(self):
        hidden = obj("rectangle", "H", 0, 0, 10, 10, visible=False)
        locked = obj("rectangle", "L", 0, 0, 10, 10, locked=True)
        self.assertEqual(hit_chain(Tree([hidden, locked]), 5, 5), [])


class ClickRuleTests(unittest.TestCase):
    def setUp(self):
        self.frame = obj("frame", "Phone", 0, 0, 400, 800)
        self.card = obj("group", "Card", 20, 20, 200, 100, self.frame.id)
        self.label = obj("text", "Label", 30, 30, 100, 20, self.card.id)
        self.other = obj("rectangle", "Other", 20, 300, 100, 100, self.frame.id)
        self.tree = Tree([self.frame, self.card, self.label, self.other])

    def target(self, x, y, selected=(), deep=False):
        chain = hit_chain(self.tree, x, y)
        return click_target(self.tree, chain, selected, deep=deep)

    def test_AT_S1_click_selects_card_then_double_click_label(self):
        self.assertIs(self.target(40, 40), self.card)
        # After entering the card, the same click selects the label.
        self.assertIs(self.target(40, 40, [self.label.id]), self.label)

    def test_AT_S2_ctrl_click_deep_selects(self):
        self.assertIs(self.target(40, 40, deep=True), self.label)

    def test_frame_children_are_clickable_directly(self):
        self.assertIs(self.target(50, 350), self.other)

    def test_empty_frame_area_selects_the_frame(self):
        self.assertIs(self.target(300, 700), self.frame)

    def test_clipped_children_outside_frame_are_not_hit(self):
        outside = obj("rectangle", "Outside", 380, 10, 100, 50, self.frame.id)
        tree = Tree([self.frame, outside])
        self.assertEqual(hit_chain(tree, 450, 30), [])


class MarqueeTests(unittest.TestCase):
    def setUp(self):
        self.frame = obj("frame", "Frame", 0, 0, 1000, 600)
        self.a = obj("rectangle", "A", 100, 100, 100, 100, self.frame.id)
        self.b = obj("rectangle", "B", 300, 100, 100, 100, self.frame.id)
        self.loose = obj("rectangle", "Loose", 1200, 100, 50, 50)
        self.tree = Tree([self.frame, self.a, self.b, self.loose])

    def test_AT_S4_touch_selects_children_not_frame(self):
        chosen = marquee_selection(self.tree, (150, 150, 350, 150 + 10))
        self.assertEqual(chosen, [self.a.id, self.b.id])

    def test_AT_S4_alt_enclose_selects_nothing_half_touched(self):
        self.assertEqual(marquee_selection(self.tree, (150, 150, 350, 160), enclose=True), [])

    def test_enclosing_marquee_selects_the_frame(self):
        self.assertEqual(marquee_selection(self.tree, (-10, -10, 1010, 610)), [self.frame.id])

    def test_ctrl_marquee_goes_deep(self):
        inner = obj("rectangle", "Inner", 110, 110, 20, 20, self.a.id)
        group = obj("group", "G", 100, 100, 100, 100, self.frame.id)
        inner.parent_id = group.id
        tree = Tree([self.frame, group, inner])
        self.assertEqual(marquee_selection(tree, (105, 105, 115, 115), deep=True), [inner.id])

    def test_marquee_reversed_drag_works(self):
        self.assertEqual(marquee_selection(self.tree, (1300, 200, 1190, 90)), [self.loose.id])


class TransformTests(unittest.TestCase):
    def test_AT_S9_shift_corner_keeps_ratio(self):
        left, top, right, bottom, *_ = resize_local(200, 100, "se", 331, 120, keep_aspect=True)
        self.assertAlmostEqual((right - left) / (bottom - top), 2.0, places=2)

    def test_AT_S9_alt_corner_keeps_center(self):
        item = obj("rectangle", "R", 100, 100, 200, 100)
        box = resize_item_box(item, "se", 350, 250, from_center=True)
        self.assertAlmostEqual(box.x + box.width / 2, 200)
        self.assertAlmostEqual(box.y + box.height / 2, 150)

    def test_rotated_resize_keeps_opposite_corner_fixed(self):
        item = obj("rectangle", "R", 0, 0, 100, 50, rotation=30)
        anchor = to_world(item, 0, 0)
        target = to_world(item, 150, 80)
        box = resize_item_box(item, "se", *target)
        moved = obj("rectangle", "M", box.x, box.y, box.width, box.height, rotation=30)
        self.assertAlmostEqual(box.width, 150)
        self.assertAlmostEqual(box.height, 80)
        new_anchor = to_world(moved, 0, 0)
        self.assertAlmostEqual(new_anchor[0], anchor[0])
        self.assertAlmostEqual(new_anchor[1], anchor[1])

    def test_dragging_past_the_edge_flips(self):
        *_, flip_x, flip_y = resize_local(100, 100, "e", -50, 50)
        self.assertTrue(flip_x)
        self.assertFalse(flip_y)

    def test_rotation_delta(self):
        self.assertAlmostEqual(rotation_delta((0, 0), (10, 0), (0, 10)), 90)

    def test_AT_O5_radius_handle(self):
        item = obj("rectangle", "R", 0, 0, 200, 100)
        self.assertAlmostEqual(radius_from_handle(item, 0, 20, 20), 20)
        self.assertAlmostEqual(radius_from_handle(item, 2, 180, 80), 20)
        self.assertAlmostEqual(radius_from_handle(item, 0, 500, 500), 50)


if __name__ == "__main__":
    unittest.main()


class ConstrainMoveTests(unittest.TestCase):
    def test_shift_drag_locks_to_the_nearer_axis(self):
        from luma_canvas.geometry import constrain_move
        dx, dy = constrain_move(86.6, 50.0)        # 30 degrees
        self.assertEqual(dy, 0.0)
        dx, dy = constrain_move(10.0, 90.0)
        self.assertEqual(dx, 0.0)
        dx, dy = constrain_move(70.0, 72.0)        # near 45 degrees
        self.assertAlmostEqual(dx, dy)
