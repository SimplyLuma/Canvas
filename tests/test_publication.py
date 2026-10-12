# SPDX-License-Identifier: Apache-2.0
"""Pages, masters, and text that flows between frames."""

import unittest

from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.publication import (
    TextMetrics, apply_master, flow_text, link, make_master,
    override_master_item, thread, unlink,
)


def _document(*objects: LayoutObject) -> Document:
    document = Document.empty("Publication")
    document.objects.extend(objects)
    document.validate()
    return document


def _text(name: str, width=200.0, height=100.0, text="") -> LayoutObject:
    return LayoutObject(new_id("text"), "text", name, x=0, y=0,
                        width=width, height=height, text=text)


class ThreadTests(unittest.TestCase):
    def test_frames_thread_in_order_from_anywhere_in_the_chain(self):
        first, second, third = _text("A"), _text("B"), _text("C")
        document = _document(first, second, third)
        link(document, first, second)
        link(document, second, third)
        names = [item.name for item in thread(document, second)]
        self.assertEqual(names, ["A", "B", "C"])

    def test_a_thread_cannot_loop(self):
        first, second = _text("A"), _text("B")
        document = _document(first, second)
        link(document, first, second)
        with self.assertRaises(ValueError):
            link(document, second, first)

    def test_unlinking_ends_the_thread(self):
        first, second = _text("A"), _text("B")
        document = _document(first, second)
        link(document, first, second)
        unlink(first)
        self.assertEqual([item.name for item in thread(document, first)], ["A"])


class FlowTests(unittest.TestCase):
    def test_text_continues_into_the_next_frame(self):
        body = " ".join(["word"] * 400)
        first, second = _text("A", 200, 60, body), _text("B", 200, 60)
        document = _document(first, second)
        link(document, first, second)
        flow = flow_text(document, first, TextMetrics())
        self.assertTrue(flow.pieces[first.id])
        self.assertTrue(flow.pieces[second.id])
        # The property that matters: pouring the text through the thread does
        # not lose or duplicate any of it.
        poured = " ".join(
            part for part in (flow.pieces[first.id], flow.pieces[second.id], flow.overflow)
            if part
        )
        self.assertEqual(poured.split(), body.split())

    def test_text_with_nowhere_to_go_is_reported_as_overflowing(self):
        body = " ".join(["word"] * 800)
        only = _text("Only", 120, 40, body)
        document = _document(only)
        flow = flow_text(document, only, TextMetrics())
        self.assertTrue(flow.overflows)

    def test_text_that_fits_does_not_overflow(self):
        only = _text("Only", 400, 400, "a short line")
        document = _document(only)
        flow = flow_text(document, only, TextMetrics())
        self.assertFalse(flow.overflows)
        self.assertEqual(flow.pieces[only.id], "a short line")

    def test_words_are_not_cut_in_half(self):
        body = " ".join(["alpha"] * 100)
        first, second = _text("A", 200, 40, body), _text("B", 200, 200)
        document = _document(first, second)
        link(document, first, second)
        flow = flow_text(document, first, TextMetrics())
        self.assertFalse(flow.pieces[first.id].endswith("alph"))


class MasterTests(unittest.TestCase):
    def test_a_master_puts_its_contents_on_a_page(self):
        master = LayoutObject(new_id("page"), "page", "A-Master", x=0, y=0, width=600, height=800)
        folio = LayoutObject(new_id("text"), "text", "Folio", parent_id=master.id,
                             x=20, y=760, width=100, height=20, text="1")
        page = LayoutObject(new_id("page"), "page", "Page 1", x=700, y=0, width=600, height=800)
        document = _document(master, folio, page)
        make_master(master)
        apply_master(document, page, master)
        placed = [child for child in document.children(page.id)]
        self.assertEqual([child.name for child in placed], ["Folio"])
        self.assertAlmostEqual(placed[0].x, 720.0)
        self.assertTrue(placed[0].locked)

    def test_applying_a_master_twice_does_not_duplicate(self):
        master = LayoutObject(new_id("page"), "page", "A-Master", x=0, y=0, width=600, height=800)
        folio = LayoutObject(new_id("text"), "text", "Folio", parent_id=master.id,
                             x=20, y=760, width=100, height=20, text="1")
        page = LayoutObject(new_id("page"), "page", "Page 1", x=700, y=0, width=600, height=800)
        document = _document(master, folio, page)
        apply_master(document, page, master)
        apply_master(document, page, master)
        self.assertEqual(len(document.children(page.id)), 1)

    def test_a_page_can_override_a_master_item(self):
        master = LayoutObject(new_id("page"), "page", "A-Master", x=0, y=0, width=600, height=800)
        folio = LayoutObject(new_id("text"), "text", "Folio", parent_id=master.id,
                             x=20, y=760, width=100, height=20, text="1")
        page = LayoutObject(new_id("page"), "page", "Page 1", x=700, y=0, width=600, height=800)
        document = _document(master, folio, page)
        apply_master(document, page, master)
        placed = document.children(page.id)[0]
        override_master_item(document, placed)
        placed.text = "iv"
        apply_master(document, page, master)
        self.assertEqual(placed.text, "iv")


if __name__ == "__main__":
    unittest.main()
