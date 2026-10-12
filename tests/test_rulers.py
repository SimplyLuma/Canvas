# SPDX-License-Identifier: MPL-2.0
"""Ruler ticks, guides as snap targets, and auto-scroll speed."""

import math
import unittest

from luma_canvas import rulers
from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.snapping import SnapEngine


class RulerTests(unittest.TestCase):
    def test_labels_are_nice_and_far_enough_apart(self):
        for zoom in (0.01, 0.137, 0.5, 1.0, 3.3, 25.6, 256.0):
            step = rulers.nice_step(zoom)
            self.assertGreaterEqual(step * zoom, rulers.LABEL_SPACING - 1e-6, zoom)
            self.assertLess(step * zoom, rulers.LABEL_SPACING * 2.6, zoom)
            leading = step / 10 ** math.floor(math.log10(step))
            self.assertIn(round(leading), (1, 2, 5))

    def test_minor_ticks_are_never_too_dense(self):
        for zoom in (0.02, 0.4, 1.0, 1.7, 8.0):
            step = rulers.nice_step(zoom)
            parts = rulers.minor_divisions(step, zoom)
            if parts > 1:
                self.assertGreaterEqual(step / parts * zoom, rulers.MINOR_SPACING)

    def test_ticks_follow_zoom_and_pan(self):
        major, minor = rulers.ticks(20, 1020, 1.0, 37.0)
        self.assertEqual(major[0][1] - major[0][0], 37.0)          # document 0 sits at the pan offset
        spacing = major[1][1] - major[0][1]
        self.assertEqual(spacing, 100.0)
        self.assertTrue(all(20 - 1 <= position <= 1020 + 1 for _, position in major + minor))
        zoomed, _ = rulers.ticks(20, 1020, 2.0, 37.0)
        self.assertEqual(zoomed[1][1] - zoomed[0][1], 100.0)       # 50 document px at 2x

    def test_crisp_lands_on_device_pixel_centres(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            for position in (10.0, 10.3, 99.99, 250.5):
                device = rulers.crisp(position, scale) * scale
                self.assertAlmostEqual(device - math.floor(device), 0.5)

    def test_labels(self):
        self.assertEqual(rulers.label(1200.0, 100), "1200")
        self.assertEqual(rulers.label(-50.0, 50), "-50")
        self.assertEqual(rulers.label(0.25, 0.05), "0.25")

    def test_autoscroll_speed_grows_toward_and_past_the_edge(self):
        width, height = 1000, 800
        self.assertEqual(rulers.autoscroll_velocity(500, 400, width, height), (0.0, 0.0))
        near = rulers.autoscroll_velocity(990, 400, width, height)[0]
        edge = rulers.autoscroll_velocity(1000, 400, width, height)[0]
        beyond = rulers.autoscroll_velocity(1080, 400, width, height)[0]
        self.assertLess(near, 0)
        self.assertLess(edge, near)
        self.assertLess(beyond, edge)
        self.assertGreater(rulers.autoscroll_velocity(400, 5, width, height)[1], 0)
        # With rulers shown, the zone starts at the ruler's edge.
        self.assertEqual(rulers.autoscroll_velocity(60, 400, width, height, inset_left=20)[0], 0.0)
        self.assertGreater(rulers.autoscroll_velocity(30, 400, width, height, inset_left=20)[0], 0.0)

    def test_layers_snap_to_guides(self):
        document = Document.empty("Guides")
        box = LayoutObject(new_id("rectangle"), "rectangle", "Box", None, 100, 100, 50, 50)
        guide = LayoutObject(new_id("guide"), "guide", "Guide", None, 203, 0, 0, 0, fill=None, metadata={"axis": "x"})
        document.objects.extend([box, guide])
        engine = SnapEngine(document, threshold=5, pixel_snap=True)
        result = engine.snap_move({box.id}, 51, 0)
        self.assertEqual(result.dx, 53)     # the right edge lands on the guide
        x, _y, _ = engine.snap_point(set(), 199, 10)
        self.assertEqual(x, 203)


if __name__ == "__main__":
    unittest.main()
