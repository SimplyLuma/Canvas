# SPDX-License-Identifier: MPL-2.0
"""Copy and paste with hierarchy, styles, images and Figma's placement rules."""

import copy
import unittest

from luma_canvas import clipboard
from luma_canvas.editing import Editor
from luma_canvas.model import Asset, Document, LayoutObject, Style, new_id


def obj(kind, name, x, y, w, h, parent=None, **fields):
    return LayoutObject(new_id(kind), kind, name, parent, x, y, w, h, **fields)


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Source")
        self.editor = Editor(self.document)
        self.frame = obj("frame", "Card", 100, 100, 300, 200)
        self.title = obj("text", "Title", 120, 120, 200, 30, self.frame.id, text="Hello")
        self.badge = obj("group", "Badge", 150, 180, 60, 20, self.frame.id)
        self.dot = obj("ellipse", "Dot", 150, 180, 20, 20, self.badge.id)
        self.document.objects.extend([self.frame, self.title, self.badge, self.dot])

    def test_payload_round_trips_through_bytes(self):
        payload = clipboard.serialize(self.document, [self.frame.id])
        self.assertEqual(clipboard.decode(clipboard.encode(payload))["roots"], [self.frame.id])
        self.assertIsNone(clipboard.decode(b"not json"))

    def test_copying_a_child_and_its_parent_copies_the_parent_once(self):
        payload = clipboard.serialize(self.document, [self.frame.id, self.title.id])
        self.assertEqual(payload["roots"], [self.frame.id])
        self.assertEqual(len(payload["objects"]), 4)

    def test_paste_keeps_hierarchy_with_new_ids_and_offsets_over_itself(self):
        payload = clipboard.serialize(self.document, [self.frame.id])
        roots = clipboard.paste(self.editor, payload)
        document = self.editor.document
        self.assertEqual(len(document.objects), 8)
        copy = document.object(roots[0])
        self.assertNotEqual(copy.id, self.frame.id)
        self.assertEqual((copy.x, copy.y), (110, 110))
        children = document.children(copy.id)
        self.assertEqual(sorted(child.name for child in children), ["Badge", "Title"])
        badge = next(child for child in children if child.name == "Badge")
        self.assertEqual([item.name for item in document.children(badge.id)], ["Dot"])
        self.assertEqual(document.selected_ids, roots)
        self.assertEqual(len(self.editor.undo_stack), 1)
        self.editor.undo()
        self.assertEqual(len(self.editor.document.objects), 4)

    def test_paste_into_a_selected_frame(self):
        target = obj("frame", "Screen", 1000, 0, 400, 800)
        self.document.objects.append(target)
        payload = clipboard.serialize(self.document, [self.title.id])
        self.document.select([target.id])
        roots = clipboard.paste(self.editor, payload)
        pasted = self.editor.document.object(roots[0])
        self.assertEqual(pasted.parent_id, target.id)
        self.assertTrue(1000 <= pasted.x and pasted.x + pasted.width <= 1400)

    def test_paste_beside_the_selection_goes_into_its_parent(self):
        payload = clipboard.serialize(self.document, [self.dot.id])
        self.document.select([self.title.id])
        roots = clipboard.paste(self.editor, payload)
        self.assertEqual(self.editor.document.object(roots[0]).parent_id, self.frame.id)

    def test_paste_over_selection_centers_on_it(self):
        payload = clipboard.serialize(self.document, [self.dot.id])
        target = obj("rectangle", "Target", 500, 500, 100, 100)
        self.document.objects.append(target)
        self.document.select([target.id])
        roots = clipboard.paste(self.editor, payload, over_selection=True)
        pasted = self.editor.document.object(roots[0])
        self.assertEqual((pasted.x + pasted.width / 2, pasted.y + pasted.height / 2), (550, 550))

    def test_cross_document_paste_brings_styles_and_images(self):
        style = Style(new_id("style"), "Brand", "color", {"value": "#123456"})
        asset = Asset(new_id("asset"), "Photo", "image/png", embedded_name="assets/photo.png")
        self.document.styles.append(style)
        self.document.assets.append(asset)
        self.document.embedded_assets["assets/photo.png"] = b"\x89PNG fake"
        picture = obj("image", "Photo", 0, 0, 10, 10, asset_id=asset.id, style_ids=[style.id], fill=None)
        self.document.objects.append(picture)
        payload = clipboard.decode(clipboard.encode(clipboard.serialize(self.document, [picture.id])))
        other = Editor(Document.empty("Other"))
        roots = clipboard.paste(other, payload)
        pasted = other.document.object(roots[0])
        self.assertEqual(pasted.asset_id, asset.id)
        # Bytes land under the asset id, which is what saving writes out.
        self.assertEqual(other.document.embedded_assets[asset.id], b"\x89PNG fake")
        self.assertEqual(pasted.style_ids, [style.id])
        other.document.validate()

    def test_paste_out_of_view_lands_in_the_middle_of_the_view(self):
        payload = clipboard.serialize(self.document, [self.dot.id])
        other = Editor(Document.empty("Other"))
        roots = clipboard.paste(other, payload, view_rect=(5000, 5000, 6000, 6000))
        pasted = other.document.object(roots[0])
        self.assertEqual((pasted.x + 10, pasted.y + 10), (5500, 5500))

    def test_image_layer_embeds_bytes_once(self):
        first = clipboard.image_layer(self.editor, b"bytes", "image/png", 400, 200, center=(0, 0),
                                      max_size=(100, 100))
        clipboard.image_layer(self.editor, b"bytes", "image/png", 400, 200, center=(0, 0))
        self.assertEqual(len(self.editor.document.assets), 1)
        self.assertEqual((first.width, first.height), (100, 50))

    def _image_fill_document(self):
        """A layer with an image paint, as a saved package loads it: bytes by asset id."""
        asset = Asset(new_id("asset"), "Texture", "image/png", embedded_name="texture.png", width=8, height=8)
        self.document.assets.append(asset)
        self.document.embedded_assets[asset.id] = b"\x89PNG texture"
        card = obj("rectangle", "Card", 0, 0, 80, 60, fill="#ffffff",
                   metadata={"extra_fills": [{"type": "image", "asset_id": asset.id, "scale_mode": "fill",
                                              "opacity": 1.0, "visible": True}]})
        self.document.objects.append(card)
        return asset, card

    def test_copy_and_paste_image_fills_by_asset_id(self):
        asset, card = self._image_fill_document()
        payload = clipboard.decode(clipboard.encode(clipboard.serialize(self.document, [card.id])))
        self.assertEqual([entry["id"] for entry in payload["assets"]], [asset.id])
        self.assertIn("data", payload["assets"][0])
        other = Editor(Document.empty("Other"))
        roots = clipboard.paste(other, payload)
        pasted = other.document.object(roots[0])
        paint = pasted.metadata["extra_fills"][0]
        self.assertEqual(paint["asset_id"], asset.id)
        self.assertEqual(other.document.embedded_assets[asset.id], b"\x89PNG texture")
        other.document.validate()

    def test_pasted_images_survive_saving(self):
        import tempfile
        from pathlib import Path
        from luma_canvas.persistence import DocumentStore
        asset, card = self._image_fill_document()
        payload = clipboard.serialize(self.document, [card.id])
        other = Editor(Document.empty("Other"))
        clipboard.paste(other, payload)
        picture = clipboard.image_layer(other, b"\x89PNG pasted", "image/png", 4, 4, center=(0, 0))
        with tempfile.TemporaryDirectory() as folder:
            path = DocumentStore.save(other.document, Path(folder) / "Pasted.canvas")
            loaded = DocumentStore.load(path)
        self.assertEqual(loaded.embedded_assets[asset.id], b"\x89PNG texture")
        self.assertEqual(loaded.embedded_assets[picture.asset_id], b"\x89PNG pasted")
        self.assertFalse(any(value.missing for value in loaded.assets))

    def test_old_embedded_name_keys_are_still_read_and_saved(self):
        import tempfile
        from pathlib import Path
        from luma_canvas.model import canonical_asset_keys
        from luma_canvas.persistence import DocumentStore
        asset = Asset(new_id("asset"), "Old", "image/png", embedded_name="assets/old.png")
        self.document.assets.append(asset)
        self.document.embedded_assets["assets/old.png"] = b"old bytes"
        picture = obj("image", "Old", 0, 0, 4, 4, asset_id=asset.id, fill=None)
        self.document.objects.append(picture)
        payload = clipboard.serialize(self.document, [picture.id])
        self.assertTrue(payload["assets"][0].get("data"))
        with tempfile.TemporaryDirectory() as folder:
            loaded = DocumentStore.load(DocumentStore.save(self.document, Path(folder) / "Old.canvas"))
        self.assertEqual(loaded.embedded_assets[asset.id], b"old bytes")
        self.assertTrue(canonical_asset_keys(self.document))
        self.assertEqual(self.document.embedded_assets[asset.id], b"old bytes")

    def test_image_layer_reuses_an_asset_found_by_its_id_keyed_bytes(self):
        # A package-loaded asset: bytes by asset id, an embedded name unrelated to the digest.
        asset = Asset(new_id("asset"), "Photo", "image/png", embedded_name="photo.png", width=4, height=4)
        self.document.assets.append(asset)
        self.document.embedded_assets[asset.id] = b"\x89PNG same"
        layer = clipboard.image_layer(self.editor, b"\x89PNG same", "image/png", 4, 4, center=(0, 0))
        document = self.editor.document
        self.assertEqual(layer.asset_id, asset.id)
        self.assertEqual(len(document.assets), 1)
        self.assertEqual(document.embedded_assets[asset.id], b"\x89PNG same")

    def test_image_layer_reuses_an_old_paste_keyed_by_embedded_name_without_dropping_it(self):
        asset = Asset(new_id("asset"), "Old", "image/png", embedded_name="assets/old.png")
        self.document.assets.append(asset)
        self.document.embedded_assets["assets/old.png"] = b"old bytes"
        layer = clipboard.image_layer(self.editor, b"old bytes", "image/png", 4, 4, center=(0, 0))
        document = self.editor.document
        self.assertEqual(layer.asset_id, asset.id)
        self.assertEqual(len(document.assets), 1)
        self.assertEqual(document.embedded_assets[asset.id], b"old bytes")
        self.assertEqual(document.embedded_assets["assets/old.png"], b"old bytes")

    def test_image_layer_restores_bytes_an_asset_lost(self):
        import hashlib
        digest = hashlib.sha256(b"lost").hexdigest()[:24]
        asset = Asset(new_id("asset"), "Lost", "image/png", embedded_name=f"assets/{digest}.png")
        self.document.assets.append(asset)
        layer = clipboard.image_layer(self.editor, b"lost", "image/png", 4, 4, center=(0, 0))
        self.assertEqual(layer.asset_id, asset.id)
        self.assertEqual(self.editor.document.embedded_assets[asset.id], b"lost")

    def test_different_bytes_make_a_new_asset(self):
        first = clipboard.image_layer(self.editor, b"one", "image/png", 4, 4, center=(0, 0))
        second = clipboard.image_layer(self.editor, b"two", "image/png", 4, 4, center=(0, 0))
        document = self.editor.document
        self.assertNotEqual(first.asset_id, second.asset_id)
        self.assertEqual(document.embedded_assets[second.asset_id], b"two")

    def test_paste_reuses_a_same_bytes_asset_under_another_id_in_the_stacks(self):
        from luma_canvas.paint import paint_stack, set_paint_stack
        source = Document.empty("Source")
        texture = Asset(new_id("asset"), "Texture", "image/png", embedded_name="texture.png")
        source.assets.append(texture)
        source.embedded_assets[texture.id] = b"\x89PNG texture"
        image_paint = {"type": "image", "asset_id": texture.id, "scale_mode": "fill", "opacity": 1.0,
                       "visible": True, "blend": "normal"}
        card = obj("rectangle", "Card", 0, 0, 80, 60, fill=None)
        set_paint_stack(card, "fill", [image_paint])
        set_paint_stack(card, "stroke", [dict(image_paint)])
        picture = obj("image", "Picture", 100, 0, 8, 8, fill=None, asset_id=texture.id)
        source.objects += [card, picture]
        payload = clipboard.decode(clipboard.encode(clipboard.serialize(source, [card.id, picture.id])))
        original = copy.deepcopy(payload)

        target = Document.empty("Target")
        mine = Asset(new_id("asset"), "Mine", "image/png", embedded_name="assets/mine.png")
        target.assets.append(mine)
        target.embedded_assets["assets/mine.png"] = b"\x89PNG texture"   # an old paste, same bytes
        editor = Editor(target)
        roots = clipboard.paste(editor, payload)
        document = editor.document
        self.assertEqual([asset.id for asset in document.assets], [mine.id])
        pasted_card, pasted_picture = (document.object(value) for value in roots)
        self.assertEqual(pasted_card.fills[0]["asset_id"], mine.id)
        self.assertEqual(pasted_card.strokes[0]["asset_id"], mine.id)
        self.assertEqual(paint_stack(pasted_card, "fill")[0]["asset_id"], mine.id)
        self.assertEqual(pasted_picture.asset_id, mine.id)
        self.assertEqual(document.embedded_assets[mine.id], b"\x89PNG texture")
        self.assertEqual(payload, original, "the payload is not changed in place")
        document.validate()

    def test_paste_fills_in_bytes_for_an_asset_id_that_lost_them(self):
        asset, card = self._image_fill_document()
        payload = clipboard.serialize(self.document, [card.id])
        other_document = Document.empty("Other")
        other_document.assets.append(Asset(asset.id, "Texture", "image/png", embedded_name="texture.png"))
        other = Editor(other_document)
        clipboard.paste(other, payload)
        self.assertEqual(len(other.document.assets), 1)
        self.assertEqual(other.document.embedded_assets[asset.id], b"\x89PNG texture")

    def test_missing_pictures_in_paint_stacks_are_marked_on_new_lists(self):
        image_paint = {"type": "image", "asset_id": "asset-gone", "opacity": 1.0, "visible": True}
        fills = [image_paint]
        item = obj("rectangle", "Card", 0, 0, 10, 10, fill=None, fills=fills, strokes=[])
        clipboard._drop_missing_image_paints(item, set())
        self.assertIsNot(item.fills, fills)
        self.assertEqual(fills[0]["asset_id"], "asset-gone")
        self.assertNotIn("asset_id", item.fills[0])
        self.assertEqual(item.fills[0]["missing_asset_id"], "asset-gone")

    def test_image_paints_without_their_picture_do_not_dangle(self):
        asset, card = self._image_fill_document()
        payload = clipboard.serialize(self.document, [card.id])
        payload["assets"] = []
        other = Editor(Document.empty("Other"))
        pasted = other.document.object(clipboard.paste(other, payload)[0])
        paint = pasted.metadata["extra_fills"][0]
        self.assertNotIn("asset_id", paint)
        self.assertEqual(paint["missing_asset_id"], asset.id)


if __name__ == "__main__":
    unittest.main()
