# SPDX-License-Identifier: MPL-2.0
"""Vector edit mode's node operations."""

import math
import unittest

from luma_canvas import nodes
from luma_canvas.geometry import flatten_subpath, nodes_outlines


def square():
    return [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100, "closed": True}]


def curve():
    return [{"x": 0, "y": 0, "out_x": 0, "out_y": 60},
            {"x": 100, "y": 0, "in_x": 100, "in_y": 60}]


def sample(path, count=64):
    return [point for shape in nodes_outlines(path) for point in flatten_subpath(shape, count)]


class NodeEditTests(unittest.TestCase):
    def test_segments_include_the_closing_one(self):
        self.assertEqual(nodes.segments(square()), [(0, 1), (1, 2), (2, 3), (3, 0)])
        two = square() + [{"x": 200, "y": 0, "move": True}, {"x": 300, "y": 0}]
        self.assertEqual(nodes.subpaths(two), [(0, 4), (4, 6)])
        self.assertEqual(nodes.segments(two)[-1], (4, 5))

    def test_move_nodes_carries_handles(self):
        moved = nodes.move_nodes(curve(), [0], 10, 5)
        self.assertEqual((moved[0]["x"], moved[0]["out_y"]), (10, 65))
        self.assertEqual(curve()[0]["x"], 0)          # never mutates its input

    def test_handle_mirrors(self):
        node = [{"x": 50, "y": 50, "in_x": 40, "in_y": 50, "out_x": 60, "out_y": 50, "mirror": "length"}]
        moved = nodes.move_handle(node, 0, "out", 50, 80)
        self.assertEqual((moved[0]["in_x"], moved[0]["in_y"]), (50, 20))
        node[0]["mirror"] = "angle"
        moved = nodes.move_handle(node, 0, "out", 50, 80)
        self.assertAlmostEqual(moved[0]["in_y"], 40)      # opposite direction, its own length of 10
        broken = nodes.move_handle(node, 0, "out", 90, 90, break_mirror=True)
        self.assertEqual((broken[0]["in_x"], broken[0]["in_y"]), (40, 50))
        self.assertEqual(broken[0]["mirror"], "none")

    def test_splitting_a_curve_keeps_its_shape(self):
        path = curve()
        before = sample(path)
        split, index = nodes.split_segment(path, (0, 1), 0.3)
        self.assertEqual(index, 1)
        self.assertEqual(len(split), 3)
        after = sample(split, 400)
        for x, y in before:
            self.assertLess(min(math.dist((x, y), other) for other in after), 0.3)

    def test_splitting_the_closing_segment(self):
        split, index = nodes.split_segment(square(), (3, 0), 0.5)
        self.assertEqual(index, 4)
        self.assertEqual((split[4]["x"], split[4]["y"]), (0, 50))
        self.assertTrue(split[4]["closed"])
        self.assertNotIn("closed", split[3])

    def test_nearest_segment(self):
        found = nodes.nearest_segment(square(), (50, 3), 5)
        self.assertEqual(found[0], (0, 1))
        self.assertAlmostEqual(found[1], 0.5, places=3)
        self.assertIsNone(nodes.nearest_segment(square(), (50, 50), 5))

    def test_delete_nodes_joins_neighbors_and_drops_tiny_subpaths(self):
        path = nodes.delete_nodes(square(), [1])
        self.assertEqual([(n["x"], n["y"]) for n in path], [(0, 0), (100, 100), (0, 100)])
        self.assertTrue(path[-1]["closed"])
        two = square() + [{"x": 200, "y": 0, "move": True}, {"x": 300, "y": 0}]
        self.assertEqual(len(nodes.delete_nodes(two, [5])), 4)
        after = nodes.delete_nodes(two, [0])
        self.assertTrue(after[3].get("move"))

    def test_close_and_open(self):
        opened = nodes.set_closed(square(), 2, False)
        self.assertNotIn("closed", opened[3])
        self.assertTrue(nodes.set_closed(opened, 0, True)[3]["closed"])

    def test_corner_and_smooth(self):
        smooth = nodes.set_mirror(square(), [1], "length")
        self.assertIn("in_x", smooth[1])
        left = math.dist(nodes.point(smooth[1]), nodes.handle(smooth[1], "in"))
        right = math.dist(nodes.point(smooth[1]), nodes.handle(smooth[1], "out"))
        self.assertAlmostEqual(left, right)
        corner = nodes.set_mirror(smooth, [1], "corner")
        self.assertNotIn("in_x", corner[1])
        with self.assertRaises(ValueError):
            nodes.set_mirror(square(), [1], "wiggly")

    def test_mirrors_are_detected_from_handles(self):
        from luma_canvas.booleans import to_nodes
        from luma_canvas.geometry import ellipse_outline
        detected = nodes.detect_mirrors(to_nodes([ellipse_outline(100, 100)]))
        self.assertTrue(all(node["mirror"] == "length" for node in detected))
        self.assertEqual(nodes.detect_mirrors(square())[0]["mirror"], "none")

    def test_rebased(self):
        path, box = nodes.rebased(nodes.move_nodes(square(), range(4), 20, 30))
        self.assertEqual(box, (20, 30, 100, 100))
        self.assertEqual((path[0]["x"], path[0]["y"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
