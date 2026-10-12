# SPDX-License-Identifier: MPL-2.0
"""The inspector's Export tab on real GTK widgets: rows, +/−, editing and the button."""

from __future__ import annotations

import os
import time
import unittest

if os.environ.get("GTK_A11Y", "none") == "none":
    os.environ["GTK_A11Y"] = "test"

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GLib, Gtk
    HAVE_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None
except (ImportError, ValueError):  # pragma: no cover - no GTK here
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    from luma_canvas import export as exports
    from luma_canvas.editing import Editor
    from luma_canvas.inspector import ExportSection, Inspector
    from luma_canvas.model import Document, LayoutObject, new_id


def pump(seconds: float = 0.05) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


class FakeHost:
    """The part of the window the inspector talks to."""
    context = None

    def __init__(self, document: "Document") -> None:
        self.editor = Editor(document)
        self.memory: dict = {}
        self.exported: list = []
        self.frames_exported = 0
        self.inspector = None

    @property
    def document(self):
        return self.editor.document

    def edit(self, label, change, *, coalesce=False):
        with self.editor.transaction(label):
            change()
        self.inspector.refresh()
        return True

    def rename_layer(self, object_id, name):
        return True

    def align(self, action):
        pass

    def open_layer_menu(self, button):
        pass

    def export_selection(self, fmt, scale):
        pass

    def export_layers(self, object_ids):
        self.exported.append(object_ids)

    def export_frames(self):
        self.frames_exported += 1

    def remember(self, key, value):
        self.memory[key] = value

    def recall(self, key, default=None):
        return self.memory.get(key, default)

    def supports(self, capability):
        return False


def segments(values, active, callback):
    box = Gtk.Box()
    buttons = {}
    first = None
    for value, label in values:
        button = Gtk.ToggleButton(label=label)
        if first is None:
            first = button
        else:
            button.set_group(first)
        buttons[value] = button
        box.append(button)
    buttons[active].set_active(True)
    for value, button in buttons.items():
        button.connect("toggled", lambda widget, choice=value: callback(choice) if widget.get_active() else None)
    return box, buttons


