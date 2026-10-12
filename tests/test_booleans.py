# SPDX-License-Identifier: MPL-2.0
"""Booleans on curves: exact results, holes, fill rules and degenerate input."""

import math
import random
import time
import unittest

from luma_canvas.booleans import Shape, boolean, sub_cubic, to_nodes
from luma_canvas.geometry import Subpath, _winding, ellipse_outline, flatten_subpath, nodes_outlines


def rect(x, y, w, h, clockwise=True):
    s = Subpath((x, y), closed=True)
    points = [(x + w, y), (x + w, y + h), (x, y + h)]
    for point in (points if clockwise else list(reversed(points))):
        s.line(*point)
    return s


def circle(cx, cy, r):
    e = ellipse_outline(2 * r, 2 * r)

    def move(x, y):
        return x + cx - r, y + cy - r
    s = Subpath(move(*e.start), closed=True)
    for segment in e.segments:
        s.cubic(*move(*segment[1:3]), *move(*segment[3:5]), *move(*segment[5:7]))
    return s


def polygon(points):
    s = Subpath(points[0], closed=True)
    for point in points[1:]:
        s.line(*point)
    return s


def signed_area(subpaths):
    total = 0.0
    for subpath in subpaths:
        points = flatten_subpath(subpath, 64)
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
            total += x0 * y1 - x1 * y0
    return total / 2


def inside(subpaths, point, rule="nonzero"):
    winding = sum(_winding(point, flatten_subpath(subpath, 64)) for subpath in subpaths)
    return winding % 2 != 0 if rule == "evenodd" else winding != 0


def near_boundary(subpaths, point, distance):
    from luma_canvas.geometry import _distance_to_segment
    for subpath in subpaths:
        points = flatten_subpath(subpath, 64)
        for a, b in zip(points, points[1:] + points[:1]):
            if _distance_to_segment(point, a, b) <= distance:
                return True
    return False


EXPECT = {
    "union": any,
    "intersect": all,
    "subtract": lambda values: values[0] and not any(values[1:]),
    "exclude": lambda values: sum(values) % 2 == 1,
}


