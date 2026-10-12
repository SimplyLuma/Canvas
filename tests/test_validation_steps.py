# SPDX-License-Identifier: MPL-2.0
"""An edit checks only what it touched, and still refuses a broken document."""

from __future__ import annotations

import unittest

from luma_canvas.editing import Editor
from luma_canvas.model import Document, InvalidDocument, LayoutObject, new_id


def layer(kind: str, name: str, parent: str | None = None, **fields) -> LayoutObject:
    return LayoutObject(new_id(kind), kind, name, parent, 0, 0, 100, 100, **fields)


class StepValidationTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Steps")
        self.frame = layer("frame", "Frame")
        self.group = layer("group", "Group", self.frame.id, fill=None)
        self.box = layer("rectangle", "Box", self.group.id)
        self.other = layer("frame", "Other")
        self.document.objects += [self.frame, self.group, self.box, self.other]
        self.document.validate()
        self.editor = Editor(self.document)

    def state(self):
        return [(item.id, item.parent_id, item.name) for item in self.editor.document.objects]

    def test_a_cycle_is_refused_and_the_document_restored(self):
        before = self.state()
        with self.assertRaises(InvalidDocument):
            with self.editor.transaction("Cycle"):
                self.editor.document.object(self.frame.id).parent_id = self.group.id
        self.assertEqual(self.state(), before)
        self.assertEqual(self.editor.undo_stack, [])

    def test_a_missing_parent_is_refused(self):
        before = self.state()
        with self.assertRaises(InvalidDocument):
            with self.editor.transaction("Orphan"):
                self.editor.document.object(self.box.id).parent_id = "frame-gone"
        self.assertEqual(self.state(), before)

    def test_nesting_in_a_shape_is_refused(self):
        with self.assertRaises(InvalidDocument):
            with self.editor.transaction("Nest"):
                self.editor.document.object(self.other.id).parent_id = self.box.id

    def test_removing_a_container_with_layers_inside_is_refused(self):
        before = self.state()
        with self.assertRaises(InvalidDocument):
            with self.editor.transaction("Remove group only"):
                self.editor.document.objects = [item for item in self.editor.document.objects
                                                if item.id != self.group.id]
        self.assertEqual(self.state(), before)

    def test_a_missing_asset_is_refused(self):
        with self.assertRaises(InvalidDocument):
            with self.editor.transaction("Asset"):
                self.editor.document.object(self.box.id).asset_id = "asset-gone"

    def test_selecting_a_removed_layer_is_refused(self):
        with self.assertRaises(InvalidDocument):
            with self.editor.transaction("Select"):
                self.editor.document.objects = [item for item in self.editor.document.objects
                                                if item.id != self.other.id]
                self.editor.document.selected_ids = [self.other.id]

    def test_duplicate_ids_are_refused(self):
        with self.assertRaises(InvalidDocument):
            with self.editor.transaction("Duplicate id"):
                copy = layer("rectangle", "Copy", self.frame.id)
                copy.id = self.box.id
                self.editor.document.objects.append(copy)

    def test_a_valid_edit_is_recorded_and_undone(self):
        with self.editor.transaction("Rename"):
            self.editor.document.object(self.box.id).name = "Renamed"
        self.assertEqual(self.editor.document.object(self.box.id).name, "Renamed")
        self.editor.undo()
        self.assertEqual(self.editor.document.object(self.box.id).name, "Box")

    def test_a_broken_gesture_is_refused_and_restored(self):
        before = self.state()
        self.editor.begin_gesture("Drag")
        self.editor.document.object(self.frame.id).parent_id = self.box.id
        with self.assertRaises(InvalidDocument):
            self.editor.end_gesture()
        self.assertEqual(self.state(), before)

    def test_the_full_check_and_the_step_check_agree_on_a_valid_document(self):
        self.document.validate()
        self.document.validate([self.box.id], removed=set(), reordered=False)
        self.document.validate([item.id for item in self.document.objects], removed=set(), reordered=True)


if __name__ == "__main__":
    unittest.main()
