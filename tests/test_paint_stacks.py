# SPDX-License-Identifier: MPL-2.0
"""Paint stacks: several fills and strokes per layer (schema 2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from luma_canvas.components import OVERRIDABLE
from luma_canvas.editing import EditError, Editor
from luma_canvas.model import SCHEMA_VERSION, Document, LayoutObject, migrate_paint_stacks, new_id
from luma_canvas.paint import (Color, gradient, move_stack_paint, paint_label, paint_opacity, paint_stack,
                               replace_stack_paint, set_paint_stack, solid, visible_paints, with_paint_opacity)


def rectangle(**fields) -> LayoutObject:
    return LayoutObject(new_id("rectangle"), "rectangle", "Box", None, 0, 0, 100, 100, **fields)


RED = solid(Color(1, 0, 0))
BLUE = solid(Color(0, 0, 1))


class StackModelTests(unittest.TestCase):
    def test_schema_version_is_two(self):
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_scalar_fill_is_the_bottom_paint(self):
        item = rectangle(fill="#ff0000")
        stack = paint_stack(item, "fill")
        self.assertEqual(len(stack), 1)
        self.assertEqual(stack[0]["type"], "solid")
        self.assertEqual((stack[0]["visible"], stack[0]["blend"], stack[0]["opacity"]), (True, "normal", 1.0))

    def test_set_stack_mirrors_the_bottom_paint(self):
        item = rectangle(fill=None)
        set_paint_stack(item, "fill", [RED, BLUE])
        self.assertEqual(item.fill, "#ff0000")
        self.assertIsNone(item.fill_gradient)
        self.assertEqual([paint["color"]["b"] for paint in item.fills], [0, 1])

    def test_a_hidden_bottom_paint_leaves_no_mirror_but_stays_in_the_stack(self):
        item = rectangle(fill=None)
        set_paint_stack(item, "fill", [{**RED, "visible": False}, BLUE])
        self.assertIsNone(item.fill)
        self.assertEqual(len(paint_stack(item, "fill")), 2)
        self.assertEqual(len(visible_paints(item, "fill")), 1)

    def test_a_gradient_bottom_paint_mirrors_into_fill_gradient(self):
        item = rectangle(fill=None)
        ramp = gradient("linear", [(0, Color(1, 0, 0)), (1, Color(0, 0, 1))])
        set_paint_stack(item, "fill", [ramp, RED])
        self.assertEqual(item.fill_gradient["type"], "linear")
        self.assertEqual(item.fill, "#ff0000")

    def test_legacy_writers_changing_the_scalar_move_the_bottom_paint_only(self):
        item = rectangle(fill=None)
        set_paint_stack(item, "fill", [RED, BLUE])
        item.fill = "#00ff00"
        stack = paint_stack(item, "fill")
        self.assertEqual(len(stack), 2)
        self.assertEqual((stack[0]["color"]["g"], stack[1]["color"]["b"]), (1, 1))

    def test_replace_keeps_visibility_and_blend(self):
        item = rectangle(fill=None)
        set_paint_stack(item, "fill", [RED, {**BLUE, "blend": "multiply", "visible": False}])
        replace_stack_paint(item, "fill", 1, RED)
        top = paint_stack(item, "fill")[1]
        self.assertEqual((top["blend"], top["visible"], top["color"]["r"]), ("multiply", False, 1))

    def test_replace_with_none_removes(self):
        item = rectangle(fill=None)
        set_paint_stack(item, "fill", [RED, BLUE])
        replace_stack_paint(item, "fill", 0, None)
        self.assertEqual(item.fill, "#0000ff")
        self.assertEqual(len(item.fills), 1)

    def test_move_reorders(self):
        item = rectangle(fill=None)
        set_paint_stack(item, "fill", [RED, BLUE])
        move_stack_paint(item, "fill", 0, 1)
        self.assertEqual(item.fill, "#0000ff")

    def test_labels_and_opacity(self):
        self.assertEqual(paint_label(solid(Color(1, 0.5, 0, 0.5))), "FF8000")
        self.assertEqual(paint_label(gradient("radial", [(0, Color(1, 0, 0)), (1, Color(0, 0, 1))])), "Radial")
        half = solid(Color(1, 0, 0, 0.5))
        self.assertAlmostEqual(paint_opacity(half), 0.5)
        folded = with_paint_opacity(half, 0.25)
        self.assertEqual(folded["color"]["a"], 1)
        self.assertAlmostEqual(paint_opacity(folded), 0.25)

    def test_schema_one_extra_fills_and_hidden_fill_migrate(self):
        item = rectangle(fill=None, metadata={"hidden_fill": "#ff0000", "extra_fills": [BLUE]})
        self.assertTrue(migrate_paint_stacks(item))
        self.assertEqual(len(item.fills), 2)
        self.assertIs(item.fills[0]["visible"], False)
        self.assertNotIn("extra_fills", item.metadata)
        self.assertNotIn("hidden_fill", item.metadata)

    def test_stacks_survive_save_and_load(self):
        document = Document.empty("Stacks")
        item = rectangle(fill=None)
        set_paint_stack(item, "fill", [RED, {**BLUE, "opacity": 0.5, "blend": "screen"}])
        set_paint_stack(item, "stroke", [BLUE, RED])
        document.objects.append(item)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stacks.json"
            path.write_text(document.to_json() if hasattr(document, "to_json") else __import__("json").dumps(document.to_dict()))
            loaded = Document.from_dict(__import__("json").loads(path.read_text()))
        again = loaded.object(item.id)
        self.assertEqual(paint_stack(again, "fill"), paint_stack(item, "fill"))
        self.assertEqual(paint_stack(again, "stroke"), paint_stack(item, "stroke"))

    def test_instances_override_a_stack_as_one_value(self):
        self.assertIn("fills", OVERRIDABLE)
        self.assertIn("strokes", OVERRIDABLE)


class StackEditorTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Stacks")
        self.item = rectangle(fill="#ff0000")
        self.document.objects.append(self.item)
        self.editor = Editor(self.document)

    def current(self):
        return self.editor.document.object(self.item.id)

    def test_add_puts_a_paint_on_top_and_undo_removes_it(self):
        self.editor.add_stack_paint([self.item.id], "fill", BLUE)
        self.assertEqual(len(paint_stack(self.current(), "fill")), 2)
        self.assertEqual(paint_stack(self.current(), "fill")[1]["color"]["b"], 1)
        self.editor.undo()
        self.assertEqual(len(paint_stack(self.current(), "fill")), 1)

    def test_update_visible_opacity_and_blend(self):
        self.editor.add_stack_paint([self.item.id], "fill", BLUE)
        self.editor.update_stack_paint([self.item.id], "fill", 1, blend="multiply", opacity=0.4, visible=False)
        top = paint_stack(self.current(), "fill")[1]
        self.assertEqual((top["blend"], top["opacity"], top["visible"]), ("multiply", 0.4, False))
        with self.assertRaises(EditError):
            self.editor.update_stack_paint([self.item.id], "fill", 1, blend="glow")

    def test_set_stack_paint_by_index_and_remove(self):
        self.editor.add_stack_paint([self.item.id], "stroke", BLUE)
        self.editor.set_stack_paint([self.item.id], "stroke", 0, RED)
        self.assertEqual(self.current().stroke, "#ff0000")
        self.assertEqual(self.current().stroke_width, 1.0)
        self.editor.set_stack_paint([self.item.id], "stroke", 0, None)
        self.assertEqual(paint_stack(self.current(), "stroke"), [])
        self.assertIsNone(self.current().stroke)

    def test_move_is_one_undo_step(self):
        self.editor.add_stack_paint([self.item.id], "fill", BLUE)
        self.editor.move_stack_paint([self.item.id], "fill", 1, 0)
        self.assertEqual(self.current().fill, "#0000ff")
        self.editor.undo()
        self.assertEqual(self.current().fill, "#ff0000")

    def test_group_paint_edits_the_layers_inside(self):
        group = LayoutObject(new_id("group"), "group", "Group", None, 0, 0, 100, 100, fill=None)
        self.item.parent_id = group.id
        self.document.objects.insert(0, group)
        self.editor.add_stack_paint([group.id], "fill", BLUE)
        self.assertEqual(len(paint_stack(self.current(), "fill")), 2)
        self.assertEqual(paint_stack(self.editor.document.object(group.id), "fill"), [])


if __name__ == "__main__":
    unittest.main()
