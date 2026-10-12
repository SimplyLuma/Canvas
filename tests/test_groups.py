# SPDX-License-Identifier: MPL-2.0
"""Group, ungroup and frame selection (Figma's behavior)."""

import unittest

from luma_canvas.editing import Editor
from luma_canvas.model import Document, LayoutObject, new_id


def obj(kind, name, x, y, w, h, parent=None, **fields):
    return LayoutObject(new_id(kind), kind, name, parent, x, y, w, h, **fields)


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Groups")
        self.frame = obj("frame", "Screen", 0, 0, 800, 600)
        self.back = obj("rectangle", "Back", 10, 10, 50, 50, self.frame.id)
        self.a = obj("rectangle", "A", 100, 100, 50, 50, self.frame.id)
        self.middle = obj("rectangle", "Middle", 400, 400, 50, 50, self.frame.id)
        self.b = obj("ellipse", "B", 200, 150, 40, 60, self.frame.id)
        self.document.objects.extend([self.frame, self.back, self.a, self.middle, self.b])
        self.editor = Editor(self.document)

    def names(self, parent_id):
        return [item.name for item in self.editor.document.objects if item.parent_id == parent_id]

    def test_group_wraps_at_the_topmost_layer_with_union_bounds(self):
        group = self.editor.group([self.b.id, self.a.id])
        self.assertEqual((group.x, group.y, group.width, group.height), (100, 100, 140, 110))
        self.assertEqual(group.parent_id, self.frame.id)
        self.assertEqual(self.names(self.frame.id), ["Back", "Middle", "Group"])
        self.assertEqual(self.names(group.id), ["A", "B"])
        self.assertEqual(self.editor.document.selected_ids, [group.id])
        self.assertEqual(len(self.editor.undo_stack), 1)
        self.editor.undo()
        self.assertEqual(self.names(self.frame.id), ["Back", "A", "Middle", "B"])

    def test_ungroup_keeps_positions_and_order(self):
        group = self.editor.group([self.a.id, self.b.id])
        released = self.editor.ungroup([group.id])
        self.assertEqual(self.names(self.frame.id), ["Back", "Middle", "A", "B"])
        self.assertEqual(set(released), {self.a.id, self.b.id})
        self.assertEqual(self.editor.document.object(self.a.id).x, 100)

    def test_group_bounds_follow_children(self):
        group = self.editor.group([self.a.id, self.b.id])
        self.editor.move([self.b.id], 100, 0)
        group = self.editor.document.object(group.id)
        self.assertEqual(group.width, 240)

    def test_empty_groups_disappear(self):
        group = self.editor.group([self.a.id])
        self.editor.delete([self.a.id])
        self.assertIsNone(self.editor.document.object(group.id))

    def test_moving_a_group_moves_its_children(self):
        group = self.editor.group([self.a.id, self.b.id])
        self.editor.move([group.id], 10, 20)
        self.assertEqual((self.editor.document.object(self.a.id).x, self.editor.document.object(self.a.id).y), (110, 120))

    def test_groups_nest(self):
        inner = self.editor.group([self.a.id, self.b.id])
        outer = self.editor.group([inner.id, self.middle.id])
        self.assertEqual(self.editor.document.object(inner.id).parent_id, outer.id)
        self.assertEqual(self.editor.document.object(outer.id).width, 350)

    def test_frame_selection_uses_the_frame_style(self):
        frame = self.editor.group([self.a.id, self.b.id], kind="frame")
        self.assertEqual((frame.kind, frame.fill, frame.clip_content), ("frame", "#ffffff", False))
        self.assertEqual(self.editor.undo_label, "Frame selection")


if __name__ == "__main__":
    unittest.main()


class GroupPaintTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Paint")
        self.rects = [obj("rectangle", f"R{index}", index * 100, 0, 50, 50, fill="#cccccc") for index in range(3)]
        self.document.objects.extend(self.rects)
        self.editor = Editor(self.document)
        self.group = self.editor.group([rect.id for rect in self.rects])

    def test_stroke_on_a_group_strokes_its_children(self):
        self.editor.set_property([self.group.id], "stroke", "#000000")
        self.editor.set_property([self.group.id], "stroke_width", 14)
        self.editor.set_property([self.group.id], "stroke_align", "outside")
        document = self.editor.document
        group = document.object(self.group.id)
        self.assertEqual((group.stroke, group.stroke_width), (None, 0.0))
        for rect in self.rects:
            item = document.object(rect.id)
            self.assertEqual((item.stroke, item.stroke_width, item.stroke_align), ("#000000", 14, "outside"))
        self.assertEqual(len(self.editor.undo_stack), 4)

    def test_paint_targets_reach_through_nested_groups(self):
        from luma_canvas.model import paint_targets
        outer = self.editor.group([self.group.id])
        targets = paint_targets(self.editor.document, [self.editor.document.object(outer.id)])
        self.assertEqual(sorted(item.name for item in targets), ["R0", "R1", "R2"])

    def test_documents_with_group_strokes_are_migrated(self):
        value = self.editor.document.to_dict()
        for entry in value["objects"]:
            if entry["kind"] == "group":
                entry.update(stroke="#ff0000", stroke_width=14, stroke_align="outside")
        restored = Document.from_dict(value)
        group = restored.object(self.group.id)
        self.assertIsNone(group.stroke)
        self.assertEqual({restored.object(rect.id).stroke for rect in self.rects}, {"#ff0000"})
