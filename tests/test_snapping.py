# SPDX-License-Identifier: Apache-2.0
"""Snapping is the thing this application is for, so it is tested directly."""

import unittest

from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.snapping import Box, SnapEngine, snap_rotation


def _document(*objects: LayoutObject) -> Document:
    document = Document.empty("Snap")
    document.objects.extend(objects)
    document.validate()
    return document


def _rect(name: str, x: float, y: float, width: float = 100, height: float = 100,
          parent: str | None = None) -> LayoutObject:
    return LayoutObject(new_id("rectangle"), "rectangle", name, parent_id=parent,
                        x=x, y=y, width=width, height=height)


class SnapMoveTests(unittest.TestCase):
    def test_clear_row_supplies_perpendicular_gap(self):
        left = _rect("left", 0, 0, 100, 100)
        right = _rect("right", 150, 0, 100, 100)
        moving = _rect("moving", 0, 147, 100, 100)
        engine = SnapEngine(_document(left, right, moving))
        result = engine.snap_move({moving.id}, 0, 0)
        self.assertAlmostEqual(result.dy, 3)
        bypass = engine.snap_move({moving.id}, 0, 0, suppressed=True)
        self.assertEqual(bypass.dy, 0)
        distant = engine.snap_move({moving.id}, 0, 30)
        self.assertNotEqual(distant.dy, 3)

    def test_perpendicular_gap_ignores_clutter_and_distant_rows(self):
        left = _rect("left", 0, 0, 100, 100)
        right = _rect("right", 150, 0, 100, 100)
        moving = _rect("moving", 0, 147, 100, 100)
        clutter = _rect("clutter", 120, 20, 10, 20)
        result = SnapEngine(_document(left, right, moving, clutter)).snap_move({moving.id}, 0, 0)
        self.assertNotEqual(result.dy, 3)
        left.y = right.y = -1000
        result = SnapEngine(_document(left, right, moving)).snap_move({moving.id}, 0, 0)
        self.assertNotEqual(result.dy, 3)

    def test_left_edges_snap_together(self):
        anchor = _rect("anchor", 100, 0)
        moving = _rect("moving", 96, 300)
        document = _document(anchor, moving)
        engine = SnapEngine(document)
        result = engine.snap_move({moving.id}, 0, 0)
        self.assertAlmostEqual(result.dx, 4.0)
        self.assertTrue(any(guide.orientation == "vertical" for guide in result.guides))

    def test_centres_snap(self):
        anchor = _rect("anchor", 0, 0, 200, 200)      # centre x = 100
        moving = _rect("moving", 47, 400, 100, 100)   # centre x = 97
        document = _document(anchor, moving)
        result = SnapEngine(document).snap_move({moving.id}, 0, 0)
        self.assertAlmostEqual(moving.x + result.dx + 50, 100.0)

    def test_the_escape_modifier_suppresses_every_snap(self):
        anchor = _rect("anchor", 100, 0)
        moving = _rect("moving", 96, 300)
        document = _document(anchor, moving)
        result = SnapEngine(document).snap_move({moving.id}, 0.3, 0.2, suppressed=True)
        self.assertEqual((result.dx, result.dy), (0.3, 0.2))
        self.assertEqual(result.guides, [])

    def test_pixels_snap_when_nothing_else_holds(self):
        moving = _rect("moving", 10.4, 20.6)
        document = _document(moving)
        result = SnapEngine(document).snap_move({moving.id}, 0.0, 0.0)
        self.assertAlmostEqual(moving.x + result.dx, 10.0)
        self.assertAlmostEqual(moving.y + result.dy, 21.0)

    def test_pixel_snapping_can_be_turned_off_for_print(self):
        moving = _rect("moving", 10.4, 20.6)
        document = _document(moving)
        engine = SnapEngine(document, pixel_snap=False)
        result = engine.snap_move({moving.id}, 0.0, 0.0)
        self.assertAlmostEqual(moving.x + result.dx, 10.4)


class EqualSpacingTests(unittest.TestCase):
    def test_matching_gaps_are_detected_and_measured(self):
        left = _rect("left", 0, 0, 100, 100)
        right = _rect("right", 400, 0, 100, 100)
        # Placed so the gaps either side are both 100.
        moving = _rect("moving", 200, 0, 100, 100)
        document = _document(left, right, moving)
        engine = SnapEngine(document)
        guides, measurements = engine.equal_spacing(
            Box(200, 0, 100, 100), [left, right]
        )
        self.assertTrue(guides)
        self.assertEqual(len(measurements), 2)
        self.assertAlmostEqual(measurements[0].distance, 100.0)
        self.assertAlmostEqual(measurements[1].distance, 100.0)

    def test_unequal_gaps_report_nothing(self):
        left = _rect("left", 0, 0, 100, 100)
        right = _rect("right", 400, 0, 100, 100)
        document = _document(left, right)
        guides, measurements = SnapEngine(document).equal_spacing(
            Box(150, 0, 100, 100), [left, right]
        )
        self.assertEqual(guides, [])
        self.assertEqual(measurements, [])


class MeasurementTests(unittest.TestCase):
    def test_distance_to_the_hovered_object(self):
        selected = _rect("selected", 0, 0, 100, 100)
        hovered = _rect("hovered", 300, 0, 100, 100)
        document = _document(selected, hovered)
        found = SnapEngine(document).distances_to({selected.id}, hovered)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].distance, 200.0)
        self.assertEqual(found[0].label, "200")


class RotationTests(unittest.TestCase):
    def test_rotation_snaps_to_fifteen_degrees_when_constrained(self):
        self.assertEqual(snap_rotation(47.0, constrain=True), 45.0)
        self.assertEqual(snap_rotation(47.0, constrain=False), 47.0)


if __name__ == "__main__":
    unittest.main()
