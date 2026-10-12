# SPDX-License-Identifier: Apache-2.0
"""Béziers, booleans, stroke alignment and corners."""

import unittest

from luma_canvas.model import LayoutObject, new_id
from luma_canvas.vector import (
    Point, boolean, break_handle, close_path, combine, corner_radii, flatten,
    insert_node, node, outline_of, remove_node, set_corner_radius, set_handle,
    simplify,
)


def _rect(x=0.0, y=0.0, width=100.0, height=100.0, kind="rectangle") -> LayoutObject:
    return LayoutObject(new_id(kind), kind, "Shape", x=x, y=y, width=width, height=height)


class NodeTests(unittest.TestCase):
    def test_a_smooth_node_mirrors_its_handles(self):
        path = [node(100, 100, smooth=True)]
        set_handle(path, 0, "out", 120, 100)
        self.assertAlmostEqual(path[0]["in_x"], 80.0)
        self.assertAlmostEqual(path[0]["in_y"], 100.0)

    def test_breaking_a_handle_stops_the_mirroring(self):
        path = [node(100, 100, smooth=True)]
        break_handle(path, 0)
        set_handle(path, 0, "out", 120, 100)
        self.assertNotIn("in_x", path[0])

    def test_nodes_can_be_inserted_and_removed(self):
        path = [node(0, 0), node(10, 10), node(20, 0)]
        insert_node(path, 1, 5, 5)
        self.assertEqual(len(path), 4)
        remove_node(path, 1)
        self.assertEqual(len(path), 3)

    def test_a_path_keeps_at_least_two_nodes(self):
        path = [node(0, 0), node(10, 10)]
        remove_node(path, 0)
        self.assertEqual(len(path), 2)


class FlattenTests(unittest.TestCase):
    def test_a_straight_segment_stays_two_points(self):
        points = flatten([node(0, 0), node(100, 0)])
        self.assertEqual(len(points), 2)

    def test_a_curve_is_followed(self):
        path = [node(0, 0, out_point=(0, 100)), node(100, 0, in_point=(100, 100))]
        points = flatten(path)
        self.assertGreater(len(points), 10)
        # The curve bulges away from the straight line between the ends.
        self.assertGreater(max(point.y for point in points), 10.0)

    def test_a_closed_path_returns_to_its_start(self):
        path = close_path([node(0, 0), node(100, 0), node(100, 100)])
        points = flatten(path)
        self.assertAlmostEqual(points[-1].x, 0.0)
        self.assertAlmostEqual(points[-1].y, 0.0)


class BooleanTests(unittest.TestCase):
    def setUp(self):
        self.a = [Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)]
        self.b = [Point(50, 50), Point(150, 50), Point(150, 150), Point(50, 150)]

    def _bounds(self, points):
        return (min(p.x for p in points), min(p.y for p in points),
                max(p.x for p in points), max(p.y for p in points))

    def test_union_covers_both(self):
        result = boolean("union", self.a, self.b)
        left, top, right, bottom = self._bounds(result)
        self.assertAlmostEqual(left, 0.0)
        self.assertAlmostEqual(right, 150.0)

    def test_intersect_covers_only_the_overlap(self):
        result = boolean("intersect", self.a, self.b)
        left, top, right, bottom = self._bounds(result)
        self.assertAlmostEqual(left, 50.0)
        self.assertAlmostEqual(top, 50.0)
        self.assertAlmostEqual(right, 100.0)
        self.assertAlmostEqual(bottom, 100.0)

    def test_subtract_keeps_the_first_shape_only(self):
        result = boolean("subtract", self.a, self.b)
        left, top, right, bottom = self._bounds(result)
        self.assertAlmostEqual(right, 100.0)

    def test_combining_keeps_its_operands_on_the_record(self):
        first, second = _rect(), _rect(50, 50)
        combined = combine("union", [first, second])
        self.assertEqual(combined.kind, "path")
        self.assertEqual(combined.metadata["boolean"], "union")
        self.assertEqual(combined.metadata["operands"], [first.id, second.id])
        self.assertTrue(combined.path)

    def test_a_boolean_needs_two_shapes(self):
        with self.assertRaises(ValueError):
            combine("union", [_rect()])


class OutlineTests(unittest.TestCase):
    def test_an_ellipse_outlines_as_a_ring_not_a_box(self):
        points = outline_of(_rect(kind="ellipse"))
        self.assertGreater(len(points), 8)


class CornerTests(unittest.TestCase):
    def test_all_four_corners_follow_the_single_radius(self):
        item = _rect()
        set_corner_radius(item, None, 12)
        self.assertEqual(corner_radii(item), (12.0, 12.0, 12.0, 12.0))

    def test_one_corner_can_differ(self):
        item = _rect()
        set_corner_radius(item, "top-right", 20)
        self.assertEqual(corner_radii(item), (0.0, 20.0, 0.0, 0.0))

    def test_an_unknown_corner_is_refused(self):
        with self.assertRaises(ValueError):
            set_corner_radius(_rect(), "middle", 4)


class SimplifyTests(unittest.TestCase):
    def test_points_on_a_straight_line_are_dropped(self):
        path = [node(0, 0), node(50, 0), node(100, 0)]
        self.assertEqual(len(simplify(path)), 2)

    def test_a_corner_is_kept(self):
        path = [node(0, 0), node(50, 50), node(100, 0)]
        self.assertEqual(len(simplify(path)), 3)


if __name__ == "__main__":
    unittest.main()
