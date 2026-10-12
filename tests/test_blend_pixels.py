# SPDX-License-Identifier: MPL-2.0
"""Every blend mode draws the color the compositing spec says it must.

These run the Cairo path, which is what PNG and PDF export draw with.
`tests/runtime_blend_pixels.py` checks the same cases on the OpenGL and Vulkan
renderers in a real window.
"""

from __future__ import annotations

import unittest

from blend_reference import CASES, MODES, as_bytes, case_document
from pixels import HAVE_GTK, cairo_pixels, difference

TOLERANCE = 2


@unittest.skipUnless(HAVE_GTK, "needs GTK to draw")
class BlendPixelTests(unittest.TestCase):
    def test_every_case_matches_the_reference(self):
        failures = []
        for case in CASES:
            document, spot = case_document(case)
            found = cairo_pixels(document, [spot])[0]
            wanted = as_bytes(case.expected)
            if difference(found, wanted) > TOLERANCE:
                failures.append(f"{case.name}: drew {found[:3]}, wanted {wanted}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_all_sixteen_modes_are_covered(self):
        self.assertEqual(len(MODES), 15)
        covered = {case.mode for case in CASES if case.kind == "layer"}
        self.assertEqual(covered, set(MODES))

    def test_png_export_matches_the_canvas(self):
        import tempfile
        from pathlib import Path

        import cairo

        from luma_canvas.export import export_png
        for case in CASES:
            if case.kind != "layer" or case.alpha != 1.0:
                continue
            document, spot = case_document(case)
            with tempfile.TemporaryDirectory() as folder:
                path = export_png(document, Path(folder) / "out.png")
                surface = cairo.ImageSurface.create_from_png(str(path))
            data = bytes(surface.get_data())
            offset = int(spot[1]) * surface.get_stride() + int(spot[0]) * 4
            found = (data[offset + 2], data[offset + 1], data[offset])
            self.assertLessEqual(difference(found, as_bytes(case.expected)), TOLERANCE, case.name)


if __name__ == "__main__":
    unittest.main()
