# SPDX-License-Identifier: Apache-2.0
"""The v70 seed maps to Canvas's document graph without any real store."""

import json
import tempfile
import unittest
from pathlib import Path

from luma_canvas.fixture import FixtureSession, load_fixture
from luma_canvas.creative_data import layer_specs, layer_move_target
from luma_canvas.creative_assets import (
    apply_color, apply_text_style, asset_catalog, choose_photo, insert_component_preset,
)
from luma_canvas.editing import Editor, EditError
from luma_canvas.viewport import fit_rect_with_insets


FIXTURE = Path(__file__).parent / "fixtures" / "canvas-v70.json"


class CanvasFixtureTests(unittest.TestCase):
    def test_initial_view_places_desktop_pair_and_phone_single_artboard(self):
        view = fit_rect_with_insets(
            0, 0, 2680, 1080, 1180, 686,
            inset_left=290, inset_right=316, inset_top=60, inset_bottom=90,
        )
        self.assertAlmostEqual(view.zoom, 0.21418, places=4)
        self.assertAlmostEqual(view.to_view(0, 0)[0], 290)
        self.assertAlmostEqual(view.to_view(1240, 0)[0], 555.6, places=1)
        phone = fit_rect_with_insets(
            0, 0, 1080, 1080, 374, 774,
            inset_left=16, inset_right=16, inset_top=150, inset_bottom=120,
        )
        self.assertAlmostEqual(phone.zoom, 342/1080)
        self.assertAlmostEqual(phone.to_view(0, 0)[0],16)
        self.assertAlmostEqual(phone.to_view(1080, 1080)[0],358)
        self.assertGreaterEqual(phone.to_view(0, 0)[1],150)
        self.assertLessEqual(phone.to_view(1080,1080)[1],774-120)

    def test_seed_is_complete_and_stays_in_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder)
            for filename in json.loads(FIXTURE.read_text("utf-8"))["assets"].values():
                (assets / filename).write_bytes(b"fixture image")
            document = load_fixture(FIXTURE, assets_dir=assets)
        self.assertEqual(document.name, "Launch kit")
        self.assertEqual([page.name for page in document.pages], ["Launch", "Brand", "Archive"])
        self.assertEqual(len(document.objects), 20)
        self.assertEqual(document.selected_ids, ["n4"])
        self.assertEqual(len([style for style in document.styles if style.kind == "color"]), 12)
        self.assertEqual([style.name for style in document.styles if style.kind == "text"],
                         ["Display", "Heading", "Body", "Serif"])
        self.assertEqual(document.object("n10").x, 1304)
        self.assertEqual(document.object("n16").x, 2836)
        self.assertEqual(document.object("n14").effects[0]["type"], "drop_shadow")
        self.assertEqual(sum(bool(item.effects) for item in document.objects), 4)
        self.assertEqual(len(document.embedded_assets), 8)
        self.assertEqual(document.assets[0].name, "life-hero-home.webp")
        choices = tuple(json.loads(FIXTURE.read_text("utf-8"))["photo_choices"])
        catalog = asset_catalog(document, photo_names=choices)
        self.assertEqual(len(catalog.colors), 12)
        self.assertEqual(len(catalog.texts), 4)
        self.assertEqual([photo.name for photo in catalog.photos], list(choices))
        self.assertEqual(catalog.components, ("button", "badge", "card"))
        tree = layer_specs(document)
        self.assertEqual([layer.name for layer in tree], ["Poster A3", "Web hero", "Post"])
        self.assertEqual([layer.name for layer in tree[2].children],
                         ["Date", "Photo", "Headline", "Eyebrow", "Glow"])
        self.assertEqual(layer_move_target(document, "n1", None, 0), (None, None))
        self.assertEqual(layer_move_target(document, "n15", None, 2), (None, "n1"))
        self.assertEqual(layer_move_target(document, "n4", "n15", 0), ("n15", None))
        with self.assertRaises(ValueError):
            layer_move_target(document, "n1", "n4", 0)
        session = FixtureSession(document)
        self.assertTrue(session.flush())
        self.assertFalse(session.has_saved)
        session.rename("Retitled fixture")
        self.assertEqual(document.name, "Retitled fixture")
        with self.assertRaises(RuntimeError):
            session.duplicate()

    def test_missing_asset_fails_loudly(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                load_fixture(FIXTURE, assets_dir=Path(folder))

    def test_assets_edit_document_with_one_undo_step_each(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder)
            for filename in json.loads(FIXTURE.read_text("utf-8"))["assets"].values():
                (assets / filename).write_bytes(b"fixture image")
            editor = Editor(load_fixture(FIXTURE, assets_dir=assets))
        headline = editor.document.object("n4")
        original_fill = headline.fill
        apply_color(editor, "fixture-color-6")
        self.assertEqual(headline.fill.upper(), "#6F7CFF")
        self.assertEqual(len(editor.undo_stack), 1)
        editor.undo()
        self.assertEqual(editor.document.object("n4").fill, original_fill)

        apply_text_style(editor, "fixture-text-2")
        headline = editor.document.object("n4")
        self.assertEqual(headline.metadata["font_size"], 48)
        self.assertEqual(headline.metadata["font_weight"], 600)
        self.assertEqual(len(editor.undo_stack), 1)
        editor.undo()
        self.assertEqual(editor.document.object("n4").metadata["font_size"], 142)

        studio = editor.document.object("n5")
        editor.document.select([studio.id])
        choose_photo(editor, "ari-street")
        self.assertEqual(editor.document.object("n5").asset_id, "ari-street")
        editor.undo()
        self.assertEqual(editor.document.object("n5").asset_id, "studio")
        with self.assertRaises(EditError):
            choose_photo(editor, "missing")

        editor.document.select(["n4"])
        before = len(editor.document.objects)
        insert_component_preset(editor, "button")
        self.assertEqual(len(editor.document.objects), before + 2)
        self.assertEqual([item.name for item in editor.document.selection],
                         ["Button", "Button label"])
        self.assertEqual({item.parent_id for item in editor.document.selection}, {"n1"})
        editor.undo()
        self.assertEqual(len(editor.document.objects), before)

        insert_component_preset(editor, "card")
        image = editor.document.selection[0]
        self.assertEqual(image.asset_id, "ari-street")
        self.assertEqual(image.parent_id, "n1")
        editor.undo()
        self.assertEqual(len(editor.document.objects), before)


if __name__ == "__main__":
    unittest.main()
