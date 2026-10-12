# SPDX-License-Identifier: MPL-2.0
"""Live boolean layers: operands stay editable, the result follows, Flatten bakes it."""

import math
import tempfile
import unittest
from pathlib import Path

from luma_canvas import geometry
from luma_canvas.booleans import update_booleans
from luma_canvas.editing import EditError, Editor
from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.persistence import DocumentStore


def obj(kind, name, x, y, w, h, parent=None, **fields):
    return LayoutObject(new_id(kind), kind, name, parent, x, y, w, h, **fields)


def area(item):
    total = 0.0
    for shape in geometry.outline(item):
        points = geometry.flatten_subpath(shape, 64)
        total += sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1])) / 2
    return abs(total)


class BooleanLayerTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Booleans")
        self.editor = Editor(self.document)
        self.frame = obj("frame", "Artboard", 0, 0, 800, 600)
        self.square = obj("rectangle", "Square", 100, 100, 200, 200, self.frame.id, fill="#2060e0")
        self.circle = obj("ellipse", "Circle", 200, 150, 160, 160, self.frame.id, fill="#e02080",
                          stroke="#000000", stroke_width=2)
        self.document.objects.extend([self.frame, self.square, self.circle])

    @property
    def doc(self):
        return self.editor.document

    def boolean(self, operation):
        return self.editor.boolean([self.square.id, self.circle.id], operation)

    def test_union_keeps_operands_and_takes_the_top_shapes_look(self):
        item = self.boolean("union")
        self.assertEqual(item.kind, "boolean")
        self.assertEqual(item.name, "Union")
        self.assertEqual([child.name for child in self.doc.children(item.id)], ["Square", "Circle"])
        self.assertEqual((item.fill, item.stroke, item.stroke_width), ("#e02080", "#000000", 2))
        self.assertEqual((item.x, item.y), (100, 100))
        self.assertAlmostEqual(item.width, 260, delta=0.01)
        self.assertAlmostEqual(item.height, 210, delta=0.01)
        self.assertTrue(any(segment[0] == "C" for shape in geometry.outline(item) for segment in shape.segments))
        self.assertEqual(self.doc.selected_ids, [item.id])
        self.assertEqual(item.parent_id, self.frame.id)

    def test_subtract_takes_the_bottom_shapes_look(self):
        item = self.boolean("subtract")
        self.assertEqual(item.fill, "#2060e0")
        circle_inside = math.pi * 80 * 80 - 0  # the circle overlaps the square partly
        self.assertLess(area(item), 40000)
        self.assertGreater(area(item), 40000 - circle_inside)

    def test_editing_an_operand_updates_the_result(self):
        item = self.boolean("intersect")
        before = area(item)
        with self.editor.transaction("Move circle"):
            self.doc.object(self.circle.id).x += 500
        self.assertEqual(self.doc.object(item.id).path, [])
        self.assertEqual(geometry.outline(self.doc.object(item.id)), [])
        self.editor.undo()
        self.assertAlmostEqual(area(self.doc.object(item.id)), before, delta=1)

    def test_moving_the_boolean_moves_its_operands(self):
        item = self.boolean("union")
        self.editor.move([item.id], 40, 10)
        self.assertEqual((self.doc.object(self.square.id).x, self.doc.object(self.square.id).y), (140, 110))
        self.assertEqual((self.doc.object(item.id).x, self.doc.object(item.id).y), (140, 110))

    def test_changing_the_operation_in_place(self):
        item = self.boolean("union")
        union_area = area(item)
        self.editor.boolean([item.id], "exclude")
        item = self.doc.object(item.id)
        self.assertEqual(item.metadata["boolean_operation"], "exclude")
        self.assertEqual(item.name, "Exclude")
        self.assertLess(area(item), union_area)

    def test_nested_booleans_and_groups_as_operands(self):
        third = obj("rectangle", "Bar", 50, 180, 400, 20, self.frame.id)
        self.doc.objects.append(third)
        inner = self.boolean("union")
        outer = self.editor.boolean([inner.id, third.id], "subtract")
        self.assertEqual(self.doc.object(inner.id).parent_id, outer.id)
        # The bar cuts the union into a top and a bottom part.
        self.assertEqual(len(geometry.outline(self.doc.object(outer.id))), 2)

    def test_hidden_operands_do_not_count(self):
        item = self.boolean("union")
        with self.editor.transaction("Hide"):
            self.doc.object(self.circle.id).visible = False
        self.assertAlmostEqual(area(self.doc.object(item.id)), 40000, delta=1)

    def test_flatten_a_boolean_into_its_result(self):
        item = self.boolean("union")
        result_area = area(item)
        flat = self.editor.flatten([item.id])
        self.assertEqual(flat.kind, "path")
        self.assertIsNone(self.doc.object(self.square.id))
        self.assertAlmostEqual(area(flat), result_area, delta=1)
        self.assertEqual(self.editor.undo_label, "Flatten")
        self.editor.undo()
        self.assertEqual(self.doc.object(item.id).kind, "boolean")

    def test_flatten_shapes_and_several_layers(self):
        flat = self.editor.flatten([self.circle.id])
        self.assertEqual((flat.kind, flat.name), ("path", "Circle"))   # a name given by a person stays
        self.assertAlmostEqual(area(flat), math.pi * 80 * 80, delta=20)
        self.editor.undo()
        combined = self.editor.flatten([self.square.id, self.circle.id])
        self.assertEqual(combined.kind, "path")
        self.assertEqual(len(geometry.outline(combined)), 2)
        with self.assertRaises(EditError):
            self.editor.flatten([self.frame.id])

    def test_ungroup_releases_the_operands(self):
        item = self.boolean("union")
        released = self.editor.ungroup([item.id])
        self.assertIsNone(self.doc.object(item.id))
        self.assertEqual(sorted(released), sorted([self.square.id, self.circle.id]))

    def test_hit_testing_follows_the_result(self):
        item = self.boolean("subtract")
        tree = geometry.Tree(self.doc.objects)
        self.assertEqual(geometry.hit_chain(tree, 120, 120)[1].id, item.id)
        # Inside the subtracted circle there is no boolean to click, only the frame.
        chain = geometry.hit_chain(tree, 280, 230)
        self.assertNotIn(item.id, [value.id for value in chain])

    def test_save_and_reopen_without_recomputing(self):
        item = self.boolean("exclude")
        path = [dict(node) for node in item.path]
        with tempfile.TemporaryDirectory() as folder:
            loaded = DocumentStore.load(DocumentStore.save(self.doc, Path(folder) / "Boolean.canvas"))
        self.assertEqual(loaded.object(item.id).path, path)
        self.assertFalse(update_booleans(loaded.objects))

    def test_needs_two_shapes(self):
        with self.assertRaises(EditError):
            self.editor.boolean([self.square.id], "union")
        with self.assertRaises(EditError):
            self.editor.boolean([self.square.id, self.circle.id], "merge")


if __name__ == "__main__":
    unittest.main()