class BooleanTests(unittest.TestCase):
    def assertArea(self, subpaths, expected, tolerance=0.002):
        got = abs(signed_area(subpaths))
        self.assertAlmostEqual(got, expected, delta=max(1e-6, expected * tolerance), msg=(got, expected))

    def test_rectangles(self):
        a, b = Shape([rect(0, 0, 100, 100)]), Shape([rect(50, 50, 100, 100)])
        for operation, area, corners in (("union", 17500, [8]), ("subtract", 7500, [6]),
                                         ("intersect", 2500, [4]), ("exclude", 15000, [6, 6])):
            with self.subTest(operation):
                result = boolean(operation, [a, b])
                self.assertArea(result, area, 1e-9)
                self.assertEqual(sorted(len(s.segments) for s in result), corners)
                self.assertTrue(all(segment[0] == "L" for s in result for segment in s.segments))

    def test_circles_stay_curves_and_areas_are_exact(self):
        r, d = 60.0, 60.0
        lens = 2 * r * r * math.acos(d / (2 * r)) - (d / 2) * math.sqrt(4 * r * r - d * d)
        disc = math.pi * r * r
        a, b = Shape([circle(80, 100, r)]), Shape([circle(140, 100, r)])
        expected = {"union": 2 * disc - lens, "intersect": lens, "subtract": disc - lens, "exclude": 2 * disc - 2 * lens}
        for operation, area in expected.items():
            with self.subTest(operation):
                result = boolean(operation, [a, b])
                self.assertTrue(all(segment[0] == "C" for s in result for segment in s.segments))
                # Four béziers per circle approximate it to 0.03%; the boolean adds nothing visible.
                self.assertArea(result, area, 0.002)

    def test_arcs_are_the_original_curve(self):
        a, b = Shape([circle(0, 0, 50)]), Shape([rect(0, -100, 100, 200)])
        result = boolean("subtract", [a, b])
        self.assertEqual(len(result), 1)
        # The left half of the circle keeps its arcs: every point on them is on the circle.
        for x, y in flatten_subpath(result[0], 16):
            if x < -0.5:
                self.assertAlmostEqual(math.hypot(x, y), 50, delta=0.08)

    def test_shared_edges_merge(self):
        result = boolean("union", [Shape([rect(0, 0, 100, 100)]), Shape([rect(100, 0, 100, 100)])])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].segments), 4)
        self.assertArea(result, 20000, 1e-9)

    def test_identical_and_touching_shapes(self):
        a = Shape([rect(0, 0, 100, 100)])
        self.assertArea(boolean("union", [a, a]), 10000, 1e-9)
        self.assertEqual(boolean("subtract", [a, a]), [])
        self.assertEqual(boolean("exclude", [a, a]), [])
        corner = Shape([rect(100, 100, 50, 50)])
        self.assertArea(boolean("union", [a, corner]), 12500, 1e-9)
        self.assertEqual(boolean("intersect", [a, corner]), [])

    def test_holes_and_fill_rules(self):
        donut = Shape([rect(0, 0, 200, 200), rect(50, 50, 100, 100)], "evenodd")
        bar = Shape([rect(90, -10, 20, 220)])
        self.assertArea(boolean("subtract", [donut, bar]), 30000 - 20 * 100, 1e-9)
        union = boolean("union", [donut, bar])
        self.assertArea(union, 30000 + 20 * 100 + 20 * 20, 1e-9)
        self.assertEqual(len(union), 3)       # the outline and two holes
        # Nonzero with a same-direction inner loop fills the hole; opposite direction leaves it.
        filled = Shape([rect(0, 0, 200, 200), rect(50, 50, 100, 100)], "nonzero")
        self.assertArea(boolean("union", [filled]), 40000, 1e-9)
        hole = Shape([rect(0, 0, 200, 200), rect(50, 50, 100, 100, clockwise=False)], "nonzero")
        self.assertArea(boolean("union", [hole]), 30000, 1e-9)

    def test_many_operands_follow_figma(self):
        shapes = [Shape([circle(100, 100, 60)]), Shape([circle(160, 100, 60)]), Shape([circle(130, 150, 60)])]
        for operation, check in EXPECT.items():
            with self.subTest(operation):
                result = boolean(operation, shapes)
                rng = random.Random(3)
                for _ in range(600):
                    point = (rng.uniform(20, 240), rng.uniform(20, 230))
                    if near_boundary([s for shape in shapes for s in shape.subpaths], point, 0.5):
                        continue
                    expected = check([inside(shape.subpaths, point) for shape in shapes])
                    self.assertEqual(inside(result, point), expected, (operation, point))

    def test_random_polygons_agree_with_point_membership(self):
        rng = random.Random(11)
        for trial in range(40):
            shapes = []
            for _ in range(2):
                cx, cy = rng.uniform(40, 160), rng.uniform(40, 160)
                count = rng.randint(3, 12)
                angles = sorted(rng.uniform(0, math.tau) for _ in range(count))
                if rng.random() < 0.3:
                    rng.shuffle(angles)       # self-intersecting
                points = [(cx + math.cos(a) * rng.uniform(20, 90), cy + math.sin(a) * rng.uniform(20, 90)) for a in angles]
                shapes.append(Shape([polygon(points)], rng.choice(["nonzero", "evenodd"])))
            everything = [s for shape in shapes for s in shape.subpaths]
            for operation, check in EXPECT.items():
                result = boolean(operation, shapes)
                for _ in range(150):
                    point = (rng.uniform(-60, 260), rng.uniform(-60, 260))
                    if near_boundary(everything, point, 0.2):
                        continue
                    expected = check([inside(shape.subpaths, point, shape.rule) for shape in shapes])
                    self.assertEqual(inside(result, point), expected, (trial, operation, point))

    def test_nodes_round_trip(self):
        result = boolean("union", [Shape([circle(80, 100, 60)]), Shape([rect(80, 40, 120, 120)])])
        nodes = to_nodes(result, (20, 40))
        again = nodes_outlines(nodes)
        self.assertAlmostEqual(abs(signed_area(again)), abs(signed_area(result)), delta=1.0)

    def test_sub_cubic(self):
        curve = ((0, 0), (0, 50), (100, 50), (100, 0))
        piece = sub_cubic(curve, 0.25, 0.75)
        reverse = sub_cubic(curve, 0.75, 0.25)
        self.assertEqual(piece, tuple(reversed(reverse)))

    def test_detailed_curves_are_fast(self):
        def wavy(cx, cy, r, count):
            s = None
            for index in range(count):
                a0 = index / count * math.tau
                a1 = (index + 1) / count * math.tau
                radius = r + (12 if index % 2 else -12)
                p0 = (cx + math.cos(a0) * r, cy + math.sin(a0) * r)
                p3 = (cx + math.cos(a1) * r, cy + math.sin(a1) * r)
                mid = (a0 + a1) / 2
                c = (cx + math.cos(mid) * radius, cy + math.sin(mid) * radius)
                if s is None:
                    s = Subpath(p0, closed=True)
                s.cubic(*c, *c, *p3)
            return s
        start = time.perf_counter()
        result = boolean("union", [Shape([wavy(200, 200, 150, 120)]), Shape([wavy(260, 220, 150, 120)])])
        elapsed = time.perf_counter() - start
        self.assertTrue(result)
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
