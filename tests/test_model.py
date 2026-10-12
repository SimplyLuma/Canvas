# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
import copy

from luma_canvas.editing import EditError, Editor
from luma_canvas.model import Document, InvalidDocument, LayoutObject, new_id


class UnifiedDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.demonstration()
        self.editor = Editor(self.document)

    def test_seed_represents_design_vector_and_publication_together(self) -> None:
        kinds = {item.kind for item in self.document.objects}
        self.assertTrue({"frame", "path", "component", "instance", "page", "spread", "text", "mask", "layout"}.issubset(kinds))
        self.assertEqual(set(self.document.workspaces), {"design", "vector", "publication"})
        self.assertTrue(any(item.text_flow_next_id for item in self.document.objects))
        self.assertEqual(len(self.document.connections), 1)

    def test_canvas_and_layer_selection_have_one_source_of_truth(self) -> None:
        target = next(item for item in self.document.objects if item.kind == "path")
        self.document.select([target.id])
        self.assertEqual(self.document.selection, [target])
        self.document.select(["missing", target.id, target.id])
        self.assertEqual(self.document.selected_ids, [target.id])

    def test_instance_relationship_survives_round_trip(self) -> None:
        instance = next(item for item in self.document.objects if item.kind == "instance")
        loaded = Document.from_dict(self.document.to_dict())
        self.assertEqual(loaded.object(instance.id).component_source_id, instance.component_source_id)
        with self.assertRaises(EditError):
            self.editor.delete([instance.component_source_id])

    def test_hierarchy_cycle_is_rejected(self) -> None:
        first = LayoutObject(new_id("group"), "group", "First")
        second = LayoutObject(new_id("group"), "group", "Second", first.id)
        first.parent_id = second.id
        empty = Document.empty(); empty.objects.extend([first, second])
        with self.assertRaises(InvalidDocument):
            empty.validate()

    def test_transaction_rolls_back_and_named_history_restores(self) -> None:
        created = self.editor.create("rectangle", 10, 20, 100, 80)
        self.editor.move([created.id], 12, -4)
        self.assertEqual((created.x, created.y), (22, 16))
        self.assertEqual(self.editor.undo_label, "Move Rectangle")
        self.editor.undo()
        self.assertEqual((self.editor.document.object(created.id).x, self.editor.document.object(created.id).y), (10, 20))
        self.editor.redo()
        self.assertEqual((self.editor.document.object(created.id).x, self.editor.document.object(created.id).y), (22, 16))

    def test_locked_objects_reject_mutation(self) -> None:
        target = self.document.objects[0]
        target.locked = True
        with self.assertRaises(EditError):
            self.editor.move([target.id], 1, 1)

    def test_vector_direction_and_instance_overrides_are_undoable(self) -> None:
        path = next(item for item in self.document.objects if item.kind == "path")
        original = copy.deepcopy(path.path)
        self.editor.reverse_path(path.id)
        self.assertEqual(path.path[0]["x"], original[-1]["x"])
        self.editor.undo()
        self.assertEqual(self.editor.document.object(path.id).path, original)
        instance = next(item for item in self.editor.document.objects if item.kind == "instance")
        instance.metadata["overrides"] = {"fill": "#ff0000"}
        self.editor.reset_instance_overrides(instance.id)
        self.assertNotIn("overrides", instance.metadata)

    def test_workspace_panel_state_is_independent(self) -> None:
        self.document.workspaces["design"].navigation_width = 310
        self.document.workspaces["publication"].navigation_width = 220
        loaded = Document.from_dict(self.document.to_dict())
        self.assertEqual(loaded.workspaces["design"].navigation_width, 310)
        self.assertEqual(loaded.workspaces["publication"].navigation_width, 220)


if __name__ == "__main__":
    unittest.main()
