# SPDX-License-Identifier: Apache-2.0
"""Per-layer export settings (Figma's Export section): the rows, file names
and writing one file per row per layer."""

import copy
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from luma_canvas import export as ex
from luma_canvas.editing import Editor
from luma_canvas.model import Document, LayoutObject, new_id

try:
    import cairo  # noqa: F401
    HAS_CAIRO = True
except ImportError:
    HAS_CAIRO = False


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


class ConstraintTests(unittest.TestCase):
    def test_figma_size_field_values(self):
        self.assertEqual(ex.parse_constraint("2x"), ("scale", 2.0))
        self.assertEqual(ex.parse_constraint(" 0,5 x"), ("scale", 0.5))
        self.assertEqual(ex.parse_constraint("@3x"), ("scale", 3.0))
        self.assertEqual(ex.parse_constraint("1.5"), ("scale", 1.5))
        self.assertEqual(ex.parse_constraint("512w"), ("width", 512.0))
        self.assertEqual(ex.parse_constraint("300H"), ("height", 300.0))
        self.assertEqual(ex.parse_constraint("640px"), ("width", 640.0))
        for bad in ("", "x", "0x", "-2x", "nanx", "infx", "abc", "2y"):
            self.assertIsNone(ex.parse_constraint(bad), bad)

    def test_limits(self):
        self.assertEqual(ex.parse_constraint("1000x"), ("scale", ex.MAX_SCALE))
        self.assertEqual(ex.parse_constraint("99999w"), ("width", float(ex.MAX_PIXELS)))

    def test_labels_round_trip(self):
        for text in ex.SCALE_PRESETS:
            constraint, value = ex.parse_constraint(text)
            self.assertEqual(ex.constraint_label({"constraint": constraint, "value": value}), text)

    def test_scale_from_a_width_or_height(self):
        bounds = (0, 0, 200, 100)
        self.assertEqual(ex.export_scale({"constraint": "scale", "value": 2}, bounds), 2)
        self.assertEqual(ex.export_scale({"constraint": "width", "value": 512}, bounds), 2.56)
        self.assertEqual(ex.export_scale({"constraint": "height", "value": 50}, bounds), 0.5)
        huge = ex.export_scale({"constraint": "scale", "value": 16}, (0, 0, 10000, 10))
        self.assertLessEqual(10000 * huge, ex.MAX_PIXELS + 1e-6)


class SettingsModelTests(unittest.TestCase):
    def test_normalize_cleans_what_is_stored_or_imported(self):
        self.assertEqual(ex.normalize_setting({"format": "JPG", "value": "2", "suffix": "@2x"}),
                         {"format": "jpeg", "constraint": "scale", "value": 2.0, "suffix": "@2x"})
        self.assertEqual(ex.normalize_setting({"format": "png", "constraint": "width", "value": 511.6}),
                         {"format": "png", "constraint": "width", "value": 512.0, "suffix": ""})
        self.assertEqual(ex.normalize_setting({"format": "svg", "constraint": "bogus", "value": -3})["value"], 1.0)
        self.assertEqual(ex.normalize_setting({"suffix": "a/b:c\n"})["suffix"], "abc")
        self.assertIsNone(ex.normalize_setting({"format": "bmp"}))
        self.assertIsNone(ex.normalize_setting("png"))

    def test_settings_are_replaced_never_mutated(self):
        rows = [ex.default_setting()]
        metadata = {"figma_id": "1:2", "export_settings": rows}
        item = LayoutObject(new_id("frame"), "frame", "Card", width=10, height=10, metadata=metadata)
        read = ex.export_settings(item)
        read[0]["value"] = 9
        self.assertEqual(rows[0]["value"], 1.0)
        updated = ex.with_export_settings(item.metadata, [*read, ex.next_setting(read)])
        self.assertIsNot(updated, metadata)
        self.assertEqual(len(metadata["export_settings"]), 1)
        self.assertEqual(updated["figma_id"], "1:2")
        self.assertEqual(len(updated["export_settings"]), 2)
        self.assertNotIn("export_settings", ex.with_export_settings(updated, []))

    def test_plus_adds_one_x_then_the_next_scale_with_its_suffix(self):
        first = ex.next_setting([])
        self.assertEqual(first, ex.default_setting())
        second = ex.next_setting([first])
        self.assertEqual((second["value"], second["suffix"]), (2.0, "@2x"))
        third = ex.next_setting([first, second])
        self.assertEqual((third["value"], third["suffix"]), (3.0, "@3x"))
        svg = ex.next_setting([{**first, "format": "svg"}])
        self.assertEqual(svg["format"], "svg")

    def test_settings_undo_with_the_document(self):
        document = Document.empty("Undo")
        frame = LayoutObject(new_id("frame"), "frame", "Card", width=10, height=10)
        document.objects.append(frame)
        editor = Editor(document)
        before = copy.deepcopy(frame.metadata)
        with editor.transaction("Add export setting"):
            item = editor.document.object(frame.id)
            item.metadata = ex.with_export_settings(item.metadata, [ex.default_setting()])
        self.assertEqual(len(ex.export_settings(editor.document.object(frame.id))), 1)
        editor.undo()
        self.assertEqual(editor.document.object(frame.id).metadata, before)
        editor.redo()
        self.assertEqual(ex.export_settings(editor.document.object(frame.id))[0]["format"], "png")


