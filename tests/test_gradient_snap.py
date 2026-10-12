import unittest
from luma_canvas.geometry import snap_gradient_point, to_world
from luma_canvas.model import LayoutObject


class GradientSnapTests(unittest.TestCase):
    def setUp(self):
        self.item = LayoutObject(id='frame', name='Frame', kind='frame', x=100, y=100, width=200, height=100)

    def test_corner_midpoint_center_and_finite_edge(self):
        for pointer, expected in (((103, 102), (100, 100)),
                                  ((202, 103), (200, 100)),
                                  ((203, 152), (200, 150)),
                                  ((102, 127), (100, 127))):
            self.assertEqual(snap_gradient_point([self.item], *pointer, 1), expected)
        self.assertEqual(snap_gradient_point([self.item], 102, 250, 1), (102, 250))

    def test_zoom_and_hidden_objects(self):
        self.assertEqual(snap_gradient_point([self.item], 104, 127, 2), (104, 127))
        self.item.visible = False
        self.assertEqual(snap_gradient_point([self.item], 102, 127, 1), (102, 127))

    def test_rotated_shape_landmark(self):
        self.item.rotation = 30
        x, y = to_world(self.item, 200, 50)
        sx, sy = snap_gradient_point([self.item], x + 2, y + 1, 1)
        self.assertAlmostEqual(sx, x)
        self.assertAlmostEqual(sy, y)
