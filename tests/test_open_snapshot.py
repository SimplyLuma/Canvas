# SPDX-License-Identifier: MPL-2.0
"""Opening a document hands its session the dictionary it was read from."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from luma_canvas.documents import DocumentSession
from luma_canvas.editing import Editor
from luma_canvas.journal import Journal, is_edit
from luma_canvas.model import Document, LayoutObject, new_id
from luma_canvas.persistence import DocumentStore, write_package
from luma_canvas.versions import VersionStore


class Timers:
    def __init__(self) -> None:
        self.now = 0.0
        self.pending: dict[int, tuple[float, object]] = {}
        self.counter = 0

    def clock(self) -> float:
        return self.now

    def add(self, seconds, callback):
        self.counter += 1
        self.pending[self.counter] = (self.now + seconds, callback)
        return self.counter

    def remove(self, handle):
        self.pending.pop(handle, None)

    def dispatch(self, callback, *args):
        callback(*args)


def document_with(count: int = 40) -> Document:
    document = Document.empty("Opened")
    frame = LayoutObject(new_id("frame"), "frame", "Frame", None, 0, 0, 900, 900, fill="#ffffff")
    document.objects.append(frame)
    for index in range(count):
        document.objects.append(LayoutObject(new_id("rectangle"), "rectangle", f"Box {index}", frame.id,
                                             index, index, 10, 10, fill="#2060e0"))
    document.validate()
    return document


class OpenSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.path = self.root / "Opened.canvas"
        DocumentStore.save(document_with(), self.path)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def session(self, editor: Editor, snapshot: dict | None) -> DocumentSession:
        timers = Timers()
        return DocumentSession(lambda: editor.document, path=self.path, journal=Journal(self.root / "journal"),
                               versions=VersionStore(self.root / "versions"), folder=self.root, timers=timers,
                               clock=timers.clock, thumbnailer=None, synchronous=True, snapshot=snapshot)

    def test_the_snapshot_is_what_the_document_holds(self) -> None:
        document, snapshot = DocumentStore.load_with_snapshot(self.path)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot, document.to_dict())
        self.assertEqual(DocumentStore.load(self.path).to_dict(), document.to_dict())

    def test_a_session_given_the_snapshot_patches_the_same_way(self) -> None:
        patches = []
        for use_snapshot in (False, True):
            document, snapshot = DocumentStore.load_with_snapshot(self.path)
            editor = Editor(document)
            session = self.session(editor, snapshot if use_snapshot else None)
            self.assertEqual(session._snapshot(), document.to_dict())
            self.assertTrue(session.has_saved)
            with editor.transaction("Move"):
                document.objects[1].x = 500
            patches.append(session._collect())
            session.close()
        self.assertEqual(patches[0], patches[1])
        self.assertTrue(is_edit(patches[1]))
        self.assertEqual(sorted(patches[1]["upsert"]), [self.first_box_id()])

    def first_box_id(self) -> str:
        document = DocumentStore.load(self.path)
        return document.objects[1].id

    def test_reading_without_copying_gives_the_same_document(self) -> None:
        data = json.loads(json.dumps(document_with(6).to_dict()))
        copied = Document.from_dict(data)
        shared = Document.from_dict(data, copy_input=False)
        self.assertEqual(copied.to_dict(), shared.to_dict())
        self.assertIsNot(copied.objects[0].metadata, data["objects"][0]["metadata"])
        self.assertIs(shared.objects[0].metadata, data["objects"][0]["metadata"])
        # Editing the document leaves the dictionary it was read from alone.
        editor = Editor(shared)
        with editor.transaction("Rename"):
            shared.objects[1].name = "Renamed"
            shared.objects[1].metadata = {**shared.objects[1].metadata, "note": "x"}
        self.assertEqual(data["objects"][1]["name"], "Box 0")
        self.assertNotIn("note", data["objects"][1].get("metadata", {}))

    def test_the_same_document_read_twice_is_not_taken_for_edits(self) -> None:
        document, snapshot = DocumentStore.load_with_snapshot(self.path)
        editor = Editor(document)
        session = self.session(editor, snapshot)
        # A version thumbnail or recovery reads the same file again: the layers
        # it makes carry this document's ids but are not its layers.
        DocumentStore.load(self.path)
        self.assertEqual(session._collect(), {})
        with editor.transaction("Move"):
            document.objects[1].x = 321
        patch = session._collect()
        self.assertEqual(sorted(patch.get("upsert", {})), [document.objects[1].id])
        session.close()

    def test_huge_documents_keep_no_version_thumbnail(self) -> None:
        from luma_canvas.documents import THUMBNAIL_LAYER_LIMIT, render_thumbnail
        small = document_with(3).to_dict()
        huge = dict(small)
        huge["objects"] = small["objects"] * (THUMBNAIL_LAYER_LIMIT // len(small["objects"]) + 1)
        self.assertGreater(len(huge["objects"]), THUMBNAIL_LAYER_LIMIT)
        self.assertIsNone(render_thumbnail(huge, 64))
        # A small document keeps its thumbnail wherever Canvas can draw one
        # (a machine without a renderer returns None for every document).
        if render_thumbnail(document_with(2).to_dict(), 64) is not None:
            self.assertIsNotNone(render_thumbnail(small, 64))

    def test_a_migrated_document_brings_no_snapshot(self) -> None:
        document = document_with(3)
        older = document.to_dict()
        older["schema_version"] = 1
        for item in older["objects"]:
            item.pop("fills", None)
            item.pop("strokes", None)
        path = self.root / "Old.canvas"
        write_package(older, {}, path)
        loaded, snapshot = DocumentStore.load_with_snapshot(path)
        self.assertIsNone(snapshot)
        self.assertEqual(loaded.schema_version, document.schema_version)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(json.loads(archive.read("document.json"))["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
