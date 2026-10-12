# SPDX-License-Identifier: MPL-2.0
"""An invisible resize zone that exactly covers the 9px gutter between islands.

`Gtk.Paned` draws a handle with its own width and borders, which closed the
kit's 9px gutter to 5px and put vertical lines on both island edges. This
widget draws nothing at rest or on hover: it only takes the gutter's space,
shows the column-resize pointer, and moves the neighboring panel's edge with
the pointer. Double-click puts the panel back to its default width.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

GUTTER = 9  # the kit's island gap; never wider, never narrower


class ResizeGutter(Gtk.Widget):
    __gtype_name__ = "CanvasResizeGutter"

    def __init__(self, *, get_width: Callable[[], int], set_width: Callable[[int], None],
                 minimum: int, maximum: int, default: int, grows_with_pointer: bool,
                 on_finished: Callable[[int], None] | None = None, label: str = "Resize panel") -> None:
        super().__init__()
        self.add_css_class("canvas-gutter")
        self.set_size_request(GUTTER, -1)
        self.set_hexpand(False)
        self.set_vexpand(True)
        self.set_cursor_from_name("col-resize")
        self.set_focusable(False)
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self._get_width = get_width
        self._set_width = set_width
        self.minimum, self.maximum, self.default = minimum, maximum, default
        # The left gutter widens its panel when dragged right; the right
        # gutter widens the inspector when dragged left.
        self._sign = 1 if grows_with_pointer else -1
        self._on_finished = on_finished
        self._origin = 0

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", self._pressed)
        self.add_controller(click)

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return GUTTER, GUTTER, -1, -1
        return 0, 0, -1, -1

    def clamp(self, width: float) -> int:
        return int(max(self.minimum, min(self.maximum, round(width))))

    def _begin(self, *_args) -> None:
        self._origin = self._get_width()

    def _update(self, _gesture, offset_x: float, _offset_y: float) -> None:
        if self.get_direction() == Gtk.TextDirection.RTL:
            offset_x = -offset_x
        self._set_width(self.clamp(self._origin + self._sign * offset_x))

    def _end(self, *_args) -> None:
        if self._on_finished:
            self._on_finished(self._get_width())

    def _pressed(self, _gesture, count: int, _x: float, _y: float) -> None:
        if count == 2:
            self._set_width(self.default)
            if self._on_finished:
                self._on_finished(self.default)
