# SPDX-License-Identifier: MPL-2.0
"""Snapping as the canvas uses it: screen thresholds, spacing, suspension."""

import unittest

from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.snapping import SCREEN_THRESHOLD, Box, SnapEngine


def rect(name, x, y, w=100, h=100, parent=None, kind="rectangle"):
    return LayoutObject(new_id(kind), kind, name, parent_id=parent, x=x, y=y, width=w, height=h)


def document(*objects):
    value = Document.empty("Snap")
    value.objects.extend(objects)
    value.validate()
    return value


class InteractionSnapTests(unittest.TestCase):
    def test_AT_S7_edge_snaps_within_three_screen_px_and_ctrl_suspends(self):
        sibling = rect("sibling", 0, 0)
        moving = rect("moving", 103, 200)
        doc = document(sibling, moving)
        zoom = 1.0
        engine = SnapEngine(doc, threshold=SCREEN_THRESHOLD / zoom)
        x, _, guides = engine.snap_point({moving.id}, 103, 250, axes="x")
        self.assertEqual(x, 100)
        self.assertTrue(guides)
        x, _, guides = engine.snap_point({moving.id}, 103, 250, axes="x", suppressed=True)
        self.assertEqual((x, guides), (103, []))

    def test_threshold_is_constant_on_screen(self):
        sibling = rect("sibling", 0, 0)
        moving = rect("moving", 104, 200)
        doc = document(sibling, moving)
        zoomed_in = SnapEngine(doc, threshold=SCREEN_THRESHOLD / 4.0, pixel_snap=False)
        self.assertEqual(zoomed_in.snap_move({moving.id}, 0, 0).dx, 0)
        zoomed_out = SnapEngine(doc, threshold=SCREEN_THRESHOLD / 0.5, pixel_snap=False)
        self.assertEqual(zoomed_out.snap_move({moving.id}, 0, 0).dx, -104 + 100)

    def test_AT_S8_gap_snaps_to_an_existing_gap_with_two_badges(self):
        a = rect("a", 0, 0)
        b = rect("b", 124, 0)            # gap 24
        c = rect("c", 244, 0)            # gap 20
        doc = document(a, b, c)
        engine = SnapEngine(doc, threshold=SCREEN_THRESHOLD)
        result = engine.snap_move({c.id}, 3, 0)   # gap 23: within reach of 24
        self.assertAlmostEqual(c.x + result.dx, 248)
        badges = [m for m in result.measurements if m.label == "24"]
        self.assertEqual(len(badges), 2)

    def test_suspended_move_draws_nothing(self):
        a = rect("a", 0, 0)
        b = rect("b", 102, 0)
        doc = document(a, b)
        result = SnapEngine(doc).snap_move({b.id}, 0, 0, suppressed=True)
        self.assertEqual((result.dx, result.guides, result.measurements), (0, [], []))

    def test_alignment_guides_have_marks(self):
        a = rect("a", 0, 0)
        b = rect("b", 0, 300)
        doc = document(a, b)
        result = SnapEngine(doc).snap_move({b.id}, 2, 0)
        vertical = [g for g in result.guides if g.orientation == "vertical"]
        self.assertTrue(vertical and vertical[0].marks)

    def test_children_of_a_moving_frame_are_not_targets(self):
        frame = rect("frame", 0, 0, 400, 400, kind="frame")
        child = rect("child", 10, 10, 50, 50, parent=frame.id)
        doc = document(frame, child)
        self.assertEqual(SnapEngine(doc)._peers({frame.id}), [])

    def test_AT_S11_measure_badge_between_selection_and_hover(self):
        a = rect("a", 0, 0)
        b = rect("b", 140, 0)
        found = SnapEngine(document(a, b)).distances_to({a.id}, b)
        self.assertEqual([m.label for m in found], ["40"])

    def test_measure_inside_a_containing_frame(self):
        frame = rect("frame", 0, 0, 400, 300, kind="frame")
        child = rect("child", 50, 20, 100, 100, parent=frame.id)
        found = SnapEngine(document(frame, child)).distances_to({child.id}, frame)
        self.assertEqual(sorted(m.label for m in found), ["180", "20", "250", "50"])

    def test_fractional_labels_are_trimmed(self):
        a = rect("a", 0, 0)
        b = rect("b", 140.25, 0)
        found = SnapEngine(document(a, b)).distances_to({a.id}, b)
        self.assertEqual(found[0].label, "40.25")


if __name__ == "__main__":
    unittest.main()
