# SPDX-License-Identifier: MPL-2.0
"""Color conversions, parsing, paints and the current style."""

import tempfile
import unittest
from pathlib import Path

from luma_canvas.editing import Editor
from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.paint import (
    Color, RecentColors, convert_paint, css_string, document_colors, format_number,
    hsl_to_rgb, hsv_to_rgb, interpolate, object_paint, assign_paint, oklch_to_srgb,
    parse_color, rgb_to_hsl, rgb_to_hsv, solid, srgb_to_oklch, to_hex,
)


def close(test, color, expected, places=3):
    for got, want in zip((color.r, color.g, color.b, color.a), expected):
        test.assertAlmostEqual(got, want, places=places)


class ConversionTests(unittest.TestCase):
    def test_hex_round_trips_every_byte(self):
        for value in range(256):
            text = f"#{value:02x}{255 - value:02x}{(value * 7) % 256:02x}"
            self.assertEqual(to_hex(parse_color(text)), text)

    def test_hex_forms(self):
        self.assertEqual(to_hex(parse_color("4F7DF3")), "#4f7df3")
        self.assertEqual(to_hex(parse_color("#abc")), "#aabbcc")
        self.assertEqual(to_hex(parse_color("#abcd")), "#aabbccdd")
        self.assertAlmostEqual(parse_color("#11223380").a, 128 / 255)

    def test_hsv_round_trip(self):
        for text in ("#4f7df3", "#ff0000", "#00ff00", "#123456", "#fedcba", "#808080"):
            color = parse_color(text)
            self.assertEqual(to_hex(hsv_to_rgb(*rgb_to_hsv(color))), text)

    def test_hsl_round_trip(self):
        for text in ("#4f7df3", "#f2994a", "#000000", "#ffffff", "#336699"):
            color = parse_color(text)
            self.assertEqual(to_hex(hsl_to_rgb(*rgb_to_hsl(color))), text)

    def test_oklch_round_trip_in_gamut(self):
        for text in ("#4f7df3", "#f2994a", "#336699", "#d9d9d9"):
            color = parse_color(text)
            self.assertEqual(to_hex(oklch_to_srgb(*srgb_to_oklch(color))), text)

    def test_oklch_out_of_gamut_is_mapped_not_wrapped(self):
        color = oklch_to_srgb(0.7, 0.5, 145)
        for channel in (color.r, color.g, color.b):
            self.assertTrue(0.0 <= channel <= 1.0)
        self.assertGreater(color.g, color.r)   # still green

    def test_hue_of_gray_is_zero_not_nan(self):
        self.assertEqual(rgb_to_hsv(Color(0.5, 0.5, 0.5))[0], 0.0)


class ParsingTests(unittest.TestCase):
    def test_AT_P4_space_rgb_with_alpha(self):
        color = parse_color("rgb(10 20 30 / 50%)")
        self.assertEqual(color.bytes(), (10, 20, 30))
        self.assertAlmostEqual(color.a, 0.5)

    def test_comma_rgba_and_percentages(self):
        close(self, parse_color("rgba(255, 0, 0, 0.25)"), (1, 0, 0, 0.25))
        close(self, parse_color("rgb(100%, 50%, 0%)"), (1, 0.5, 0, 1))

    def test_hsl_forms(self):
        self.assertEqual(to_hex(parse_color("hsl(0 100% 50%)")), "#ff0000")
        self.assertEqual(to_hex(parse_color("hsla(120, 100%, 25%, 1)")), "#008000")
        self.assertEqual(to_hex(parse_color("hsl(0.5turn 100% 50%)")), "#00ffff")

    def test_hsb_and_named_colors(self):
        self.assertEqual(to_hex(parse_color("hsb(240 100% 100%)")), "#0000ff")
        self.assertEqual(to_hex(parse_color("RebeccaPurple")), "#663399")
        self.assertEqual(parse_color("transparent").a, 0.0)

    def test_oklch_input(self):
        red = parse_color("oklch(62.8% 0.2577 29.23)")
        self.assertEqual(to_hex(red), "#ff0000")
        self.assertIsNotNone(parse_color("oklch(0.7 0.1 250 / 40%)"))

    def test_invalid_input_is_none(self):
        for text in ("", "#12", "#12345", "rgb(1 2)", "nope", "hsl(a b c)"):
            self.assertIsNone(parse_color(text), text)

    def test_css_string(self):
        self.assertEqual(css_string(parse_color("#4f7df3")), "rgb(79 125 243)")
        self.assertEqual(css_string(parse_color("rgb(79 125 243 / 40%)")), "rgb(79 125 243 / 40%)")

    def test_format_number_trims(self):
        self.assertEqual(format_number(320.0), "320")
        self.assertEqual(format_number(48.5), "48.5")
        self.assertEqual(format_number(12.2549), "12.25")


