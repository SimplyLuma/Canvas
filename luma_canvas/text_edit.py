# SPDX-License-Identifier: MPL-2.0
"""Editing a text layer in place on the canvas.

A session holds the text, the caret and the selection anchor (character
indices into the stored text) and edits them the way a text field does:
typing replaces the selection, Backspace and Delete remove a character or a
word with Ctrl, arrows move by character or word, Up and Down by line, Home
and End to the line, Shift extends. Positions on screen come from the same
Pango layout the renderer draws (`typography.build_layout`), so the caret
lands where the glyphs are.

Undo inside a session is grouped by word; the whole session is one undo step
in the document (the canvas opens an editor gesture when editing starts and
closes it on commit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the session logic itself needs no Pango
    from .typography import TextLayout

WORD_BREAK = set(" \t\n.,;:!?()[]{}\"'/\\-")


@dataclass
class TextSession:
    item_id: str
    text: str
    cursor: int = 0
    anchor: int = 0
    original: str = ""
    created: bool = False
    preedit: str = ""
    history: list[tuple[str, int, int]] = field(default_factory=list)
    future: list[tuple[str, int, int]] = field(default_factory=list)
    _last_kind: str = ""

    @classmethod
    def begin(cls, item: Any, *, created: bool = False, select_all: bool = False) -> "TextSession":
        text = item.text or ""
        session = cls(item.id, text, len(text), 0 if select_all else len(text), text, created)
        return session

    # ── selection ────────────────────────────────────────────────────────

    @property
    def selection(self) -> tuple[int, int]:
        return min(self.cursor, self.anchor), max(self.cursor, self.anchor)

    @property
    def has_selection(self) -> bool:
        return self.cursor != self.anchor

    def selected_text(self) -> str:
        start, end = self.selection
        return self.text[start:end]

    def set_cursor(self, index: int, *, extend: bool = False) -> None:
        self.cursor = max(0, min(len(self.text), index))
        if not extend:
            self.anchor = self.cursor
        self._last_kind = ""

    def select_all(self) -> None:
        self.anchor, self.cursor = 0, len(self.text)

    def select_word(self, index: int) -> None:
        start, end = word_bounds(self.text, index)
        self.anchor, self.cursor = start, end

    def select_paragraph(self, index: int) -> None:
        start = self.text.rfind("\n", 0, max(0, index)) + 1
        end = self.text.find("\n", index)
        self.anchor, self.cursor = start, len(self.text) if end < 0 else end

    # ── editing ──────────────────────────────────────────────────────────

    def _remember(self, kind: str) -> None:
        # A new undo group starts at word boundaries and when the kind changes.
        if kind != self._last_kind or kind == "boundary":
            self.history.append((self.text, self.cursor, self.anchor))
            del self.history[:-200]
        self.future.clear()
        self._last_kind = kind

    def insert(self, value: str) -> None:
        if not value:
            return
        kind = "boundary" if any(character in WORD_BREAK for character in value) else "type"
        self._remember(kind)
        start, end = self.selection
        self.text = self.text[:start] + value + self.text[end:]
        self.cursor = self.anchor = start + len(value)
        if kind == "boundary":
            self._last_kind = "type-after-boundary"

    def delete(self, direction: int, *, word: bool = False) -> None:
        if self.has_selection:
            self._remember("delete")
            start, end = self.selection
        elif direction < 0:
            if self.cursor == 0:
                return
            self._remember("delete")
            start = previous_word(self.text, self.cursor) if word else self.cursor - 1
            end = self.cursor
        else:
            if self.cursor >= len(self.text):
                return
            self._remember("delete")
            start = self.cursor
            end = next_word(self.text, self.cursor) if word else self.cursor + 1
        self.text = self.text[:start] + self.text[end:]
        self.cursor = self.anchor = start

    def undo(self) -> bool:
        if not self.history:
            return False
        self.future.append((self.text, self.cursor, self.anchor))
        self.text, self.cursor, self.anchor = self.history.pop()
        self._last_kind = ""
        return True

    def redo(self) -> bool:
        if not self.future:
            return False
        self.history.append((self.text, self.cursor, self.anchor))
        self.text, self.cursor, self.anchor = self.future.pop()
        self._last_kind = ""
        return True

    # ── movement ─────────────────────────────────────────────────────────

    def move(self, how: str, *, extend: bool = False, layout: TextLayout | None = None) -> None:
        start, end = self.selection
        if how == "left":
            index = start if self.has_selection and not extend else self.cursor - 1
        elif how == "right":
            index = end if self.has_selection and not extend else self.cursor + 1
        elif how == "word_left":
            index = previous_word(self.text, self.cursor)
        elif how == "word_right":
            index = next_word(self.text, self.cursor)
        elif how == "doc_start":
            index = 0
        elif how == "doc_end":
            index = len(self.text)
        elif how in {"home", "end", "up", "down"} and layout is not None:
            index = line_move(layout, self.cursor, how)
        elif how == "home":
            index = self.text.rfind("\n", 0, self.cursor) + 1
        elif how == "end":
            found = self.text.find("\n", self.cursor)
            index = len(self.text) if found < 0 else found
        else:
            return
        self.set_cursor(index, extend=extend)


def word_bounds(text: str, index: int) -> tuple[int, int]:
    index = max(0, min(len(text), index))
    if not text:
        return 0, 0
    probe = min(index, len(text) - 1)
    if text[probe] in WORD_BREAK and probe > 0 and text[probe - 1] not in WORD_BREAK and index == len(text):
        probe -= 1
    if text[probe] in WORD_BREAK:
        return probe, probe + 1
    start = probe
    while start > 0 and text[start - 1] not in WORD_BREAK:
        start -= 1
    end = probe
    while end < len(text) and text[end] not in WORD_BREAK:
        end += 1
    return start, end


def previous_word(text: str, index: int) -> int:
    index = max(0, min(len(text), index))
    while index > 0 and text[index - 1] in WORD_BREAK:
        index -= 1
    while index > 0 and text[index - 1] not in WORD_BREAK:
        index -= 1
    return index


def next_word(text: str, index: int) -> int:
    index = max(0, min(len(text), index))
    while index < len(text) and text[index] in WORD_BREAK:
        index += 1
    while index < len(text) and text[index] not in WORD_BREAK:
        index += 1
    return index


def index_at(layout: TextLayout, x: float, y: float) -> int:
    """The caret index nearest a point in the text box's local coordinates."""
    import gi
    gi.require_version("Pango", "1.0")
    from gi.repository import Pango
    _inside, byte, trailing = layout.layout.xy_to_index(round(x * Pango.SCALE),
                                                         round((y - layout.offset_y) * Pango.SCALE))
    index = layout.from_byte(byte)
    if trailing:
        index += trailing
    return index


