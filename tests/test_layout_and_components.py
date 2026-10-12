# SPDX-License-Identifier: Apache-2.0
"""Constraints, auto-layout, and components keeping their instances in step."""

import unittest

from luma_canvas.components import (
    detach, instances_of, make_component, record_override, sync_component, usage,
)
from luma_canvas.layout import AutoLayout, apply_auto_layout, apply_constraints
from luma_canvas.model import Document, LayoutObject, new_id


def _document(*objects: LayoutObject) -> Document:
    document = Document.empty("Test")
    document.objects.extend(objects)
    document.validate()
    return document


class ConstraintTests(unittest.TestCase):
    def _frame_with_child(self, constraints: dict) -> tuple[Document, LayoutObject, LayoutObject]:
        frame = LayoutObject(new_id("frame"), "frame", "Frame", x=0, y=0, width=400, height=300)
        child = LayoutObject(new_id("rectangle"), "rectangle", "Child", parent_id=frame.id,
                             x=300, y=200, width=80, height=60, constraints=constraints)
        return _document(frame, child), frame, child

    def test_a_right_pinned_child_keeps_its_right_margin(self):
        document, frame, child = self._frame_with_child({"horizontal": "right"})
        frame.width = 500
        apply_constraints(document, frame, 400, 300)
        self.assertAlmostEqual(child.x + child.width, 480.0)

    def test_left_and_right_stretches_the_child(self):
        document, frame, child = self._frame_with_child({"horizontal": "left-right"})
        frame.width = 500
        apply_constraints(document, frame, 400, 300)
        self.assertAlmostEqual(child.width, 180.0)

    def test_centre_keeps_the_child_centred(self):
        document, frame, child = self._frame_with_child({"horizontal": "centre"})
        frame.width = 800
        apply_constraints(document, frame, 400, 300)
        self.assertAlmostEqual(child.x + child.width / 2, 540.0)

    def test_scale_grows_the_child_with_the_frame(self):
        document, frame, child = self._frame_with_child({"horizontal": "scale"})
        frame.width = 800
        apply_constraints(document, frame, 400, 300)
        self.assertAlmostEqual(child.width, 160.0)


class AutoLayoutTests(unittest.TestCase):
    def test_children_stack_with_the_gap_and_padding(self):
        frame = LayoutObject(new_id("frame"), "frame", "Stack", x=0, y=0, width=200, height=400,
                             layout=AutoLayout(direction="vertical", gap=10,
                                               padding_top=20, padding_left=20,
                                               padding_right=20, padding_bottom=20).write())
        first = LayoutObject(new_id("rectangle"), "rectangle", "A", parent_id=frame.id,
                             x=0, y=0, width=50, height=40)
        second = LayoutObject(new_id("rectangle"), "rectangle", "B", parent_id=frame.id,
                              x=0, y=0, width=50, height=60)
        document = _document(frame, first, second)
        apply_auto_layout(document, frame)
        self.assertAlmostEqual(first.y, 20.0)
        self.assertAlmostEqual(second.y, 70.0)   # 20 + 40 + 10
        self.assertAlmostEqual(first.x, 20.0)

    def test_a_hugging_frame_resizes_to_its_content(self):
        frame = LayoutObject(new_id("frame"), "frame", "Hug", x=0, y=0, width=999, height=999,
                             layout=AutoLayout(direction="vertical", gap=10, sizing="hug",
                                               padding_top=10, padding_bottom=10,
                                               padding_left=10, padding_right=10).write())
        child = LayoutObject(new_id("rectangle"), "rectangle", "A", parent_id=frame.id,
                             x=0, y=0, width=50, height=40)
        document = _document(frame, child)
        apply_auto_layout(document, frame)
        self.assertAlmostEqual(frame.height, 60.0)
        self.assertAlmostEqual(frame.width, 70.0)

    def test_a_growing_child_takes_the_spare_room(self):
        frame = LayoutObject(new_id("frame"), "frame", "Row", x=0, y=0, width=300, height=100,
                             layout=AutoLayout(direction="horizontal", gap=10,
                                               padding_top=0, padding_bottom=0,
                                               padding_left=0, padding_right=0).write())
        fixed = LayoutObject(new_id("rectangle"), "rectangle", "Fixed", parent_id=frame.id,
                             x=0, y=0, width=100, height=40)
        grows = LayoutObject(new_id("rectangle"), "rectangle", "Grows", parent_id=frame.id,
                             x=0, y=0, width=10, height=40, grow=1.0)
        document = _document(frame, fixed, grows)
        apply_auto_layout(document, frame)
        self.assertAlmostEqual(grows.width, 190.0)   # 300 - 100 - 10