class NamingTests(unittest.TestCase):
    def test_suffix_and_extension(self):
        self.assertEqual(ex.file_name("Card", ex.default_setting()), "Card.png")
        self.assertEqual(ex.file_name("Card", {"format": "png", "suffix": "@2x"}), "Card@2x.png")
        self.assertEqual(ex.file_name("Card", {"format": "jpeg", "suffix": "-dark"}), "Card-dark.jpg")
        self.assertEqual(ex.file_name("Card", {"format": "svg"}), "Card.svg")
        self.assertEqual(ex.file_name("Card", {"format": "pdf"}), "Card.pdf")

    def test_slashes_make_folders_and_unsafe_names_are_cleaned(self):
        self.assertEqual(ex.file_name("icons/home", ex.default_setting()), "icons/home.png")
        self.assertEqual(ex.file_name("../../etc/passwd", ex.default_setting()), "etc/passwd.png")
        self.assertEqual(ex.file_name('a:b*c?"', ex.default_setting()), "a_b_c__.png")
        self.assertEqual(ex.file_name("  ", ex.default_setting()), "Untitled.png")
        self.assertEqual(ex.file_name("/", {"format": "svg", "suffix": ""}), "Untitled.svg")

    def test_rows_that_would_collide_keep_both(self):
        one = ex.default_setting()
        two = {**one, "value": 2.0}
        jobs = ex.plan_exports([("Card", ["a"], [one, two, one]), ("Card", ["b"], [one])])
        self.assertEqual([job.path for job in jobs], ["Card.png", "Card@2x.png", "Card@1x.png", "Card@1x 2.png"])
        self.assertEqual(jobs[0].object_ids, ("a",))
        self.assertEqual(jobs[3].object_ids, ("b",))
        self.assertEqual(len({job.path.casefold() for job in jobs}), len(jobs))

    def test_page_targets_have_no_ids_and_bad_rows_are_skipped(self):
        jobs = ex.plan_exports([("Page 1", None, [{"format": "bmp"}, {"format": "pdf"}])])
        self.assertEqual([(job.path, job.object_ids) for job in jobs], [("Page 1.pdf", None)])


@unittest.skipUnless(HAS_CAIRO, "raster export needs pycairo")
class MultiFileExportTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Export")
        self.card = LayoutObject(new_id("frame"), "frame", "Card", x=0, y=0, width=200, height=100, fill="#527fae",
                                 metadata={"export_settings": [
                                     ex.default_setting(),
                                     {"format": "png", "constraint": "scale", "value": 2, "suffix": "@2x"},
                                     {"format": "jpeg", "constraint": "width", "value": 50, "suffix": "-thumb"},
                                     {"format": "svg", "constraint": "scale", "value": 3, "suffix": ""},
                                     {"format": "pdf", "constraint": "scale", "value": 1, "suffix": ""},
                                 ]})
        self.icon = LayoutObject(new_id("ellipse"), "ellipse", "icons/dot", x=300, y=0, width=40, height=40,
                                 fill="#bb536f", metadata={"export_settings": [
                                     {"format": "png", "constraint": "height", "value": 120, "suffix": ""}]})
        self.document.objects += [self.card, self.icon]
        self.directory = Path(tempfile.mkdtemp())

    def test_one_file_per_row_per_layer(self):
        targets = [(item.name, [item.id], ex.export_settings(item)) for item in (self.card, self.icon)]
        jobs = ex.plan_exports(targets)
        written = ex.run_exports(self.document, self.directory, jobs)
        names = sorted(str(path.relative_to(self.directory.resolve())) for path in written)
        self.assertEqual(names, ["Card-thumb.jpg", "Card.pdf", "Card.png", "Card.svg", "Card@2x.png",
                                 "icons/dot.png"])
        self.assertEqual(png_size(self.directory / "Card.png"), (200, 100))
        self.assertEqual(png_size(self.directory / "Card@2x.png"), (400, 200))
        self.assertEqual(png_size(self.directory / "icons/dot.png"), (120, 120))
        self.assertTrue((self.directory / "Card-thumb.jpg").read_bytes().startswith(b"\xff\xd8"))
        self.assertTrue((self.directory / "Card.pdf").read_bytes().startswith(b"%PDF"))
        svg = (self.directory / "Card.svg").read_text()
        self.assertIn("<svg", svg)
        self.assertIn('width="200"', svg)

    def test_each_file_holds_only_its_layer(self):
        jobs = ex.plan_exports([(self.icon.name, [self.icon.id], [ex.default_setting()])])
        ex.run_exports(self.document, self.directory, jobs)
        self.assertEqual(png_size(self.directory / "icons/dot.png"), (40, 40))

    def test_page_export_covers_everything(self):
        jobs = ex.plan_exports([("Page 1", None, [ex.default_setting()])])
        (path,) = ex.run_exports(self.document, self.directory, jobs)
        self.assertEqual(png_size(path), (340, 100))

    def test_nothing_is_written_outside_the_folder(self):
        job = ex.ExportJob("../escape.png", None, ex.default_setting())
        with self.assertRaises(ValueError):
            ex.run_exports(self.document, self.directory, [job])
        self.assertFalse((self.directory.parent / "escape.png").exists())

    def test_an_empty_layer_does_not_fail_the_batch(self):
        hidden = LayoutObject(new_id("rectangle"), "rectangle", "Hidden", width=10, height=10, visible=False)
        self.document.objects.append(hidden)
        jobs = ex.plan_exports([("Hidden", [hidden.id], [ex.default_setting()]),
                                ("Card", [self.card.id], [ex.default_setting()])])
        written = ex.run_exports(self.document, self.directory, jobs)
        self.assertEqual([path.name for path in written], ["Card.png"])
        with self.assertRaises(ValueError):
            ex.run_exports(self.document, self.directory, jobs[:1])


if __name__ == "__main__":
    unittest.main()