def caret_rect(layout: TextLayout, index: int) -> tuple[float, float, float, float]:
    import gi
    gi.require_version("Pango", "1.0")
    from gi.repository import Pango
    strong, _weak = layout.layout.get_cursor_pos(layout.byte_index(index))
    return (strong.x / Pango.SCALE, strong.y / Pango.SCALE + layout.offset_y,
            max(1.0, strong.width / Pango.SCALE), strong.height / Pango.SCALE)


def selection_rects(layout: TextLayout, start: int, end: int) -> list[tuple[float, float, float, float]]:
    """Rectangles covering a selection, line by line, in local coordinates.

    Positions come from `index_to_pos` (PyGObject's `get_x_ranges` binding
    returns a truncated array).
    """
    import gi
    gi.require_version("Pango", "1.0")
    from gi.repository import Pango
    if start >= end:
        return []
    encoded = layout.display.encode("utf-8")
    first, last = layout.byte_index(start), layout.byte_index(end)

    def previous_boundary(byte: int) -> int:
        byte -= 1
        while byte > 0 and (encoded[byte] & 0xC0) == 0x80:
            byte -= 1
        return max(0, byte)

    rects = []
    iterator = layout.layout.get_iter()
    while True:
        line = iterator.get_line_readonly()
        _ink, logical = iterator.get_line_extents()
        line_start, line_end = line.start_index, line.start_index + line.length
        low, high = max(first, line_start), min(last, line_end)
        top = logical.y / Pango.SCALE + layout.offset_y
        height = logical.height / Pango.SCALE
        if low < high:
            left = layout.layout.index_to_pos(low).x / Pango.SCALE
            tail = layout.layout.index_to_pos(previous_boundary(high))
            right = (tail.x + tail.width) / Pango.SCALE
            if encoded[previous_boundary(high):high] == b"\n":
                right = tail.x / Pango.SCALE + 4.0
            rects.append((min(left, right), top, max(2.0, abs(right - left)), height))
        elif last > line_end and first <= line_end and line.length == 0:
            rects.append((logical.x / Pango.SCALE, top, 4.0, height))
        if not iterator.next_line():
            break
    return rects


def line_move(layout: TextLayout, index: int, how: str) -> int:
    import gi
    gi.require_version("Pango", "1.0")
    from gi.repository import Pango
    byte = layout.byte_index(index)
    line_number, x = layout.layout.index_to_line_x(byte, False)
    line_count = layout.layout.get_line_count()
    if how in {"home", "end"}:
        line = layout.layout.get_line_readonly(line_number)
        target = line.start_index if how == "home" else line.start_index + line.length
        if how == "end":
            text_bytes = layout.display.encode("utf-8")
            if target > 0 and target <= len(text_bytes) and text_bytes[target - 1:target] == b"\n":
                target -= 1
        return layout.from_byte(target)
    target_line = line_number + (-1 if how == "up" else 1)
    if target_line < 0:
        return 0
    if target_line >= line_count:
        return layout.source_index(len(layout.display))
    line = layout.layout.get_line_readonly(target_line)
    _inside, byte, trailing = line.x_to_index(x)
    return layout.from_byte(byte) + trailing
