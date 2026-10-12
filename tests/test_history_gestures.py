# SPDX-License-Identifier: MPL-2.0
"""Undo coalescing: one gesture is one undo step."""

import unittest

from luma_canvas.editing import Editor
from luma_canvas.model import Document
from luma_canvas.paint import Color, solid


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class GestureTests(unittest.TestCase):
    def setUp(self):
        self.editor = Editor(Document.empty("History"))
        self.clock = FakeClock()
        self.editor.clock = self.clock
        self.rect = self.editor.create("rectangle", 0, 0, 100, 100)

    def test_a_drag_is_one_undo_step(self):
        self.editor.begin_gesture("Move Rectangle")
        for step in range(30):
            self.editor.document.object(self.rect.id).x = step
        self.editor.end_gesture()
        self.assertEqual(len(self.editor.undo_stack), 2)
        self.assertEqual(self.editor.undo_label, "Move Rectangle")
        self.editor.undo()
        self.assertEqual(self.editor.document.object(self.rect.id).x, 0)

    def test_a_gesture_that_changes_nothing_records_nothing(self):
        self.editor.begin_gesture("Nothing")
        self.editor.end_gesture()
        self.assertEqual(len(self.editor.undo_stack), 1)

    def test_AT_S6_alt_drag_duplicate_is_one_step_with_the_move(self):
        self.editor.begin_gesture("Duplicate")
        copy = self.editor.duplicate([self.rect.id], offset=0, rename=False)[0]
        self.editor.document.object(copy.id).x = 250
        self.editor.end_gesture()
        self.assertEqual(len(self.editor.document.objects), 2)
        self.editor.undo()
        self.assertEqual(len(self.editor.document.objects), 1)
        self.assertEqual(self.editor.document.objects[0].x, 0)

    def test_AT_H2_nudges_coalesce_within_the_window(self):
        for _ in range(40):
            self.clock.now += 0.05
            self.editor.move([self.rect.id], 1, 0, key="nudge")
        self.assertEqual(len(self.editor.undo_stack), 2)
        self.assertEqual(self.editor.document.object(self.rect.id).x, 40)
        self.editor.undo()
        self.assertEqual(self.editor.document.object(self.rect.id).x, 0)

    def test_nudges_after_a_pause_are_separate(self):
        self.editor.move([self.rect.id], 1, 0, key="nudge")
        self.clock.now += 2
        self.editor.move([self.rect.id], 1, 0, key="nudge")
        self.assertEqual(len(self.editor.undo_stack), 3)

    def test_AT_P2_picker_drag_is_one_step(self):
        self.editor.begin_gesture("Change fill")
        for step in range(50):
            self.editor.set_paint([self.rect.id], "fill", solid(Color(step / 50, 0.2, 0.3)))
        self.editor.end_gesture()
        self.assertEqual(len(self.editor.undo_stack), 2)
        self.editor.undo()
        self.assertEqual(self.editor.document.object(self.rect.id).fill, "#d9d9d9")

    def test_undo_during_a_gesture_closes_it_first(self):
        self.editor.begin_gesture("Move")
        self.editor.document.object(self.rect.id).x = 99
        self.editor.undo()
        self.assertFalse(self.editor.gesture_open)
        self.assertEqual(self.editor.document.object(self.rect.id).x, 0)

    def test_cancel_restores(self):
        self.editor.begin_gesture("Move")
        self.editor.document.object(self.rect.id).x = 99
        self.editor.cancel_gesture()
        self.assertEqual(self.editor.document.object(self.rect.id).x, 0)
        self.assertEqual(len(self.editor.undo_stack), 1)


if __name__ == "__main__":
    unittest.main()
