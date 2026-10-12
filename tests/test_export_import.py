# SPDX-License-Identifier: Apache-2.0
"""Export in every format Canvas claims, and the importer registry."""

import tempfile
import unittest
from pathlib import Path

from luma_canvas.export import EXPORT_FORMATS, export
from luma_canvas.importers import (
    ImportFormat, format_for, formats, import_document, readable_formats, register,
)
from luma_canvas.model import Document, LayoutObject, new_id

try:
    import cairo  # noqa: F401
    HAS_CAIRO = True
except ImportError:      # a development machine without pycairo
    HAS_CAIRO = False

needs_cairo = unittest.skipUnless(
    HAS_CAIRO, "raster and PDF export need pycairo, which the image has"
)


def _document() -> Document:
    document = Document.empty("Export")
    document.objects.append(
        LayoutObject(new_id("rectangle"), "rectangle", "Plate",
                     x=0, y=0, width=200, height=120, fill="#527fae")
    )
    document.objects.append(
        LayoutObject(new_id("text"), "text", "Caption",
                     x=10, y=140, width=200, height=30, text="Hello", fill="#283038")
    )
    document.validate()
    return document


@needs_cairo
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.document = _document()
        self.directory = Path(tempfile.mkdtemp())

    def test_every_declared_format_writes_a_file(self):
        for fmt in EXPORT_FORMATS:
            with self.subTest(fmt=fmt):
                destination = self.directory / f"out.{fmt}"
                export(self.document, destination, fmt=fmt)
                self.assertTrue(destination.exists(), fmt)
                self.assertGreater(destination.stat().st_size, 0, fmt)

    def test_scale_factors_change_the_pixel_size(self):
        one = self.directory / "one.png"
        three = self.directory / "three.png"
        export(self.document, one, fmt="png", scale=1)
        export(self.document, three, fmt="png", scale=3)
        self.assertGreater(three.stat().st_size, one.stat().st_size)

    def test_a_scale_factor_multiplies_the_pixels(self):
        import struct

        def size(scale: int) -> tuple[int, int]:
            destination = self.directory / f"sized-{scale}.png"
            export(self.document, destination, fmt="png", scale=scale)
            return struct.unpack(">II", destination.read_bytes()[16:24])

        one = size(1)
        self.assertEqual(size(2), (one[0] * 2, one[1] * 2))
        self.assertEqual(size(3), (one[0] * 3, one[1] * 3))

    def test_a_pdf_is_a_pdf(self):
        destination = self.directory / "out.pdf"
        export(self.document, destination, fmt="pdf")
        self.assertTrue(destination.read_bytes().startswith(b"%PDF"))

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(ValueError):
            export(self.document, self.directory / "out.tiff", fmt="tiff")

    def test_exporting_one_object_crops_to_it(self):
        import struct

        target = self.document.objects[0]
        destination = self.directory / "one-object.png"
        export(self.document, destination, fmt="png", object_ids=[target.id])
        width, height = struct.unpack(">II", destination.read_bytes()[16:24])
        self.assertEqual((width, height), (200, 120))


def _png_pixels(path):
    """(width, height, rgba bytes) through cairo, which reads what it writes."""
    import cairo
    surface = cairo.ImageSurface.create_from_png(str(path))
    width, height, stride = surface.get_width(), surface.get_height(), surface.get_stride()
    data = bytes(surface.get_data())

    def pixel(x, y):
        b, g, r, a = data[y * stride + x * 4:y * stride + x * 4 + 4]
        return (r, g, b, a)
    return width, height, pixel


def _clip_document(align="outside", radius=0.0):
    document = Document.empty("Clip")
    frame = LayoutObject(new_id("frame"), "frame", "Frame", x=0, y=0, width=200, height=100, fill="#ffffff",
                         stroke="#2060e0", stroke_width=10, stroke_align=align, radius=radius)
    child = LayoutObject(new_id("rectangle"), "rectangle", "Overflow", frame.id, 150, 20, 120, 40, fill="#e02080",
                         stroke="#e02080", stroke_width=12, stroke_align="outside",
                         effects=[{"type": "drop_shadow", "x": 0, "y": 30, "blur": 0, "spread": 0,
                                   "color": "#e02080", "visible": True}])
    document.objects.extend([frame, child])
    document.validate()
    return document, frame, child


