# SPDX-License-Identifier: MPL-2.0
"""A Figma file with blend modes, paint stacks, text strokes and blur, drawn.

The `blending` fixture goes through the real importer and is then rendered;
its probes are the numbers the compositing spec gives, so this is a round trip
from a `.fig` file to pixels. What the importer still cannot carry must show up
in the report, and nothing else may.
"""

from __future__ import annotations

import tempfile
import unittest

from pixels import HAVE_GTK
from pathlib import Path

import fig_fixtures
from blend_reference import BACKDROP, SOURCE, composite, hex_color
from pixels import HAVE_GTK, cairo_pixels, difference

from luma_canvas.fig import import_fig
from luma_canvas.paint import paint_stack, visible_paints

TOLERANCE = 3


def by_name(document, name):
    return next(item for item in document.all_objects() if item.name == name)


@unittest.skipUnless(HAVE_GTK, "needs GTK to draw")
class FigFidelityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = fig_fixtures.blending()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "blending.fig"
            path.write_bytes(cls.fixture.data)
            cls.result = import_fig(path)
        cls.document = cls.result.document

    # ── what the file carried across ─────────────────────────────────────

    def test_a_layers_blend_mode_is_kept(self):
        self.assertEqual(by_name(self.document, "Multiply").metadata["blend"], "multiply")
        self.assertEqual(by_name(self.document, "Difference group").metadata["blend"], "difference")

    def test_every_fill_is_kept_with_its_own_blend_mode(self):
        stack = paint_stack(by_name(self.document, "Two fills"), "fill")
        self.assertEqual(len(stack), 2)
        self.assertEqual([paint["blend"] for paint in stack], ["normal", "screen"])

    def test_every_stroke_is_kept_with_its_own_blend_mode(self):
        item = by_name(self.document, "Two strokes")
        stack = paint_stack(item, "stroke")
        self.assertEqual(len(stack), 2)
        self.assertEqual([paint["blend"] for paint in stack], ["normal", "multiply"])
        self.assertEqual(item.stroke_width, 16)
        self.assertEqual(item.stroke_align, "inside")

    def test_text_keeps_its_fills_and_its_stroke(self):
        item = by_name(self.document, "Outlined text")
        self.assertEqual([paint["blend"] for paint in paint_stack(item, "fill")], ["normal", "overlay"])
        self.assertEqual(len(visible_paints(item, "stroke")), 1)
        self.assertEqual(item.stroke_width, 2)

    def test_background_blur_is_kept(self):
        effects = by_name(self.document, "Frosted").effects
        self.assertEqual([effect["type"] for effect in effects], ["background_blur"])
        self.assertEqual(effects[0]["radius"], 18)

    # ── what it draws ────────────────────────────────────────────────────

    def test_the_probes_of_the_fixture_are_drawn(self):
        points = [(x, y) for x, y, _ in self.fixture.probes]
        found = cairo_pixels(self.document, points)
        for (x, y, wanted), pixel in zip(self.fixture.probes, found):
            want = tuple(int(wanted[index:index + 2], 16) for index in (1, 3, 5))
            self.assertLessEqual(difference(pixel, want), TOLERANCE,
                                 f"at {x},{y}: drew {pixel[:3]}, wanted {want}")

    def test_the_stroke_stack_draws_over_the_whole_band(self):
        """Both strokes cover the band, so the blend holds along the edge."""
        wanted = tuple(int(hex_color(composite("multiply", BACKDROP, SOURCE, 1.0))[index:index + 2], 16)
                       for index in (1, 3, 5))
        for point in ((22, 150), (34, 150), (70, 122), (70, 178)):
            pixel = cairo_pixels(self.document, [point])[0]
            self.assertLessEqual(difference(pixel, wanted), TOLERANCE, f"at {point}: {pixel[:3]}")

    def test_text_draws_its_stroke_outside_its_glyphs(self):
        """The stroke reaches past the glyph fills, so the layer draws wider."""
        import copy

        from luma_canvas.export import export_bounds
        document = copy.deepcopy(self.document)
        item = by_name(document, "Outlined text")
        with_stroke = export_bounds(document, [item.id])
        item.strokes, item.stroke, item.stroke_width = [], None, 0.0
        without = export_bounds(document, [item.id])
        self.assertGreater(with_stroke[2], without[2])

    def test_background_blur_mixes_what_is_behind_it(self):
        """The frosted layer blurs the hard edge under it, on every renderer."""
        import copy
        document = copy.deepcopy(self.document)
        plain = copy.deepcopy(self.document)
        by_name(plain, "Frosted").effects = []
        edge = 400.0
        points = [(edge - 8, 150), (edge + 8, 150)]
        blurred = cairo_pixels(document, points)
        unblurred = cairo_pixels(plain, points)
        self.assertGreater(unblurred[1][0] - unblurred[0][0], 60,
                           f"the fixture must have a hard edge: {unblurred}")
        # Each side of the edge is pulled towards the other.
        self.assertGreater(blurred[0][0] - unblurred[0][0], 20, f"dark side: {blurred} vs {unblurred}")
        self.assertGreater(unblurred[1][0] - blurred[1][0], 20, f"light side: {blurred} vs {unblurred}")

    def test_a_group_keeps_its_own_effects(self):
        group = by_name(self.document, "Shadowed group")
        self.assertEqual(group.kind, "group")
        self.assertEqual([effect["type"] for effect in group.effects], ["drop_shadow"])
        # Not copied onto the layers inside it, which would shadow each other.
        self.assertEqual([by_name(self.document, name).effects for name in ("Lower block", "Upper block")],
                         [[], []])

    def test_a_group_shadow_survives_saving_and_reopening(self):
        import tempfile
        from pathlib import Path

        from luma_canvas.persistence import DocumentStore
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "blending.canvas"
            DocumentStore.save(self.document, path)
            reopened = DocumentStore.load(path)
        self.assertEqual([effect["type"] for effect in by_name(reopened, "Shadowed group").effects],
                         ["drop_shadow"])

    def test_show_behind_transparent_areas_is_kept_per_shadow(self):
        self.assertIs(by_name(self.document, "Glass behind").effects[0]["behind"], True)
        self.assertIs(by_name(self.document, "Glass knockout").effects[0]["behind"], False)

    def test_svg_export_draws_the_group_effect_once(self):
        from luma_canvas.export import svg_text
        group = by_name(self.document, "Shadowed group")
        svg = svg_text(self.document, [group.id])
        self.assertEqual(svg.count("feGaussianBlur"), 1)
        self.assertIn("filter=", svg)

    def test_a_progressive_blur_grows_along_its_line(self):
        """The layer's left edge stays sharp; its right edge is smeared."""
        left_out, left_in, right_in, right_out = cairo_pixels(
            self.document, [(137, 340), (143, 340), (336, 340), (344, 340)])
        self.assertGreater(left_out[0], 230, f"outside the sharp edge: {left_out}")
        self.assertLess(left_in[0], 25, f"inside the sharp edge: {left_in}")
        self.assertGreater(right_in[0], 60, f"inside the blurred edge: {right_in}")
        self.assertLess(right_out[0], 220, f"outside the blurred edge: {right_out}")

    def test_smoothed_corners_are_drawn_as_squircles(self):
        from luma_canvas import geometry
        item = by_name(self.document, "Squircle")
        self.assertAlmostEqual(geometry.corner_smoothing_of(item), 0.6, places=4)
        shape = geometry.outline(item)[0]
        # A squircle corner is a cubic, an arc and a cubic, not one arc.
        self.assertGreater(len([part for part in shape.segments if part[0] == "C"]), 4)
        # It leaves the edge at (1 + smoothing) x radius from the corner.
        self.assertAlmostEqual(shape.start[0], 19.2, places=3)

    def test_a_leaning_layer_keeps_its_shear(self):
        from luma_canvas import geometry
        item = by_name(self.document, "Leaning")
        self.assertAlmostEqual(geometry.skew_of(item), 0.5, places=4)
        self.assertEqual((item.rotation, item.width, item.height), (0.0, 60.0, 60.0))
        # Its bottom edge sits half its height to the right of its top edge.
        top = geometry.to_world(item, 0, 0)
        bottom = geometry.to_world(item, 0, item.height)
        self.assertAlmostEqual(bottom[0] - top[0], 30.0, places=4)
        # And a point is found in the leaning box, not the upright one.
        self.assertAlmostEqual(geometry.to_local(item, *bottom)[1], item.height, places=4)

    def test_svg_export_leans_the_same_way(self):
        from luma_canvas.export import svg_text
        item = by_name(self.document, "Leaning")
        self.assertIn("matrix(1 0 0.5 1", svg_text(self.document, [item.id]))

    def test_every_export_format_writes_the_whole_fixture(self):
        import tempfile
        from pathlib import Path

        from luma_canvas.export import export_jpeg, export_pdf, export_png, export_svg
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            for write, name in ((export_png, "out.png"), (export_jpeg, "out.jpg"),
                                (export_pdf, "out.pdf"), (export_svg, "out.svg")):
                path = write(self.document, out / name)
                self.assertGreater(path.stat().st_size, 1000, name)

    def test_a_stroke_of_any_paint_is_kept_and_drawn(self):
        item = by_name(self.document, "Angular stroke")
        stack = paint_stack(item, "stroke")
        self.assertEqual([paint["type"] for paint in stack], ["angular"])
        from luma_canvas.export import svg_text
        svg = svg_text(self.document, [item.id])
        # The band is masked and the paint drawn through it.
        self.assertIn("<mask", svg)
        self.assertIn("mask=", svg)

    def test_svg_export_carries_what_it_can_express(self):
        from luma_canvas.export import svg_text
        svg = svg_text(self.document)
        self.assertIn("mix-blend-mode:multiply", svg)
        self.assertIn("mix-blend-mode:screen", svg)
        self.assertIn("mix-blend-mode:difference", svg)
        # The squircle is a path of cubics, not a rounded rectangle.
        self.assertGreaterEqual(svg.count("C"), 8)

    def test_an_unrelated_renderer_draws_the_svg_the_same(self):
        """The SVG writer mirrors the canvas: librsvg draws the same colors.

        Background blur is the documented exception (SVG 1.1 has no backdrop
        filter), so only the probes outside it are compared.
        """
        try:
            import gi
            gi.require_version("Rsvg", "2.0")
            from gi.repository import Rsvg
        except (ImportError, ValueError):  # pragma: no cover - no librsvg here
            self.skipTest("needs librsvg")
        import cairo

        from luma_canvas.export import export_bounds, svg_text
        left, top, width, height = export_bounds(self.document)
        handle = Rsvg.Handle.new_from_data(svg_text(self.document).encode())
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
        context = cairo.Context(surface)
        viewport = Rsvg.Rectangle()
        viewport.x, viewport.y, viewport.width, viewport.height = 0, 0, width, height
        handle.render_document(context, viewport)
        surface.flush()
        data = bytes(surface.get_data())
        stride = surface.get_stride()
        for x, y, wanted in self.fixture.probes:
            offset = int(y - top) * stride + int(x - left) * 4
            alpha = data[offset + 3] or 255
            found = tuple(min(255, round(data[offset + index] * 255 / alpha)) for index in (2, 1, 0))
            want = tuple(int(wanted[index:index + 2], 16) for index in (1, 3, 5))
            self.assertLessEqual(difference(found, want), 6, f"svg at {x},{y}: {found} wanted {want}")

    # ── what the report says ─────────────────────────────────────────────

    def test_the_report_only_names_what_is_really_missing(self):
        topics = {finding.topic for finding in self.result.report.findings}
        self.assertEqual(topics, {"blend_approximate"})
        finding = next(item for item in self.result.report.findings if item.topic == "blend_approximate")
        self.assertEqual(finding.extra, {"Linear dodge"})

    def test_gaps_that_closed_are_gone_from_the_topics(self):
        from luma_canvas.fig.report import TOPICS
        # Progressive blur stays, for a file that has no direction in it.
        self.assertIn("progressive_blur", TOPICS)
        for topic in ("blend", "paint_blend", "strokes", "stroke_paint", "text_stroke", "background_blur",
                      "group_effects", "shadow_behind", "corner_smoothing", "skew"):
            self.assertNotIn(topic, TOPICS, topic)


if __name__ == "__main__":
    unittest.main()
