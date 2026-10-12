# SPDX-License-Identifier: MPL-2.0
"""Text editing sessions: typing, deleting, moving and word-grouped undo."""

import unittest

from luma_canvas.model import LayoutObject, new_id
from luma_canvas.text_edit import TextSession, next_word, previous_word, word_bounds


def session(text: str, cursor: int | None = None) -> TextSession:
    item = LayoutObject(new_id("text"), "text", "Text", text=text)
    value = TextSession.begin(item)
    if cursor is not None:
        value.set_cursor(cursor)
    return value


class SessionTests(unittest.TestCase):
    def test_typing_replaces_the_selection(self):
        value = session("Hello world")
        value.anchor, value.cursor = 6, 11
        value.insert("there")
        self.assertEqual(value.text, "Hello there")
        self.assertEqual((value.cursor, value.anchor), (11, 11))

    def test_backspace_and_delete(self):
        value = session("abc", 2)
        value.delete(-1)
        self.assertEqual((value.text, value.cursor), ("ac", 1))
        value.delete(1)
        self.assertEqual((value.text, value.cursor), ("a", 1))

    def test_ctrl_backspace_deletes_a_word(self):
        value = session("one two three")
        value.delete(-1, word=True)
        self.assertEqual(value.text, "one two ")

    def test_word_movement_and_bounds(self):
        text = "one two, three"
        self.assertEqual(next_word(text, 0), 3)
        self.assertEqual(previous_word(text, len(text)), 9)
        self.assertEqual(word_bounds(text, 5), (4, 7))

    def test_shift_extends(self):
        value = session("abcdef", 2)
        value.move("right", extend=True)
        value.move("right", extend=True)
        self.assertEqual(value.selected_text(), "cd")
        value.move("left")
        self.assertEqual((value.cursor, value.has_selection), (2, False))

    def test_undo_is_grouped_by_word(self):
        value = session("")
        for character in "Hello world":
            value.insert(character)
        value.undo()
        self.assertEqual(value.text, "Hello ")
        value.undo()
        self.assertEqual(value.text, "Hello")
        value.redo()
        self.assertEqual(value.text, "Hello ")

    def test_select_word_and_paragraph(self):
        value = session("first line\nsecond words here")
        value.select_word(14)
        self.assertEqual(value.selected_text(), "second")
        value.select_paragraph(14)
        self.assertEqual(value.selected_text(), "second words here")


if __name__ == "__main__":
    unittest.main()
