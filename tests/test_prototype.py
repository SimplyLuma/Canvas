# SPDX-License-Identifier: Apache-2.0
"""Prototype links and the mode that runs them."""

import unittest

from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.prototype import Presentation, connect, connections_from, disconnect


def _document():
    first = LayoutObject(new_id("frame"), "frame", "Home", x=0, y=0, width=400, height=800)
    second = LayoutObject(new_id("frame"), "frame", "Detail", x=500, y=0, width=400, height=800)
    button = LayoutObject(new_id("rectangle"), "rectangle", "Button", parent_id=first.id,
                          x=20, y=20, width=100, height=40)
    document = Document.empty("Proto")
    document.objects.extend([first, second, button])
    document.validate()
    return document, first, second, button


class ConnectionTests(unittest.TestCase):
    def test_an_object_links_to_a_frame(self):
        document, first, second, button = _document()
        connection = connect(document, button, second, trigger="click")
        self.assertEqual(connection.destination_id, second.id)
        self.assertEqual(len(connections_from(document, button.id)), 1)

    def test_linking_again_replaces_rather_than_duplicates(self):
        document, first, second, button = _document()
        connect(document, button, second)
        connect(document, button, first)
        self.assertEqual(len(connections_from(document, button.id)), 1)

    def test_a_link_must_arrive_at_a_frame(self):
        document, first, second, button = _document()
        with self.assertRaises(ValueError):
            connect(document, button, button)

    def test_an_unknown_trigger_is_refused(self):
        document, first, second, button = _document()
        with self.assertRaises(ValueError):
            connect(document, button, second, trigger="telepathy")

    def test_disconnecting_removes_the_link(self):
        document, first, second, button = _document()
        connect(document, button, second)
        disconnect(document, button.id)
        self.assertEqual(connections_from(document, button.id), [])


class PresentationTests(unittest.TestCase):
    def test_a_run_starts_at_a_frame_nothing_links_into(self):
        document, first, second, button = _document()
        connect(document, button, second)
        run = Presentation.begin(document)
        self.assertEqual(run.frame.id, first.id)

    def test_pressing_a_hotspot_moves_on_and_back_returns(self):
        document, first, second, button = _document()
        connect(document, button, second)
        run = Presentation.begin(document)
        self.assertEqual(len(run.hotspots()), 1)
        run.activate(button.id)
        self.assertEqual(run.frame.id, second.id)
        self.assertTrue(run.back())
        self.assertEqual(run.frame.id, first.id)

    def test_back_from_the_start_does_nothing(self):
        document, first, second, button = _document()
        run = Presentation.begin(document)
        self.assertFalse(run.back())


if __name__ == "__main__":
    unittest.main()