class ComponentTests(unittest.TestCase):
    def _component(self):
        source = LayoutObject(new_id("component"), "component", "Button", x=0, y=0,
                              width=120, height=40, fill="#111111")
        label = LayoutObject(new_id("text"), "text", "Label", parent_id=source.id,
                             x=10, y=10, width=100, height=20, text="Press")
        document = _document(source, label)
        instance = make_component(document, source)
        document.validate()
        return document, source, instance

    def test_an_instance_is_created_and_counted(self):
        document, source, instance = self._component()
        self.assertEqual(instance.component_source_id, source.id)
        self.assertEqual(usage(document)[0].count, 1)

    def test_editing_the_component_updates_the_instance(self):
        document, source, instance = self._component()
        source.fill = "#ff0000"
        sync_component(document, source.id)
        self.assertEqual(instance.fill, "#ff0000")

    def test_an_override_survives_the_component_changing(self):
        document, source, instance = self._component()
        record_override(instance, "fill", "#00ff00")
        source.fill = "#ff0000"
        sync_component(document, source.id)
        self.assertEqual(instance.fill, "#00ff00")

    def test_children_come_across_and_follow(self):
        document, source, instance = self._component()
        children = [item for item in document.descendants(instance.id)]
        self.assertEqual(len(children), 1)
        label = document.object(source.id) and [
            item for item in document.descendants(source.id)][0]
        label.text = "Send"
        sync_component(document, source.id)
        self.assertEqual(children[0].text, "Send")

    def test_duplicate_names_rename_and_metadata_keep_stable_children(self):
        document, source, instance = self._component()
        label = document.descendants(source.id)[0]
        label.metadata = {'font_size': 32}
        sibling = label.clone()
        sibling.id = new_id('text')
        sibling.text = 'Second'
        document.objects.append(sibling)
        sync_component(document, source.id)
        children = document.descendants(instance.id)
        self.assertEqual(len(children), 2)
        label.name = 'Renamed'
        children[0].metadata['font_size'] = 18
        self.assertEqual(label.metadata['font_size'], 32)
        sync_component(document, source.id)
        self.assertEqual({child.id for child in document.descendants(instance.id)}, {child.id for child in children})
        self.assertEqual(children[0].metadata['font_size'], 32)
        document.validate()

    def test_leaf_component_preserves_text_and_reset_clears_real_overrides(self):
        from luma_canvas.editing import Editor
        leaf = LayoutObject(new_id('text'), 'text', 'Title', text='Hello', width=120, height=40)
        document = _document(leaf)
        instance = make_component(document, leaf)
        self.assertEqual(leaf.kind, 'text')
        self.assertEqual(document.descendants(instance.id)[0].text, 'Hello')
        editor = Editor(document)
        clone = document.descendants(instance.id)[0]
        editor.set_property([clone.id], 'text', 'Personal')
        editor.set_property([leaf.id], 'text', 'Updated')
        self.assertEqual(clone.text, 'Personal')
        from luma_canvas.paint import solid, parse_hex
        editor.set_paint([clone.id], 'fill', solid(parse_hex('#ff0000')))
        editor.set_paint([leaf.id], 'fill', solid(parse_hex('#00ff00')))
        self.assertEqual(clone.fill, '#ff0000')
        editor.reset_instance_overrides(instance.id)
        self.assertEqual(clone.fill, '#00ff00')
        self.assertEqual(clone.text, 'Updated')
        self.assertEqual(clone.overrides, {})
        document.validate()

    def test_detaching_stops_the_link(self):
        document, source, instance = self._component()
        detach(document, instance)
        source.fill = "#0000ff"
        sync_component(document, source.id)
        self.assertNotEqual(instance.fill, "#0000ff")
        self.assertEqual(instances_of(document, source.id), [])


if __name__ == "__main__":
    unittest.main()