@needs_cairo
class ClipExportTests(unittest.TestCase):
    """Exports clip a frame's children exactly as the canvas does (Figma)."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    PINK, BLUE = (0xe0, 0x20, 0x80), (0x20, 0x60, 0xe0)

    def test_png_clips_children_and_keeps_the_frame_stroke_on_top(self):
        # (alignment, how far the frame's stroke reaches out, where its band starts and ends in world x)
        for align, reach, band in (("outside", 10, (200, 210)), ("center", 5, (195, 205)), ("inside", 0, (190, 200))):
            with self.subTest(align=align):
                document, frame, _child = _clip_document(align)
                destination = self.directory / f"clip-{align}.png"
                export(document, destination, fmt="png", object_ids=[frame.id])
                width, height, pixel = _png_pixels(destination)
                # The export covers the frame and its own stroke, not the overflow.
                self.assertEqual((width, height), (200 + reach * 2, 100 + reach * 2))
                # The frame's own stroke is drawn over the child, and not clipped.
                self.assertEqual(pixel(reach + band[0] + 2, reach + 40)[:3], self.BLUE)
                self.assertEqual(pixel(reach + band[1] - 2, reach + 40)[:3], self.BLUE)
                # Inside the frame the child shows; its shadow too, down to the frame's edge.
                self.assertEqual(pixel(reach + 160, reach + 40)[:3], self.PINK)
                self.assertEqual(pixel(reach + 160, reach + 85)[:3], self.PINK)

    def test_png_clip_leaves_nothing_past_the_frame(self):
        document, frame, child = _clip_document("inside")
        frame.stroke = None
        frame.stroke_width = 0
        # Export the whole page: the child's overflow must not widen or paint the export.
        destination = self.directory / "page.png"
        export(document, destination, fmt="png")
        width, height, pixel = _png_pixels(destination)
        self.assertEqual((width, height), (200, 100))
        self.assertEqual(pixel(199, 40)[:3], self.PINK)
        self.assertEqual(pixel(199, 3)[:3], (0xff, 0xff, 0xff))

    def test_rounded_frames_clip_their_corners(self):
        document, frame, child = _clip_document("inside", radius=40)
        child.x, child.y, child.width, child.height = -50, -50, 300, 200
        child.effects = []
        destination = self.directory / "rounded.png"
        export(document, destination, fmt="png", object_ids=[frame.id])
        _width, _height, pixel = _png_pixels(destination)
        self.assertEqual(pixel(2, 2)[3], 0)
        self.assertEqual(pixel(100, 50)[:3], (0xe0, 0x20, 0x80))

    def test_turning_clip_content_off_exports_the_overflow(self):
        document, frame, _child = _clip_document("inside")
        frame.clip_content = False
        destination = self.directory / "overflow.png"
        export(document, destination, fmt="png", object_ids=[frame.id])
        width, height, pixel = _png_pixels(destination)
        self.assertGreater(width, 260)
        self.assertEqual(pixel(260, 40 - 0)[:3], (0xe0, 0x20, 0x80))

    def test_svg_clips_children_and_draws_the_frame_stroke_after_them(self):
        document, frame, child = _clip_document("outside", radius=12)
        from luma_canvas.export import svg_text
        text = svg_text(document, [frame.id])
        self.assertIn("<clipPath", text)
        clip_use = text.index('clip-path="url(#clip')
        child_at = text.index(child.id)
        self.assertLess(clip_use, child_at)
        # The frame's stroke group follows the clipped children and is not inside the clip.
        after_children = text[child_at:]
        self.assertIn('stroke-width="20"', after_children)
        self.assertIn("C", text[text.index("<clipPath"):text.index("</clipPath>")])  # rounded corners are curves

    def test_pdf_export_draws_through_the_scene(self):
        document, frame, _child = _clip_document("outside")
        destination = self.directory / "clip.pdf"
        export(document, destination, fmt="pdf", object_ids=[frame.id])
        data = destination.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        from luma_canvas.export import export_bounds
        self.assertEqual(export_bounds(document, [frame.id]), (-10.0, -10.0, 220.0, 120.0))


class RegistryTests(unittest.TestCase):
    def test_svg_is_readable(self):
        self.assertIn("svg", [fmt.key for fmt in readable_formats()])

    def test_the_formats_that_are_not_readable_are_still_declared(self):
        keys = {fmt.key for fmt in formats()}
        self.assertTrue({"fig", "indd", "ai", "psd"} <= keys)

    def test_an_unreadable_format_says_so_rather_than_failing_oddly(self):
        with self.assertRaises(ValueError) as raised:
            import_document(Path("/tmp/whatever.indd"))
        self.assertIn("not readable yet", str(raised.exception))

    def test_a_suffix_finds_its_format(self):
        self.assertEqual(format_for(Path("a.indd")).label, "InDesign")

    def test_a_new_format_is_a_registration_not_a_special_case(self):
        register(ImportFormat("test", "Test", (".luma-test",),
                              lambda path: Document.empty("from test")))
        document = import_document(Path("/tmp/example.luma-test"))
        self.assertEqual(document.name, "from test")


if __name__ == "__main__":
    unittest.main()
