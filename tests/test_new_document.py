# SPDX-License-Identifier: MPL-2.0
"""What New gives, and that it is not yet an edit."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from luma_canvas.documents import DocumentSession, new_document
from luma_canvas.editing import Editor
from luma_canvas.journal import Journal
from luma_canvas.versions import VersionStore
from tests.test_autosave import FakeTimers


class NewDocumentTest(unittest.TestCase):
    def test_new_document_has_one_hd_frame_at_the_origin(self) -> None:
        document = new_document()
        self.assertEqual(len(document.objects), 1)
        frame = document.objects[0]
        self.assertEqual((frame.kind, frame.name), ("frame", "Frame 1"))
        self.assertEqual((frame.x, frame.y, frame.width, frame.height), (0.0, 0.0, 1920.0, 1080.0))
        self.assertEqual(frame.fill, "#ffffff")
        self.assertIsNone(frame.parent_id)
        self.assertEqual(document.selected_ids, [])

    def test_new_document_is_not_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            timers = FakeTimers()
            editor = Editor(new_document())
            session = DocumentSession(lambda: editor.document, path=None, journal=Journal(root / "j"),
                                      versions=VersionStore(root / "v"), folder=root / "Canvas",
                                      timers=timers, clock=timers.clock, thumbnailer=None, synchronous=True)
            editor.document.zoom = 0.4   # the initial fit
            session.note_change()
            timers.advance(20)
            self.assertTrue(session.pristine)
            self.assertIsNone(session.path)
            self.assertFalse((root / "Canvas").exists())
            editor.create("rectangle", 10, 10, 50, 50)
            session.note_change()
            timers.advance(2)
            self.assertTrue((root / "Canvas" / "Untitled.canvas").exists())


if __name__ == "__main__":
    unittest.main()