class PaintTests(unittest.TestCase):
    def test_solid_to_linear_fades_the_same_color(self):
        paint = convert_paint(solid(parse_color("#4f7df3")), "linear")
        self.assertEqual(paint["type"], "linear")
        self.assertEqual(len(paint["stops"]), 2)
        self.assertEqual(paint["stops"][0]["color"]["a"], 1.0)
        self.assertEqual(paint["stops"][1]["color"]["a"], 0.0)

    def test_AT_P6_interpolated_middle_stop(self):
        paint = {"type": "linear", "stops": [
            {"position": 0.0, "color": Color(1, 0, 0).to_dict()},
            {"position": 1.0, "color": Color(0, 0, 1).to_dict()}]}
        close(self, interpolate(paint["stops"], 0.5), (0.5, 0, 0.5, 1))

    def test_paint_round_trips_through_an_object(self):
        item = LayoutObject(new_id("rectangle"), "rectangle", "R")
        gradient = convert_paint(solid(parse_color("#336699")), "radial")
        assign_paint(item, "fill", gradient)
        self.assertEqual(item.fill, "#336699")
        self.assertEqual(object_paint(item, "fill")["type"], "radial")
        assign_paint(item, "fill", solid(parse_color("#ff0000")))
        self.assertIsNone(item.fill_gradient)
        self.assertEqual(item.fill, "#ff0000")

    def test_document_colors_most_used_first(self):
        objects = [LayoutObject(new_id("r"), "rectangle", "R", fill=fill)
                   for fill in ("#111111", "#222222", "#222222", None)]
        self.assertEqual(document_colors(objects), ["#222222", "#111111"])

    def test_recent_colors_are_capped_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            recent = RecentColors(Path(folder) / "recent.json")
            for value in range(20):
                recent.add(Color(value / 255, 0, 0))
            recent.add(Color(19 / 255, 0, 0))
            values = recent.load()
            self.assertEqual(len(values), 16)
            self.assertEqual(values[0], "#130000")
            self.assertEqual(values.count("#130000"), 1)


class CurrentStyleTests(unittest.TestCase):
    def setUp(self):
        self.editor = Editor(Document.empty("Style"))

    def test_new_document_defaults(self):
        rectangle = self.editor.create("rectangle", 0, 0, 10, 10)
        self.assertEqual(rectangle.fill, "#d9d9d9")
        self.assertIsNone(rectangle.stroke)

    def test_AT_C1_chip_fill_is_used_by_the_next_rectangle(self):
        style = self.editor.document.current_style
        style["shape"]["fill"] = "#4f7df3"
        self.editor.set_current_style(style)
        rectangle = self.editor.create("rectangle", 100, 100, 200, 100)
        self.assertEqual(rectangle.fill, "#4f7df3")

    def test_AT_C2_editing_a_selection_teaches_the_style(self):
        rectangle = self.editor.create("rectangle", 0, 0, 10, 10)
        self.editor.set_property([rectangle.id], "fill", "#f2994a")
        ellipse = self.editor.create("ellipse", 0, 0, 10, 10)
        self.assertEqual(ellipse.fill, "#f2994a")

    def test_AT_C3_lines_keep_the_line_style(self):
        rectangle = self.editor.create("rectangle", 0, 0, 10, 10)
        self.editor.set_property([rectangle.id], "fill", "#f2994a")
        line = self.editor.create("line", 0, 0, 100, 0)
        self.assertEqual(line.stroke, "#000000")
        self.assertEqual(line.stroke_width, 1.0)
        self.assertIsNone(line.fill)

    def test_AT_C4_default_style_preference(self):
        self.editor.style_follows_edits = False
        rectangle = self.editor.create("rectangle", 0, 0, 10, 10)
        self.editor.set_property([rectangle.id], "fill", "#f2994a")
        self.assertEqual(self.editor.create("ellipse", 0, 0, 10, 10).fill, "#d9d9d9")

    def test_AT_C5_frames_stay_white(self):
        style = self.editor.document.current_style
        style["shape"]["fill"] = "#4f7df3"
        self.editor.set_current_style(style)
        self.assertEqual(self.editor.create("frame", 0, 0, 10, 10).fill, "#ffffff")

    def test_AT_C6_duplicates_keep_their_own_style(self):
        rectangle = self.editor.create("rectangle", 0, 0, 10, 10, fill="#ff0000", use_style=False)
        before = dict(self.editor.document.current_style["shape"])
        copy = self.editor.duplicate([rectangle.id])[0]
        self.assertEqual(copy.fill, "#ff0000")
        self.assertEqual(self.editor.document.current_style["shape"], before)

    def test_AT_C8_line_without_stroke_borrows_shape_fill(self):
        style = self.editor.document.current_style
        style["shape"]["fill"] = "#4f7df3"
        style["line"]["stroke"] = None
        self.editor.set_current_style(style)
        path = self.editor.create("path", 0, 0, 10, 10, path=[{"x": 0, "y": 0}, {"x": 10, "y": 10}])
        self.assertEqual(path.stroke, "#4f7df3")
        self.assertEqual(path.stroke_width, 1.0)

    def test_set_paint_gradient_teaches_the_style(self):
        rectangle = self.editor.create("rectangle", 0, 0, 10, 10)
        gradient = convert_paint(solid(parse_color("#336699")), "linear")
        self.editor.set_paint([rectangle.id], "fill", gradient)
        ellipse = self.editor.create("ellipse", 0, 0, 10, 10)
        self.assertEqual(ellipse.fill_gradient["type"], "linear")

    def test_style_survives_save_and_load(self):
        style = self.editor.document.current_style
        style["shape"]["fill"] = "#4f7df3"
        self.editor.set_current_style(style)
        restored = Document.from_dict(self.editor.document.to_dict())
        self.assertEqual(restored.current_style["shape"]["fill"], "#4f7df3")

    def test_old_documents_get_a_default_style(self):
        value = self.editor.document.to_dict()
        value.pop("current_style")
        self.assertEqual(Document.from_dict(value).current_style["shape"]["fill"], "#d9d9d9")


if __name__ == "__main__":
    unittest.main()