@unittest.skipUnless(HAVE_DISPLAY, "needs a display and GTK 4")
class ExportInspectorTests(unittest.TestCase):
    def setUp(self) -> None:
        document = Document.empty("Export tab")
        self.card = LayoutObject(new_id("frame"), "frame", "Card", x=0, y=0, width=200, height=100, fill="#ffffff",
                                 metadata={"export_settings": [
                                     exports.default_setting(),
                                     {"format": "png", "constraint": "scale", "value": 2.0, "suffix": "@2x"}]})
        self.dot = LayoutObject(new_id("ellipse"), "ellipse", "Dot", self.card.id, 10, 10, 20, 20, fill="#000000")
        document.objects += [self.card, self.dot]
        self.host = FakeHost(document)
        self.inspector = Inspector(self.host, segments)
        self.host.inspector = self.inspector
        self.window = Gtk.Window()
        self.window.set_default_size(280, 600)
        self.window.set_child(self.inspector)
        self.window.present()
        pump(0.1)
        self.inspector.tab_buttons["export"].set_active(True)
        pump(0.05)

    def tearDown(self) -> None:
        self.window.destroy()
        pump(0.02)

    def select(self, *items) -> "ExportSection":
        self.host.document.select([item.id for item in items])
        self.inspector.refresh()
        pump(0.05)
        section = self.inspector.section("export")
        self.assertIsInstance(section, ExportSection)
        return section

    def settings(self, item):
        return exports.export_settings(self.host.document.object(item.id))

    def test_rows_show_the_layers_settings(self):
        section = self.select(self.card)
        self.assertEqual(len(section.rows), 2)
        self.assertEqual([row.size.get_text() for row in section.rows], ["1x", "2x"])
        self.assertEqual([row.suffix.get_text() for row in section.rows], ["", "@2x"])
        self.assertEqual(section.rows[0].format.get_current(), "png")
        self.assertTrue(section.button.get_visible())
        self.assertEqual(section.button.get_label(), "Export Card")
        self.assertFalse(section.empty.get_visible())

    def test_plus_minus_and_edits_are_undoable(self):
        section = self.select(self.card)
        section.add_row()
        section = self.inspector.section("export")
        self.assertEqual([row.size.get_text() for row in section.rows], ["1x", "2x", "3x"])
        self.assertEqual(self.settings(self.card)[2]["suffix"], "@3x")

        row = section.rows[2]
        row.size.set_text("512w")
        row.size.emit("activate")
        pump(0.05)
        self.assertEqual(self.settings(self.card)[2], {"format": "png", "constraint": "width", "value": 512.0,
                                                       "suffix": "@3x"})
        row = self.inspector.section("export").rows[2]
        row.suffix.set_text("-wide")
        row.suffix.emit("activate")
        row.format.set_current("jpeg")
        row.format.emit("changed", "jpeg")
        pump(0.05)
        self.assertEqual(self.settings(self.card)[2]["format"], "jpeg")
        self.assertEqual(self.settings(self.card)[2]["suffix"], "-wide")
        self.assertTrue(self.inspector.section("export").rows[2].size.get_sensitive())

        row = self.inspector.section("export").rows[1]
        row.format.set_current("svg")
        row.format.emit("changed", "svg")
        pump(0.05)
        self.assertFalse(self.inspector.section("export").rows[1].size.get_sensitive())

        self.inspector.section("export").remove_row(0)
        pump(0.05)
        self.assertEqual(len(self.settings(self.card)), 2)
        self.assertEqual(len(self.inspector.section("export").rows), 2)

        self.assertEqual(len(self.host.editor.undo_stack), 6)
        while self.host.editor.undo_stack:
            self.host.editor.undo()
        self.inspector.refresh()
        pump(0.05)
        self.assertEqual(len(self.settings(self.card)), 2)
        self.assertEqual(self.settings(self.card)[1]["value"], 2.0)

    def test_a_bad_size_reverts(self):
        section = self.select(self.card)
        row = section.rows[1]
        row.size.set_text("big")
        row.size.emit("activate")
        pump(0.05)
        self.assertEqual(row.size.get_text(), "2x")
        self.assertEqual(self.settings(self.card)[1]["value"], 2.0)
        self.assertEqual(len(self.host.editor.undo_stack), 0)

    def test_a_layer_without_settings_offers_plus(self):
        section = self.select(self.dot)
        self.assertEqual(section.rows, [])
        self.assertTrue(section.empty.get_visible())
        self.assertFalse(section.button.get_visible())
        section.add_row()
        self.assertEqual(self.settings(self.dot), [exports.default_setting()])
        section = self.inspector.section("export")
        self.assertTrue(section.button.get_visible())
        section.button.emit("clicked")
        self.assertEqual(self.host.exported, [[self.dot.id]])

    def test_mixed_selection_replaces_on_plus(self):
        section = self.select(self.card, self.dot)
        self.assertTrue(section.mixed.get_visible())
        self.assertEqual(section.rows, [])
        self.assertEqual(section.button.get_label(), "Export 2 layers")
        self.assertTrue(section.button.get_visible())
        section.add_row()
        self.assertEqual(self.settings(self.card), [exports.default_setting()])
        self.assertEqual(self.settings(self.dot), [exports.default_setting()])
        self.assertEqual(len(self.inspector.section("export").rows), 1)

    def test_nothing_selected_exports_the_page_and_all_frames(self):
        section = self.select()
        self.assertEqual(len(section.rows), 1)
        self.assertTrue(section.button.get_label().startswith("Export "))
        self.assertTrue(section.frames_button.get_visible())
        section.add_row()
        self.assertEqual(len(self.host.memory[ExportSection.PAGE_SETTINGS]), 2)
        self.assertEqual(len(self.inspector.section("export").rows), 2)
        self.assertEqual(len(self.host.editor.undo_stack), 0)
        self.inspector.section("export").button.emit("clicked")
        self.inspector.section("export").frames_button.emit("clicked")
        self.assertEqual(self.host.exported, [None])
        self.assertEqual(self.host.frames_exported, 1)

    def test_presets_popover_sets_the_size(self):
        section = self.select(self.card)
        row = section.rows[0]
        # Shared menus use a drawer at phone width; this assertion covers the desktop popover.
        self.window.set_child(None)
        self.window.destroy()
        self.window = Gtk.Window()
        self.window.set_default_size(1000, 600)
        self.window.set_child(self.inspector)
        self.window.present()
        pump(0.1)
        self.assertGreaterEqual(self.window.get_width(), 1000)
        row._show_presets()
        pump(0.05)
        self.assertIsNotNone(row.presets)
        popover = row.presets if isinstance(row.presets, Gtk.Widget) else getattr(row.presets, "popover", None)
        if isinstance(popover, Gtk.Widget):
            self.assertTrue(popover.get_visible())
            popover.popdown()
        row._apply_size("0.5x")
        pump(0.05)
        self.assertEqual(self.settings(self.card)[0]["value"], 0.5)

    def test_presets_open_a_drawer_at_phone_width(self):
        from luma_appkit import MenuDrawer

        row = self.select(self.card).rows[0]
        row._show_presets()
        pump(0.05)

        def descendants(widget):
            child = widget.get_first_child()
            while child is not None:
                yield child
                yield from descendants(child)
                child = child.get_next_sibling()

        drawers = [widget for widget in descendants(self.window)
                   if isinstance(widget, MenuDrawer) and widget.get_mapped()]
        self.assertEqual(len(drawers), 1)
        drawers[0].close()


if __name__ == "__main__":
    unittest.main()
