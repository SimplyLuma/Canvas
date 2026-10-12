# SPDX-License-Identifier: MPL-2.0
"""Appearance on real GTK widgets: blend mode and corner smoothing."""

from __future__ import annotations

import os
import unittest

if os.environ.get("GTK_A11Y", "none") == "none":
    os.environ["GTK_A11Y"] = "test"

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk
    HAVE_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None
except (ImportError, ValueError):  # pragma: no cover - no GTK here
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    from test_export_inspector_gtk import FakeHost, pump, segments

    from luma_canvas import geometry
    from luma_canvas.inspector import AppearanceSection, Inspector
    from luma_canvas.model import Document, LayoutObject, new_id


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class AppearanceSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        document = Document.empty("Appearance")
        self.card = LayoutObject(new_id("rectangle"), "rectangle", "Card", None, 0, 0, 200, 120,
                                 fill="#ffffff", radius=24)
        document.objects.append(self.card)
        self.host = FakeHost(document)
        self.inspector = Inspector(self.host, segments)
        self.host.inspector = self.inspector
        self.window = Gtk.Window()
        self.window.set_default_size(280, 700)
        self.window.set_child(self.inspector)
        self.window.present()
        pump(0.1)
        document.select([self.card.id])
        self.inspector.refresh()
        pump(0.05)
        self.section = self.inspector.section("appearance")
        self.assertIsInstance(self.section, AppearanceSection)

    def tearDown(self) -> None:
        self.window.destroy()
        pump(0.02)

    def test_a_blend_mode_can_be_chosen_and_comes_back(self):
        self.section._set_blend("multiply")
        pump(0.05)
        self.assertEqual(self.host.document.objects[0].metadata["blend"], "multiply")
        self.section._set_blend("normal")
        pump(0.05)
        self.assertNotIn("blend", self.host.document.objects[0].metadata)

    def test_corner_smoothing_reaches_the_shape(self):
        self.assertIsNotNone(self.section.smoothing)
        self.section._set_smoothing(60.0)
        pump(0.05)
        card = self.host.document.objects[0]
        self.assertAlmostEqual(card.metadata["corner_smoothing"], 0.6, places=4)
        self.assertAlmostEqual(geometry.outline(card)[0].start[0], 24 * 1.6, places=3)
        # And the field shows it again after a refresh.
        self.inspector.refresh()
        pump(0.05)
        self.assertAlmostEqual(self.section.smoothing.get_value(), 60.0, places=3)

    def test_no_smoothing_leaves_the_metadata_clean(self):
        self.section._set_smoothing(60.0)
        pump(0.05)
        self.section._set_smoothing(0.0)
        pump(0.05)
        self.assertNotIn("corner_smoothing", self.host.document.objects[0].metadata)


if __name__ == "__main__":
    unittest.main()
