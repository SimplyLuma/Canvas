# SPDX-License-Identifier: MPL-2.0
"""Autosave timing, journal crash safety, versions and document commands."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest import mock

from luma_canvas.autosave import SaveScheduler
from luma_canvas.documents import DocumentSession, recover
from luma_canvas.editing import Editor
from luma_canvas.journal import Journal, apply_patch, diff_documents, is_edit
from luma_canvas.model import Document
from luma_canvas.persistence import (
    DocumentDamaged, DocumentStore, DocumentTooNew, documents_folder, next_free_path, write_package,
)
from luma_canvas.versions import AUTOMATIC, DAY, HOUR, NAMED, OPENED, VersionStore, Version, retained


class FakeTimers:
    """A manual clock: timers fire when `advance` passes their deadline."""

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

    def advance(self, seconds: float) -> None:
        target = self.now + seconds
        while True:
            due = sorted((when, handle) for handle, (when, _cb) in self.pending.items() if when <= target)
            if not due:
                break
            when, handle = due[0]
            _when, callback = self.pending.pop(handle)
            self.now = max(self.now, when)
            callback()
        self.now = target


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder = self.root / "Documents" / "Canvas"
        self.timers = FakeTimers()
        self.journal = Journal(self.root / "state" / "journal")
        self.versions = VersionStore(self.root / "data" / "versions", clock=lambda: 1_000_000 + self.timers.now)
        self.store = DocumentStore(self.root / "state" / "recovery")
        self.statuses: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def session(self, editor: Editor, path: Path | None = None) -> DocumentSession:
        return DocumentSession(
            lambda: editor.document, path=path, store=self.store, journal=self.journal,
            versions=self.versions, folder=self.folder, timers=self.timers, clock=self.timers.clock,
            on_status=lambda state, detail: self.statuses.append((state, detail)),
            thumbnailer=None, synchronous=True,
        )


class DeferredSaveCompletionTest(Fixture):
    def test_flush_acknowledges_write_without_main_loop_dispatch(self):
        editor = Editor(Document.empty('Fresh'))
        timers = FakeTimers()
        callbacks = []
        timers.dispatch = lambda callback,*args:callbacks.append((callback,args))
        session = DocumentSession(lambda:editor.document,path=None,store=self.store,journal=self.journal,
            versions=self.versions,folder=self.folder,timers=timers,thumbnailer=None)
        try:
            editor.create("rectangle", 0, 0, 100, 100)
            session.note_change()
            self.assertTrue(session.flush())
            self.assertFalse(session.scheduler.dirty)
            self.assertEqual(DocumentStore.load(session.path).name, 'Fresh')
            for callback,args in callbacks:callback(*args)
            self.assertTrue(session.flush())
        finally:
            session.close()


class SchedulerTest(unittest.TestCase):
    def test_debounce_and_ten_second_ceiling(self) -> None:
        scheduler = SaveScheduler()
        self.assertEqual(scheduler.changed(0.0), 1.5)
        for second in range(1, 12):
            deadline = scheduler.changed(float(second))
            self.assertLessEqual(deadline, 10.0)
        self.assertTrue(scheduler.due(10.0))

    def test_failure_backs_off_to_a_minute(self) -> None:
        scheduler = SaveScheduler()
        scheduler.changed(0)
        delays = []
        for _ in range(6):
            marks = scheduler.started_save()
            delays.append(scheduler.save_failed(marks, 100.0) - 100.0)
        self.assertEqual(delays, [10.0, 20.0, 40.0, 60.0, 60.0, 60.0])
        self.assertTrue(scheduler.dirty)


class JournalTest(Fixture):
    def test_patches_round_trip_every_kind_of_change(self) -> None:
        document = Document.demonstration()
        before = document.to_dict()
        editor = Editor(document)
        first = document.objects[3]
        editor.move([first.id], 10, 5)
        editor.delete([document.objects[-1].id])
        editor.create("rectangle", 1, 2, 30, 40)
        editor.reorder(first.id, None, first.parent_id)
        after = editor.document.to_dict()
        patch = diff_documents(before, after)
        self.assertTrue(is_edit(patch))
        self.assertEqual(apply_patch(before, patch), after)

    def test_view_changes_are_not_edits(self) -> None:
        document = Document.empty()
        before = document.to_dict()
        document.zoom = 2.0
        document.canvas_x = 40
        self.assertFalse(is_edit(diff_documents(before, document.to_dict())))

    def test_torn_last_line_is_ignored(self) -> None:
        document = Document.empty()
        self.journal.start(document.id, path=None, base_revision=0, base_document=document.to_dict())
        editor = Editor(document)
        editor.create("rectangle", 0, 0, 10, 10)
        self.journal.append(document.id, [diff_documents(document.to_dict() | {"objects": []}, editor.document.to_dict())])
        with self.journal.path_for(document.id).open("a") as stream:
            stream.write('{"type": "patch", "upsert": {"cut')
        record = self.journal.read(document.id)
        self.assertEqual(len(record.patches), 1)
        self.assertEqual(len(Document.from_dict(record.replay()).objects), 1)


class AutosaveTest(Fixture):
    def test_AT_D4_first_change_creates_untitled_file(self) -> None:
        editor = Editor(Document.empty())
        session = self.session(editor)
        self.assertIsNone(session.path)
        editor.create("rectangle", 10, 10, 100, 50)
        session.note_change()
        self.assertEqual(session.path, self.folder / "Untitled.canvas")
        self.timers.advance(2.0)
        saved = DocumentStore.load(self.folder / "Untitled.canvas")
        self.assertEqual([item.kind for item in saved.objects], ["rectangle"])
        second_editor = Editor(Document.empty())
        second = self.session(second_editor)
        second_editor.create("ellipse", 0, 0, 5, 5)
        second.note_change()
        self.assertEqual(second.path, self.folder / "Untitled 2.canvas")

    def test_AT_D5_unchanged_new_document_leaves_no_file(self) -> None:
        editor = Editor(Document.empty())
        session = self.session(editor)
        editor.document.zoom = 3.0  # looking is not editing
        session.note_change()
        self.assertTrue(session.flush())
        session.close()
        self.assertFalse(self.folder.exists() and any(self.folder.iterdir()))
        self.assertEqual(self.journal.pending(), [])

    def test_AT_D6_close_right_after_an_edit_keeps_it(self) -> None:
        path = self.root / "Named.canvas"
        document = Document.empty("Named")
        DocumentStore.save(document, path)
        editor = Editor(DocumentStore.load(path))
        session = self.session(editor, path)
        session.opened()
        editor.create("text", 0, 0, 100, 20)
        session.note_change()
        self.timers.advance(0.3)
        self.assertTrue(session.flush())
        session.close()
        self.assertEqual(len(DocumentStore.load(path).objects), 1)

    def test_AT_D7_continuous_editing_saves_at_least_every_ten_seconds(self) -> None:
        editor = Editor(Document.empty())
        session = self.session(editor)
        writes = []
        original = write_package

        def counting(snapshot, assets, destination):
            writes.append((self.timers.now, len(snapshot["objects"])))
            return original(snapshot, assets, destination)

        with mock.patch("luma_canvas.documents.write_package", counting):
            for second in range(25):
                editor.create("rectangle", second, 0, 10, 10)
                session.note_change()
                self.timers.advance(1.0)
            self.timers.advance(1.5)
        self.assertGreaterEqual(len([when for when, _ in writes if when <= 25]), 2)
        self.assertEqual(writes[-1][1], 25)
        self.assertEqual(len(DocumentStore.load(session.path).objects), 25)

    def test_AT_D8_crash_after_edit_is_recovered_from_the_journal(self) -> None:
        path = self.root / "Crash.canvas"
        DocumentStore.save(Document.empty("Crash"), path)
        editor = Editor(DocumentStore.load(path))
        session = self.session(editor, path)
        session.opened()
        editor.create("rectangle", 0, 0, 10, 10)
        session.note_change()
        self.timers.advance(1.2)   # journal flushed (0.8 s), save not yet due
        # kill -9: nothing else runs. The file still lacks the edit.
        self.assertEqual(len(DocumentStore.load(path).objects), 0)
        found = recover(self.journal, self.store, self.folder)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, path)
        self.assertEqual(len(found[0].document.objects), 1)
        self.assertIsNotNone(found[0].pre_recovery)

    def test_untitled_crash_before_first_save_is_recovered(self) -> None:
        editor = Editor(Document.empty())
        session = self.session(editor)
        editor.create("ellipse", 0, 0, 10, 10)
        session.note_change()
        self.timers.advance(1.0)
        found = recover(self.journal, self.store, self.folder)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].document.objects[0].kind, "ellipse")

    def test_saved_journal_is_not_replayed(self) -> None:
        editor = Editor(Document.empty())
        session = self.session(editor)
        editor.create("ellipse", 0, 0, 10, 10)
        session.note_change()
        self.timers.advance(3.0)
        self.assertEqual(recover(self.journal, self.store, self.folder), [])

    def test_AT_D11_unwritable_folder_fails_retries_and_recovers(self) -> None:
        path = self.root / "ReadOnly" / "Doc.canvas"
        DocumentStore.save(Document.empty("Doc"), path)
        editor = Editor(DocumentStore.load(path))
        session = self.session(editor, path)
        session.opened()
        failing = mock.patch("luma_canvas.documents.write_package",
                             side_effect=OSError(errno.ENOSPC, "No space left on device"))
        with failing:
            editor.create("rectangle", 0, 0, 10, 10)
            session.note_change()
            self.timers.advance(2.0)
            self.assertEqual(session.state, "failed")
            self.assertEqual(session.error, "The disk is full.")
            self.assertEqual(self.journal.read(editor.document.id).patches[0]["upsert"].popitem()[1]["kind"], "rectangle")
            self.assertTrue(self.store.recoverable())
        self.timers.advance(10.5)
        self.assertEqual(session.state, "saved")
        self.assertEqual(len(DocumentStore.load(path).objects), 1)

    def test_rename_move_duplicate_and_copy(self) -> None:
        editor = Editor(Document.empty())
        session = self.session(editor)
        editor.create("rectangle", 0, 0, 10, 10)
        session.note_change()
        self.timers.advance(2)
        renamed = session.rename("Onboarding flow")
        self.assertEqual(renamed.name, "Onboarding flow.canvas")
        self.assertFalse((self.folder / "Untitled.canvas").exists())
        self.assertTrue(renamed.exists())
        other = Editor(Document.empty())
        taken = self.session(other)
        other.create("rectangle", 0, 0, 1, 1)
        taken.note_change()
        self.timers.advance(2)
        with self.assertRaises(FileExistsError):
            taken.rename("Onboarding flow")
        copy_path = session.duplicate()
        self.assertEqual(copy_path.name, "Onboarding flow copy.canvas")
        self.assertNotEqual(DocumentStore.load(copy_path).id, editor.document.id)
        moved = session.move(self.root / "Clients")
        self.assertEqual(moved, self.root / "Clients" / "Onboarding flow.canvas")
        self.assertTrue(moved.exists() and not renamed.exists())

    def test_versions_follow_the_document_through_a_rename(self) -> None:
        editor = Editor(Document.empty())
        session = self.session(editor)
        editor.create("rectangle", 0, 0, 10, 10)
        session.note_change()
        session.create_version(NAMED)
        session.rename("Kept")
        self.assertEqual([version.title for version in session.list_versions()], ["Version 1"])
        snapshot, _assets = session.load_version(session.list_versions()[0].id)
        self.assertEqual(len(snapshot["objects"]), 1)


class CrashSafetyTest(Fixture):
    def test_crash_during_write_leaves_the_old_file(self) -> None:
        path = self.root / "Safe.canvas"
        DocumentStore.save(Document.empty("Safe"), path)
        original = path.read_bytes()
        changed = Document.demonstration()
        with mock.patch("luma_canvas.persistence.os.replace", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                write_package(changed.to_dict(), {}, path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([p.name for p in self.root.iterdir()], ["Safe.canvas"])

    def test_AT_D17_truncated_package_is_an_error_not_an_empty_document(self) -> None:
        path = self.root / "Broken.canvas"
        DocumentStore.save(Document.demonstration(), path)
        path.write_bytes(path.read_bytes()[:200])
        with self.assertRaises(DocumentDamaged) as raised:
            DocumentStore.load(path)
        self.assertEqual(raised.exception.reason, "The file is damaged.")

    def test_newer_schema_says_so(self) -> None:
        path = self.root / "Future.canvas"
        snapshot = Document.empty().to_dict()
        snapshot["schema_version"] = 99
        write_package(snapshot, {}, path)
        with self.assertRaises(DocumentTooNew) as raised:
            DocumentStore.load(path)
        self.assertIn("newer version", raised.exception.reason)


class VersionTest(Fixture):
    def test_AT_D21_retention_keeps_hourly_then_daily_and_named(self) -> None:
        now = 100 * DAY
        versions = []
        for index in range(3 * 24 * 6):   # every ten minutes for three days
            created = now - index * 600
            versions.append(Version(id=f"a{index}", created=created, kind=AUTOMATIC))
        versions.append(Version(id="named", created=now - 40 * DAY, kind=NAMED, name="Launch"))
        versions += [Version(id=f"o{index}", created=now - index * DAY, kind=OPENED) for index in range(12)]
        keep = retained(versions, now)
        last_day = [v for v in versions if v.kind == AUTOMATIC and now - v.created <= DAY]
        self.assertTrue(all(v.id in keep for v in last_day))
        older = [v for v in versions if v.kind == AUTOMATIC and now - v.created > DAY]
        hours = {int(v.created // HOUR) for v in older}
        self.assertEqual(len([v for v in older if v.id in keep]), len(hours))
        self.assertIn("named", keep)
        self.assertEqual(len([v for v in versions if v.kind == OPENED and v.id in keep]), 10)

    def test_assets_are_stored_once(self) -> None:
        document = Document.empty()
        store = VersionStore(self.root / "v")
        store.create(document.to_dict(), embedded_assets={"a": b"png"})
        store.create(document.to_dict(), embedded_assets={"a": b"png"})
        self.assertEqual(len(list((store.folder(document.id) / "assets").iterdir())), 1)


class FolderTest(unittest.TestCase):
    def test_documents_folder_reads_user_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            config.mkdir()
            (config / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Papers"\n')
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
                self.assertEqual(documents_folder(), Path.home() / "Papers" / "Canvas")

    def test_next_free_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "Untitled.canvas").touch()
            (folder / "Untitled 2.canvas").touch()
            self.assertEqual(next_free_path(folder).name, "Untitled 3.canvas")


if __name__ == "__main__":
    unittest.main()
