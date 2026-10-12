# SPDX-License-Identifier: MPL-2.0
"""Use as mask (Ctrl+Alt+M): the bottom layer masks the layers above it, as in Figma."""

import unittest

from luma_canvas import geometry
from luma_canvas.editing import EditError, Editor
from luma_canvas.layers import masked_layers
from luma_canvas.model import Document, LayoutObject, new_id


def obj(kind, name, x, y, w, h, parent=None, **fields):
    return LayoutObject(new_id(kind), kind, name, parent, x, y, w, h, **fields)


class MaskTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Masks")
        self.editor = Editor(self.document)
        self.frame = obj("frame", "Card", 0, 0, 600, 400)
        self.circle = obj("ellipse", "Circle", 100, 100, 200, 200, self.frame.id, fill="#000000")
        self.photo = obj("rectangle", "Photo", 50, 50, 400, 300, self.frame.id, fill="#e02080")
        self.caption = obj("text", "Caption", 60, 320, 200, 30, self.frame.id, text="Hi")
        self.document.objects.extend([self.frame, self.circle, self.photo, self.caption])

    @property
    def doc(self):
        return self.editor.document

    def test_several_layers_become_a_mask_group_with_the_bottom_one_as_mask(self):
        mask = self.editor.use_as_mask([self.photo.id, self.circle.id])
        self.assertEqual(mask.id, self.circle.id)
        group = self.doc.object(self.doc.object(self.circle.id).parent_id)
        self.assertEqual(group.name, "Mask group")
        self.assertTrue(group.metadata.get("mask_group"))
        self.assertEqual(group.parent_id, self.frame.id)
        self.assertEqual([child.name for child in self.doc.children(group.id)], ["Circle", "Photo"])
        self.assertEqual(geometry.mask_mode(self.doc.object(self.circle.id)), "alpha")
        # A mask group is as big as its mask, not its content.
        self.assertEqual((group.x, group.y, group.width, group.height), (100, 100, 200, 200))
        self.assertEqual(self.doc.selected_ids, [group.id])
        self.assertEqual(self.editor.undo_label, "Use as mask")

    def test_undo_and_redo(self):
        before = self.doc.to_dict()
        self.editor.use_as_mask([self.photo.id, self.circle.id])
        self.editor.undo()
        self.assertEqual(self.doc.to_dict()["objects"], before["objects"])
        self.editor.redo()
        self.assertEqual(geometry.mask_mode(self.doc.object(self.circle.id)), "alpha")

    def test_one_layer_masks_the_layers_above_it_in_place(self):
        self.editor.use_as_mask([self.circle.id])
        self.assertEqual(self.doc.object(self.circle.id).parent_id, self.frame.id)
        self.assertEqual(masked_layers(self.doc.children(self.frame.id)), {self.photo.id, self.caption.id})

    def test_release_removes_the_mask_and_the_group_it_made(self):
        self.editor.use_as_mask([self.photo.id, self.circle.id])
        group_id = self.doc.object(self.circle.id).parent_id
        released = self.editor.release_mask([group_id])
        self.assertIsNone(self.doc.object(group_id))
        self.assertEqual(sorted(released), sorted([self.circle.id, self.photo.id]))
        self.assertIsNone(geometry.mask_mode(self.doc.object(self.circle.id)))
        self.assertEqual([child.name for child in self.doc.children(self.frame.id)], ["Circle", "Photo", "Caption"])
        self.assertEqual(self.editor.undo_label, "Release mask")
        self.editor.undo()
        self.assertEqual(geometry.mask_mode(self.doc.object(self.circle.id)), "alpha")

    def test_releasing_a_mask_in_a_plain_group_keeps_the_group(self):
        self.editor.use_as_mask([self.circle.id])
        self.editor.release_mask([self.circle.id])
        self.assertIsNone(geometry.mask_mode(self.doc.object(self.circle.id)))
        self.assertEqual(self.doc.object(self.circle.id).parent_id, self.frame.id)

    def test_mask_types(self):
        self.editor.use_as_mask([self.circle.id], mode="outline")
        self.assertEqual(geometry.mask_mode(self.doc.object(self.circle.id)), "outline")
        self.editor.set_mask_mode([self.circle.id], "luminance")
        self.assertEqual(geometry.mask_mode(self.doc.object(self.circle.id)), "luminance")
        with self.assertRaises(EditError):
            self.editor.set_mask_mode([self.circle.id], "sparkle")

    def test_layers_from_different_parents_cannot_share_a_mask(self):
        other = obj("rectangle", "Elsewhere", 900, 0, 10, 10)
        self.doc.objects.append(other)
        with self.assertRaises(EditError):
            self.editor.use_as_mask([other.id, self.circle.id])

    def test_masked_content_is_only_hit_inside_the_mask(self):
        self.editor.use_as_mask([self.photo.id, self.circle.id])
        tree = geometry.Tree(self.doc.objects)
        inside = geometry.hit_chain(tree, 200, 200)
        self.assertEqual(inside[-1].name, "Photo")
        # Over the photo but outside the circle: nothing masked is there to click.
        outside = geometry.hit_chain(tree, 70, 70)
        self.assertNotIn("Photo", [item.name for item in outside])

    def test_hidden_masks_do_not_mask(self):
        self.editor.use_as_mask([self.circle.id])
        self.doc.object(self.circle.id).visible = False
        self.assertEqual(masked_layers(self.doc.children(self.frame.id)), set())
        tree = geometry.Tree(self.doc.objects)
        self.assertEqual(geometry.hit_chain(tree, 70, 70)[-1].name, "Photo")

    def test_masks_survive_save_and_load(self):
        import tempfile
        from pathlib import Path
        from luma_canvas.persistence import DocumentStore
        self.editor.use_as_mask([self.photo.id, self.circle.id], mode="luminance")
        with tempfile.TemporaryDirectory() as folder:
            loaded = DocumentStore.load(DocumentStore.save(self.doc, Path(folder) / "Mask.canvas"))
        self.assertEqual(geometry.mask_mode(loaded.object(self.circle.id)), "luminance")
        self.assertTrue(loaded.object(loaded.object(self.circle.id).parent_id).metadata["mask_group"])


if __name__ == "__main__":
    unittest.main()
