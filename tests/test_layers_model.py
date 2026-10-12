# SPDX-License-Identifier: MPL-2.0
"""Layer tree ordering, in-place diffing and drop placement (no display)."""
from __future__ import annotations

import unittest

from luma_canvas.editing import Editor
from luma_canvas.layers import drop_target, layer_rows, splice_plan
from luma_canvas.model import Document, LayoutObject


def fixture() -> tuple[Document, dict[str, str]]:
    document = Document.empty()
    frame = LayoutObject("frame-1", "frame", "Home", None, 0, 0, 400, 300)
    card = LayoutObject("rect-1", "rectangle", "Card", "frame-1", 10, 10, 100, 50)
    title = LayoutObject("text-1", "text", "Title", "frame-1", 10, 80, 100, 20)
    loose = LayoutObject("ellipse-1", "ellipse", "Dot", None, 500, 0, 20, 20)
    document.objects.extend([frame, card, title, loose])
    document.validate()
    return document, {"frame": frame.id, "card": card.id, "title": title.id, "dot": loose.id}


class LayerRowsTest(unittest.TestCase):
    def test_top_most_first_with_children_under_their_frame(self) -> None:
        document, ids = fixture()
        self.assertEqual(layer_rows(document), [
            (ids["dot"], 0), (ids["frame"], 0), (ids["title"], 1), (ids["card"], 1)])

    def test_collapsed_frames_hide_children(self) -> None:
        document, ids = fixture()
        self.assertEqual(layer_rows(document, expanded=[]), [(ids["dot"], 0), (ids["frame"], 0)])

    def test_ten_thousand_layers_are_ordered_quickly(self) -> None:
        document = Document.empty()
        for frame_index in range(100):
            frame = LayoutObject(f"frame-{frame_index}", "frame", f"Frame {frame_index}", None, 0, 0, 10, 10)
            document.objects.append(frame)
            for child in range(99):
                document.objects.append(LayoutObject(f"r-{frame_index}-{child}", "rectangle", "R", frame.id, 0, 0, 1, 1))
        import time
        start = time.perf_counter()
        rows = layer_rows(document)
        self.assertEqual(len(rows), 10_000)
        self.assertLess(time.perf_counter() - start, 0.3)


class SplicePlanTest(unittest.TestCase):
    def test_no_change_is_no_splice(self) -> None:
        self.assertIsNone(splice_plan(["a", "b"], ["a", "b"]))

    def test_move_touches_only_the_moved_stretch(self) -> None:
        self.assertEqual(splice_plan(list("abcdef"), list("abdcef")), (2, 2, ["d", "c"]))

    def test_insert_and_remove(self) -> None:
        self.assertEqual(splice_plan(list("abc"), list("abxc")), (2, 0, ["x"]))
        self.assertEqual(splice_plan(list("abxc"), list("abc")), (2, 1, []))

    def test_forced_item_is_recreated(self) -> None:
        self.assertEqual(splice_plan(list("abc"), list("abc"), force=["b"]), (1, 1, ["b"]))


class DropTargetTest(unittest.TestCase):
    def test_AT_L3_drop_above_a_sibling_paints_over_it(self) -> None:
        document, ids = fixture()
        editor = Editor(document)
        # Drag Card above Title: Card now paints on top inside the frame.
        parent, before = drop_target(document, [ids["card"]], ids["title"], "above")
        self.assertEqual((parent, before), (ids["frame"], None))
        editor.reorder(ids["card"], before, parent)
        self.assertEqual(layer_rows(editor.document)[2:], [(ids["card"], 1), (ids["title"], 1)])
        self.assertEqual(editor.undo_label, "Reorder Card")

    def test_drop_below_paints_under(self) -> None:
        document, ids = fixture()
        self.assertEqual(drop_target(document, [ids["title"]], ids["card"], "below"), (ids["frame"], ids["card"]))

    def test_drop_into_frame_reparents_on_top(self) -> None:
        document, ids = fixture()
        self.assertEqual(drop_target(document, [ids["dot"]], ids["frame"], "into"), (ids["frame"], None))

    def test_a_frame_cannot_be_dropped_into_itself_or_its_children(self) -> None:
        document, ids = fixture()
        self.assertIsNone(drop_target(document, [ids["frame"]], ids["card"], "above"))
        self.assertIsNone(drop_target(document, [ids["card"]], ids["dot"], "into"))



class FocusScopeTest(unittest.TestCase):
    def test_selecting_a_child_focuses_its_frame(self) -> None:
        from luma_canvas.layers import focus_scope
        document, ids = fixture()
        roots, members = focus_scope(document, [ids["card"]])
        self.assertEqual(roots, {ids["frame"]})
        self.assertEqual(members, {ids["frame"], ids["card"], ids["title"]})

    def test_selecting_a_frame_focuses_it_and_nothing_else(self) -> None:
        from luma_canvas.layers import focus_scope
        document, ids = fixture()
        roots, members = focus_scope(document, [ids["frame"]])
        self.assertNotIn(ids["dot"], members)
        self.assertEqual(focus_scope(document, [])[1], set())


if __name__ == "__main__":
    unittest.main()
