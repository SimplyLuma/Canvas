# SPDX-License-Identifier: MPL-2.0
"""Packages and journals written a slice of layers at a time are the same
bytes (packages) and the same JSON (journals) as one encoder call."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from luma_canvas.journal import Journal, apply_patch, diff_documents, patch_line
from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.persistence import _canonical_json


def big_document(count: int = 950) -> Document:
    document = Document.empty("Big — “quotes” and ünïcödé")
    frame = LayoutObject(new_id("frame"), "frame", "Frame ✓", None, 0, 0, 1000, 1000, fill="#ffffff")
    document.objects.append(frame)
    for index in range(count):
        document.objects.append(LayoutObject(
            new_id("rectangle"), "rectangle", f"Box {index} “{index}”", frame.id, index, index, 10, 10,
            fill="#2060e0", metadata={"note": "ünïcödé", "nested": {"b": [1, 2, {"c": None}], "a": 1.5}},
            path=[] if index % 2 else [{"x": 0, "y": 0}, {"x": 1.25, "y": 2, "closed": True}]))
    return document


class ChunkedJsonTests(unittest.TestCase):
    def test_a_package_document_is_byte_identical(self):
        snapshot = big_document().to_dict()
        expected = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        for chunk in (1, 7, 200, 10_000):
            self.assertEqual(_canonical_json(snapshot, chunk=chunk), expected, chunk)

    def test_empty_and_small_documents_are_unchanged(self):
        for snapshot in (Document.empty("Empty").to_dict(), big_document(3).to_dict(), {"a": 1}):
            expected = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            self.assertEqual(_canonical_json(snapshot, chunk=2), expected)

    def test_a_journal_line_is_the_same_json(self):
        before = big_document(5).to_dict()
        after_document = big_document(900)
        after_document.id = before["id"]
        after = after_document.to_dict()
        patch = diff_documents(before, after)
        self.assertGreater(len(patch["upsert"]), 200)
        line = patch_line(patch, chunk=50)
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(json.loads(line), {"type": "patch", **patch})
        self.assertEqual(line, json.dumps({"type": "patch", **patch}, ensure_ascii=False, sort_keys=True) + "\n")

    def test_journals_written_in_slices_replay(self):
        before = big_document(10).to_dict()
        after_document = big_document(600)
        after_document.id = before["id"]
        after = after_document.to_dict()
        patch = diff_documents(before, after)
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder))
            journal.start(before["id"], path=None, base_revision=0, base_document=before)
            journal.append(before["id"], [patch])
            record = journal.read(before["id"])
        self.assertEqual(record.replay(), apply_patch(before, patch))
        self.assertEqual(record.replay()["objects"], after["objects"])


if __name__ == "__main__":
    unittest.main()
