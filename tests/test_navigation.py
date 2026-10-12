# SPDX-License-Identifier: MPL-2.0
"""Input router arithmetic: zoom at the pointer, pan deltas, ladders, fit."""

import math
import unittest

from luma_canvas.viewport import (
    ZOOM_MAX, ZOOM_MIN, View, fit_rect, interpolate_view, ladder_step, scroll_action,
)


def zoom_by(view, action, px, py):
    return view.zoomed_at(view.zoom * action.factor, px, py)


class ZoomTests(unittest.TestCase):
    def test_AT_N1_ctrl_wheel_zooms_at_pointer(self):
        view = View(1.0, 0.0, 0.0)
        pointer = view.to_view(500, 300)
        for _ in range(3):
            view = zoom_by(view, scroll_action(0, -1, smooth=False, ctrl=True, shift=False), *pointer)
        self.assertAlmostEqual(view.zoom, 1.728)
        doc = view.to_document(*pointer)
        self.assertAlmostEqual(doc[0], 500, delta=0.5 / view.zoom)
        self.assertAlmostEqual(doc[1], 300, delta=0.5 / view.zoom)

    def test_AT_N2_pinch_doubles_about_center(self):
        view = View(0.75, 120, -40)
        before = view.to_document(400, 400)
        view = view.zoomed_at(0.75 * 2.0, 400, 400)
        after = view.to_document(400, 400)
        self.assertAlmostEqual(view.zoom, 1.5)
        self.assertAlmostEqual(before[0], after[0])
        self.assertAlmostEqual(before[1], after[1])

    def test_smooth_ctrl_scroll_zooms_continuously(self):
        action = scroll_action(0, -10, smooth=True, ctrl=True, shift=False)
        self.assertAlmostEqual(action.factor, math.exp(0.1))

    def test_ctrl_horizontal_scroll_does_not_zoom_out(self):
        self.assertEqual(scroll_action(3, 0, smooth=True, ctrl=True, shift=False).kind, "none")

    def test_AT_N11_limits_do_not_jump_the_scroll(self):
        view = View(ZOOM_MAX, 10, 20)
        same = view.zoomed_at(ZOOM_MAX * 1.2, 300, 300)
        self.assertEqual((same.zoom, same.x, same.y), (ZOOM_MAX, 10, 20))
        low = View(ZOOM_MIN, 5, 5).zoomed_at(ZOOM_MIN / 2, 100, 100)
        self.assertEqual((low.zoom, low.x, low.y), (ZOOM_MIN, 5, 5))

    def test_ladder_steps(self):
        self.assertEqual(ladder_step(1.0, 1), 2.0)
        self.assertEqual(ladder_step(1.0, -1), 0.5)
        self.assertEqual(ladder_step(0.75, 1), 1.0)
        self.assertEqual(ladder_step(0.75, -1), 0.5)
        self.assertEqual(ladder_step(256.0, 1), ZOOM_MAX)
        self.assertEqual(ladder_step(0.01, -1), ZOOM_MIN)


class PanTests(unittest.TestCase):
    def test_AT_N5_smooth_horizontal_pan_is_exact(self):
        action = scroll_action(30, 0, smooth=True, ctrl=False, shift=False)
        self.assertEqual((action.kind, action.dx, action.dy), ("pan", -30, 0))

    def test_touchpad_pans_both_axes_at_once(self):
        action = scroll_action(12.5, -7.25, smooth=True, ctrl=False, shift=False)
        self.assertEqual((action.dx, action.dy), (-12.5, 7.25))

    def test_wheel_notch_is_fifty_pixels(self):
        action = scroll_action(0, 1, smooth=False, ctrl=False, shift=False)
        self.assertEqual((action.dx, action.dy), (0, -50))

    def test_AT_N6_shift_wheel_pans_horizontally(self):
        action = scroll_action(0, 1, smooth=False, ctrl=False, shift=True)
        self.assertEqual((action.dx, action.dy), (-50, 0))

    def test_shift_swaps_axes_even_with_touchpad_drift(self):
        action = scroll_action(0.4, 12.0, smooth=True, ctrl=False, shift=True)
        self.assertEqual((action.dx, action.dy), (-12.0, -0.4))

    def test_both_axes_use_the_same_sign(self):
        right = scroll_action(1, 0, smooth=False, ctrl=False, shift=False)
        down = scroll_action(0, 1, smooth=False, ctrl=False, shift=False)
        self.assertEqual(right.dx, down.dy)

    def test_AT_N7_horizontal_wheel_pans_horizontally(self):
        action = scroll_action(1, 0, smooth=False, ctrl=False, shift=False)
        self.assertEqual((action.dx, action.dy), (-50, 0))

    def test_AT_N3_pan_moves_offset_exactly(self):
        view = View(2.0, 10, 10).panned(120, -40)
        self.assertEqual((view.x, view.y), (130, -30))


class FitTests(unittest.TestCase):
    def test_AT_N8_fit_all_with_margin(self):
        view = fit_rect(0, 0, 4000, 3000, 1000, 800)
        left, top = view.to_view(0, 0)
        right, bottom = view.to_view(4000, 3000)
        self.assertAlmostEqual(left, 48)          # width constrains
        self.assertAlmostEqual(right, 952)
        self.assertTrue(top >= 48 and bottom <= 752)

    def test_fit_is_capped_for_tiny_content(self):
        self.assertEqual(fit_rect(0, 0, 10, 10, 1000, 800).zoom, 1.0)

    def test_AT_N9_selection_is_centered(self):
        view = fit_rect(100, 200, 300, 260, 1000, 800, max_zoom=None)
        cx, cy = view.to_view(200, 230)
        self.assertAlmostEqual(cx, 500)
        self.assertAlmostEqual(cy, 400)

    def test_animation_ends_exactly(self):
        start, end = View(1, 0, 0), View(4, -300, -200)
        final = interpolate_view(start, end, 1.0, 800, 600)
        self.assertAlmostEqual(final.zoom, 4)
        self.assertAlmostEqual(final.x, -300)
        self.assertAlmostEqual(final.y, -200)
        middle = interpolate_view(start, end, 0.5, 800, 600)
        self.assertTrue(1 < middle.zoom < 4)


if __name__ == "__main__":
    unittest.main()
