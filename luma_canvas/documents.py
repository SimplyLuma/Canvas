# SPDX-License-Identifier: MPL-2.0
"""Documents save themselves (CANVAS-SPEC 13.3).

A `DocumentSession` belongs to one window. It turns committed changes into
journal patches within a second, rewrites the file 1.5 s after editing stops
(and at least every 10 s while it continues), keeps version history, and does
all disk work on one worker thread from immutable snapshots, so the main loop
never waits on a disk.

GTK is not imported here. Timers and main-loop dispatch are injected: the
window passes GLib's, tests pass fakes and run the worker synchronously.
"""

from __future__ import annotations

import copy
import os
import pickle
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .autosave import SaveScheduler
from .journal import Journal, JournalRecord, diff_documents, is_edit
from .model import ChangeTracker, Document, LayoutObject, new_id, object_dict
from .persistence import (
    DOCUMENT_SUFFIX, DocumentStore, PackageError, data_home, documents_folder,
    next_free_path, state_home, write_package,
)
from .versions import AUTOMATIC, NAMED, OPENED, RESTORE_POINT, VersionStore

JOURNAL_BATCH = 0.8          # seconds; the spec allows one
SAVING_LABEL_DELAY = 0.5     # "Saving…" only when a save takes longer than this
CLOSE_TIMEOUT = 5.0


def clone(value: Any) -> Any:
    """An independent copy of a JSON-like snapshot: a pickle round trip, far
    faster than `copy.deepcopy` for documents with many layers."""
    return pickle.loads(pickle.dumps(value, pickle.HIGHEST_PROTOCOL))


# Paths handed to untitled documents in this process and not yet written, so
# two new windows changed in the same second do not both take "Untitled".
_reserved: set[Path] = set()
_reserved_lock = threading.Lock()


def reserve_untitled(folder: Path) -> Path:
    with _reserved_lock:
        candidate = next_free_path(folder)
        number = 2
        while candidate in _reserved or candidate.exists():
            candidate = folder / f"Untitled {number}{DOCUMENT_SUFFIX}"
            number += 1
        _reserved.add(candidate)
        return candidate


def release_reservation(path: Path | None) -> None:
    with _reserved_lock:
        _reserved.discard(path)


NEW_FRAME_SIZE = (1920.0, 1080.0)


def new_document(name: str = "Untitled") -> Document:
    """What New gives: one 1920 × 1080 white frame at the page origin.

    Nothing is selected and the document is not an edit yet, so it creates no
    file until the person changes something.
    """
    document = Document.empty(name)
    width, height = NEW_FRAME_SIZE
    document.objects.append(LayoutObject(
        new_id("frame"), "frame", "Frame 1", None, 0.0, 0.0, width, height, fill="#ffffff",
    ))
    document.select([])
    document.validate()
    return document


class Timers:
    """The main-loop timer interface a session needs."""

    def add(self, seconds: float, callback: Callable[[], None]) -> Any:
        from gi.repository import GLib

        def fire() -> bool:
            callback()
            return False
        return GLib.timeout_add(max(0, int(seconds * 1000)), fire)

    def remove(self, handle: Any) -> None:
        from gi.repository import GLib

        if handle:
            GLib.source_remove(handle)

    def dispatch(self, callback: Callable[..., None], *args: Any) -> None:
        from gi.repository import GLib

        def run() -> bool:
            callback(*args)
            return False
        GLib.idle_add(run)


# Above this many layers a version keeps no thumbnail: reading the document
# again and drawing all of it costs far more than the picture is worth, and it
# would hold the interpreter lock away from the window while it did.
THUMBNAIL_LAYER_LIMIT = 4000


def render_thumbnail(snapshot: dict[str, Any], size: int = 512) -> bytes | None:
    """A PNG of the whole page, at most `size` px on its long side."""
    if len(snapshot.get("objects", ())) > THUMBNAIL_LAYER_LIMIT:
        return None
    try:
        from . import export
        return export.thumbnail_png(Document.from_dict(snapshot), size)
    except Exception:  # a thumbnail is a nicety; never fail a version for it
        return None


@dataclass
class SaveState:
    SAVED = "saved"
    SAVING = "saving"
    FAILED = "failed"


class DocumentSession:
    def __init__(
        self,
        get_document: Callable[[], Document],
        *,
        path: Path | None,
        store: DocumentStore | None = None,
        journal: Journal | None = None,
        versions: VersionStore | None = None,
        folder: Path | None = None,
        timers: Timers | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_status: Callable[[str, str], None] | None = None,
        thumbnailer: Callable[[dict[str, Any]], bytes | None] | None = render_thumbnail,
        synchronous: bool = False,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.get_document = get_document
        self.path = path
        self.store = store or DocumentStore()
        self.journal = journal or Journal(state_home() / "journal")
        self.versions = versions or VersionStore(data_home() / "versions")
        self.folder = folder or documents_folder()
        self.timers = timers or Timers()
        self.clock = clock
        self.on_status = on_status or (lambda _state, _detail: None)
        self.thumbnailer = thumbnailer
        self.scheduler = SaveScheduler()
        self.state = SaveState.SAVED
        self.error = ""
        # A document Canvas named ("Untitled 3") until the person names it.
        self.untitled = path is None
        # True until the first edit. An untitled document closed while
        # pristine leaves no file behind.
        self.pristine = path is None
        document = get_document()
        # What the journal and the file know, kept up to date one changed layer
        # at a time: the document's fields, its layers by id, and their order.
        # These dicts are replaced, never changed, so the worker thread can
        # write a snapshot built from them while editing goes on.
        # A document just read from a file hands over the dictionary it was
        # read from, so opening one copies nothing on the main thread.
        full = snapshot if snapshot is not None else document.to_dict()
        self._fields: dict[str, Any] = {key: value for key, value in full.items() if key != "objects"}
        self._objects: dict[str, dict[str, Any]] = {item["id"]: item for item in full.get("objects", [])}
        self._order: list[str] = [item["id"] for item in full.get("objects", [])]
        self._resync = False
        # Layers changed or removed since the last save, so a save checks
        # only those (and the document-wide rules) before writing.
        self._unsaved_changed: set[str] = set()
        self._unsaved_removed: set[str] = set()
        self._unsaved_reordered = False
        self._tracker = ChangeTracker().open()
        self._saved: dict[str, Any] | None = None if path is None else self._snapshot()
        self._opened: dict[str, Any] = self._snapshot()
        self._pending_patches: list[dict[str, Any]] = []
        self._journal_started = False
        self._journal_timer: Any = None
        self._save_timer: Any = None
        self._retry_timer: Any = None
        self._saving_label_timer: Any = None
        self._closed = False
        self._synchronous = synchronous
        self._jobs: queue.Queue = queue.Queue()
        self._save_results: queue.SimpleQueue = queue.SimpleQueue()
        self._idle = threading.Event()
        self._idle.set()
        self._outstanding = 0
        self._outstanding_lock = threading.Lock()
        self._last_save_ok = True
        self._base_before: dict[str, Any] = self._opened
        if not synchronous:
            self._worker = threading.Thread(target=self._work, name="canvas-save", daemon=True)
            self._worker.start()

    # ── names ───────────────────────────────────────────────────────────

    @property
    def display_name(self) -> str:
        if self.path is not None and not self.untitled:
            return self.path.stem
        return "Untitled"

    @property
    def location_label(self) -> str:
        if self.path is None:
            return self.folder.name
        parent = self.path.parent
        home = Path.home()
        try:
            relative = parent.relative_to(home)
            parts = relative.parts
            return " › ".join(parts[-2:]) if parts else "Home"
        except ValueError:
            return parent.name or str(parent)

    # ── lifecycle ───────────────────────────────────────────────────────

    def opened(self) -> None:
        """A document was opened from a file: start its journal, keep a
        "Last opened" version."""
        document = self.get_document()
        self._start_journal(document)
        self._enqueue(self._job_version, clone(self._opened), dict(document.embedded_assets), OPENED, None)

    def adopt_recovered(self, pre_recovery: dict[str, Any] | None) -> None:
        """The window holds a document rebuilt from a journal: keep what the
        file held as a version, and save the recovered state now."""
        document = self.get_document()
        if pre_recovery is not None:
            self._enqueue(self._job_version, pre_recovery, dict(document.embedded_assets), RESTORE_POINT,
                          "Before recovery")
        self.pristine = False
        self._fields, self._objects, self._order, self._resync = {}, {}, [], True
        self.note_change()
        self.save_now()

    def adopt_imported(self, stem: str) -> Path:
        """A document imported from another format (a Figma file): give it a
        Canvas document of its own, `<stem>.canvas` in the Canvas folder, and
        save it now. The imported file itself is never written to."""
        base = " ".join(stem.replace("/", " ").replace("\0", " ").split()).lstrip(".") or "Untitled"
        with _reserved_lock:
            candidate = self.folder / f"{base}{DOCUMENT_SUFFIX}"
            number = 2
            while candidate in _reserved or candidate.exists():
                candidate = self.folder / f"{base} {number}{DOCUMENT_SUFFIX}"
                number += 1
            _reserved.add(candidate)
        self.path = candidate
        self.untitled = False
        self.pristine = False
        # The source file stays where it is, so there is nothing to journal:
        # write the package now.
        self.scheduler.changed(self.clock())
        self.save_now()
        return candidate

    # ── what changed ────────────────────────────────────────────────────

    def _snapshot(self) -> dict[str, Any]:
        """The document as last collected, as one plain dict (shares the
        collected dicts, which are never changed)."""
        return {**self._fields, "objects": [self._objects[object_id] for object_id in self._order]}

    def _collect(self) -> dict[str, Any]:
        """A patch from what was collected to the document now, and collect it.

        Only layers the change tracker reported (or that appeared) are read, so
        this costs what changed, not the size of the document."""
        document = self.get_document()
        # Only layers of this document: the same document read again (a version
        # thumbnail, recovery) makes other layers carrying the same ids.
        written = self._tracker.take_changed()
        ids = {object_id for object_id, layer in written.items()
               if layer is None or document.object(object_id) is layer}
        verify = self._snapshot() if os.environ.get("CANVAS_VERIFY_CHANGES") else None
        fields_now = document.fields_dict()
        patch: dict[str, Any] = {}
        changed = {key: value for key, value in fields_now.items() if self._fields.get(key) != value}
        unset = [key for key in self._fields if key not in fields_now]
        if changed:
            patch["set"] = changed
        if unset:
            patch["unset"] = unset
        self._base_before = self._snapshot()
        order = [item.id for item in document.objects]
        reordered = order != self._order
        if self._resync:
            candidates: set[str] = set(order)
            self._resync = False
        elif reordered:
            candidates = ids | (set(order) - self._objects.keys())
        else:
            candidates = ids
        # Trackers hear about every layer made or changed in the process (other
        # windows, imports), so only ids of this document are looked up.
        present_ids = set(order)
        layers = [layer for layer in (document.object(object_id) for object_id in candidates
                                      if object_id in present_ids) if layer is not None]
        upsert = {value["id"]: value for value in clone([object_dict(layer) for layer in layers])
                  if self._objects.get(value["id"]) != value}
        present = set(order) if reordered else None
        removed = [object_id for object_id in self._order if object_id not in present] if present is not None else []
        if upsert:
            patch["upsert"] = upsert
        if removed:
            patch["remove"] = removed
        if upsert or removed or reordered:
            patch["order"] = order
        self._unsaved_changed |= upsert.keys()
        self._unsaved_removed |= set(removed)
        self._unsaved_reordered = self._unsaved_reordered or reordered
        self._fields = fields_now
        if upsert or removed:
            objects = dict(self._objects)
            objects.update(upsert)
            for object_id in removed:
                objects.pop(object_id, None)
            self._objects = objects
        self._order = order
        if verify is not None:
            expected = diff_documents(verify, document.to_dict())
            if expected != patch:
                raise AssertionError(f"autosave missed a change: expected {sorted(expected)} "
                                     f"upserts {sorted(expected.get('upsert', {}))}, got {sorted(patch)} "
                                     f"upserts {sorted(patch.get('upsert', {}))}")
        return patch

    def note_change(self) -> None:
        """Call after anything that may have changed the document."""
        if self._closed:
            return
        document = self.get_document()
        patch = self._collect()
        if not patch:
            return
        edit = is_edit(patch)
        if not edit:
            # View-only changes ride along with the next save.
            return
        if self.pristine:
            self.pristine = False
            if self.path is None:
                self.path = reserve_untitled(self.folder)
            if not self._journal_started:
                self._start_journal(document, base_document=self._before_patch(patch) if self._saved is None else None)
        elif not self._journal_started:
            self._start_journal(document, base_document=self._before_patch(patch) if self._saved is None else None)
        self._pending_patches.append(patch)
        if self._journal_timer is None:
            self._journal_timer = self.timers.add(JOURNAL_BATCH, self._flush_journal)
        now = self.clock()
        deadline = self.scheduler.changed(now)
        self._arm_save(deadline, now)
        if self.scheduler.version_due():
            self.create_version(AUTOMATIC)

    def _before_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """The journal's base for a document with no file yet: the document as
        it was before `patch` (the patch itself is appended to the journal)."""
        return self._base_before

    def _arm_save(self, deadline: float | None, now: float) -> None:
        if deadline is None:
            return
        if self._save_timer is not None:
            self.timers.remove(self._save_timer)
        self._save_timer = self.timers.add(max(0.0, deadline - now), self._save_timer_fired)

    def _save_timer_fired(self) -> None:
        self._save_timer = None
        if self.scheduler.due(self.clock()):
            self.save_now()
        else:
            self._arm_save(self.scheduler.deadline(), self.clock())

    @property
    def dirty(self) -> bool:
        return self.scheduler.dirty or bool(self._pending_patches)

    def save_now(self) -> None:
        """Write the whole package now, if anything needs writing."""
        if self._closed and not self.dirty:
            return
        if self.pristine or self.path is None:
            return
        if not self.dirty and self.state != SaveState.FAILED:
            return
        document = self.get_document()
        pending = self._tracker.ids
        try:
            document.validate(self._unsaved_changed | pending, removed=self._unsaved_removed,
                              reordered=self._unsaved_reordered or [item.id for item in document.objects] != self._order)
        except ValueError as error:
            self._report(SaveState.FAILED, str(error))
            return
        tail = self._collect()
        if tail and is_edit(tail):
            self._pending_patches.append(tail)
        snapshot = self._snapshot()
        self._flush_journal()
        for timer in ("_save_timer", "_retry_timer"):
            handle = getattr(self, timer)
            if handle is not None:
                self.timers.remove(handle)
                setattr(self, timer, None)
        marks = self.scheduler.started_save()
        if self._saving_label_timer is None and self.state != SaveState.FAILED:
            self._saving_label_timer = self.timers.add(SAVING_LABEL_DELAY, self._show_saving)
        self._unsaved_changed, self._unsaved_removed, self._unsaved_reordered = set(), set(), False
        self._enqueue(self._job_save, snapshot, dict(document.embedded_assets),
                      self.path, marks)

    def _show_saving(self) -> None:
        self._saving_label_timer = None
        if not self._idle.is_set():
            self._report(SaveState.SAVING, "")

    def flush(self, timeout: float = CLOSE_TIMEOUT) -> bool:
        """Save everything now and wait for the disk, up to `timeout`.

        Used on focus loss (without waiting), close, quit and session end.
        Returns whether the document is safely on disk (or needs no file).
        """
        if self.pristine and self.path is None:
            return True
        self.save_now()
        self._flush_journal()
        if not self._synchronous:
            self._idle.wait(timeout)
        self._apply_save_results()
        return self._idle.is_set() and self.state != SaveState.FAILED and not self.scheduler.dirty

    def close(self) -> None:
        """The window is going away. Call after a successful `flush`."""
        self._closed = True
        self._tracker.close()
        for timer in ("_save_timer", "_retry_timer", "_journal_timer", "_saving_label_timer"):
            handle = getattr(self, timer)
            if handle is not None:
                self.timers.remove(handle)
                setattr(self, timer, None)
        if self.pristine and self.untitled:
            release_reservation(self.path)
            document = self.get_document()
            self._enqueue(self._job_discard_journal, document.id)
        if not self._synchronous:
            self._jobs.put(None)

    # ── journal ─────────────────────────────────────────────────────────

    def _start_journal(self, document: Document, base_document: dict[str, Any] | None = None) -> None:
        self._journal_started = True
        self._enqueue(self._job_journal_start, document.id, self.path, int(self._fields.get("revision", 0)),
                      clone(base_document) if base_document is not None else None)

    def _flush_journal(self) -> None:
        if self._journal_timer is not None:
            self.timers.remove(self._journal_timer)
            self._journal_timer = None
        if not self._pending_patches:
            return
        patches, self._pending_patches = self._pending_patches, []
        self._enqueue(self._job_journal_append, self.get_document().id, patches)

    # ── versions and document commands ─────────────────────────────────

    def create_version(self, kind: str = NAMED, name: str | None = None,
                       on_done: Callable[[Any], None] | None = None) -> None:
        document = self.get_document()
        tail = self._collect()
        if tail and is_edit(tail):
            self._pending_patches.append(tail)
        self.save_now()
        self._enqueue(self._job_version, self._snapshot(), dict(document.embedded_assets), kind, name, on_done)

    def list_versions(self):
        return self.versions.list(self.get_document().id)

    def load_version(self, version_id: str) -> tuple[dict[str, Any], dict[str, bytes]]:
        return self.versions.load(self.get_document().id, version_id)

    @property
    def last_opened(self) -> dict[str, Any]:
        return clone(self._opened)

    @property
    def last_saved(self) -> dict[str, Any] | None:
        return clone(self._saved) if self._saved is not None else None

    @property
    def has_saved(self) -> bool:
        """Whether there is a saved state to revert to, without copying it."""
        return self._saved is not None

    def keep_restore_point(self, name: str = "Before restoring") -> None:
        """Before anything replaces the document, keep what it was."""
        document = self.get_document()
        self._enqueue(self._job_version, document.to_dict(), dict(document.embedded_assets), RESTORE_POINT, name)

    def _check_name(self, name: str) -> str:
        name = name.strip()
        if not name or "/" in name or name in {".", ".."} or "\0" in name:
            raise ValueError("Use a name without slashes")
        return name

    def rename(self, name: str) -> Path:
        """Rename the file in place. Raises FileExistsError for a taken name."""
        name = self._check_name(name)
        folder = self.path.parent if self.path else self.folder
        target = folder / f"{name}{DOCUMENT_SUFFIX}"
        if self.path is not None and target == self.path:
            return target
        if target.exists():
            raise FileExistsError("A document with that name already exists")
        self.flush()
        if self.path is not None and self.path.exists():
            os.replace(self.path, target)
        release_reservation(self.path)
        self.path = target
        self.untitled = False
        self._after_relocation()
        return target

    def move(self, folder: Path) -> Path:
        target = folder / (self.path.name if self.path else f"{self.display_name}{DOCUMENT_SUFFIX}")
        if self.path is not None and target == self.path:
            return target
        if target.exists():
            raise FileExistsError("A document with that name already exists there")
        self.flush()
        folder.mkdir(parents=True, exist_ok=True)
        if self.path is not None and self.path.exists():
            shutil.move(str(self.path), str(target))
        release_reservation(self.path)
        self.path = target
        self._after_relocation()
        return target

    def _after_relocation(self) -> None:
        document = self.get_document()
        if self.pristine:
            self.pristine = False
            self._collect()
            self._start_journal(document, base_document=self._snapshot())
            self.scheduler.changed(self.clock())
            self.save_now()
        else:
            self._enqueue(self._job_journal_start, document.id, self.path,
                          int(self._fields.get("revision", 0)), None)
            self.scheduler.changed(self.clock())
            self.save_now()

    def copy_to(self, destination: Path, *, name: str | None = None) -> Path:
        """Write a copy with its own identity (its own versions and journal)."""
        document = self.get_document()
        document.validate()
        snapshot = document.to_dict()
        snapshot["id"] = new_id("document")
        snapshot["name"] = name or destination.stem
        write_package(snapshot, dict(document.embedded_assets), destination)
        return destination

    def duplicate(self) -> Path:
        folder = self.path.parent if self.path else self.folder
        target = next_free_path(folder, f"{self.display_name} copy")
        self.flush()
        return self.copy_to(target)

    # ── worker ──────────────────────────────────────────────────────────

    def _enqueue(self, job: Callable[..., None], *args: Any) -> None:
        if self._synchronous:
            job(*args)
            return
        with self._outstanding_lock:
            self._outstanding += 1
            self._idle.clear()
        self._jobs.put((job, args))

    def _work(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                return
            job, args = item
            try:
                job(*args)
            except Exception as error:  # keep the worker alive for the next job
                self.timers.dispatch(self._report, SaveState.FAILED, str(error))
            finally:
                with self._outstanding_lock:
                    self._outstanding -= 1
                    if self._outstanding == 0:
                        self._idle.set()

    def _job_journal_start(self, document_id: str, path: Path | None, revision: int,
                           base_document: dict[str, Any] | None) -> None:
        try:
            self.journal.start(document_id, path=path, base_revision=revision, base_document=base_document)
        except OSError:
            pass  # the recovery package below is the fallback

    def _job_journal_append(self, document_id: str, patches: list[dict[str, Any]]) -> None:
        try:
            self.journal.append(document_id, patches)
        except OSError:
            pass

    def _job_discard_journal(self, document_id: str) -> None:
        self.journal.discard(document_id)

    def _job_save(self, snapshot: dict[str, Any], assets: dict[str, bytes], path: Path,
                  marks: tuple[float | None, float | None]) -> None:
        try:
            write_package(snapshot, assets, path)
        except OSError as error:
            try:
                recovery = Document.from_dict(snapshot)
                recovery.embedded_assets = assets
                self.store.autosave(recovery, path)
            except (OSError, ValueError):
                pass
            self._dispatch_save_result(self._save_failed, marks, _describe_error(error, path))
            return
        try:
            self.journal.start(str(snapshot["id"]), path=path, base_revision=int(snapshot.get("revision", 0)))
        except OSError:
            pass
        self.store.discard_recovery(str(snapshot["id"]))
        release_reservation(path)
        self._dispatch_save_result(self._save_succeeded, snapshot)

    def _job_version(self, snapshot: dict[str, Any], assets: dict[str, bytes], kind: str,
                     name: str | None, on_done: Callable[[Any], None] | None = None) -> None:
        thumbnail = self.thumbnailer(snapshot) if self.thumbnailer else None
        version = self.versions.create(snapshot, kind=kind, name=name, embedded_assets=assets, thumbnail=thumbnail)
        if on_done is not None:
            self.timers.dispatch(on_done, version)

    # ── results, on the main loop ───────────────────────────────────────

    def _dispatch_save_result(self, callback, *args) -> None:
        self._save_results.put((callback, args))
        self.timers.dispatch(self._apply_save_results)

    def _apply_save_results(self) -> None:
        # flush blocks the main loop while waiting for disk. Acknowledge completed
        # writes here as well as from idle dispatch, without pumping unrelated UI.
        while True:
            try:
                callback, args = self._save_results.get_nowait()
            except queue.Empty:
                return
            callback(*args)

    def _save_succeeded(self, snapshot: dict[str, Any]) -> None:
        self._saved = snapshot
        self.scheduler.save_succeeded()
        if self._saving_label_timer is not None:
            self.timers.remove(self._saving_label_timer)
            self._saving_label_timer = None
        if not self.scheduler.dirty:
            self._report(SaveState.SAVED, "")

    def _save_failed(self, marks: tuple[float | None, float | None], message: str) -> None:
        if self._saving_label_timer is not None:
            self.timers.remove(self._saving_label_timer)
            self._saving_label_timer = None
        now = self.clock()
        retry_at = self.scheduler.save_failed(marks, now)
        self._report(SaveState.FAILED, message)
        if not self._closed:
            if self._retry_timer is not None:
                self.timers.remove(self._retry_timer)
            self._retry_timer = self.timers.add(retry_at - now, self._retry)

    def _retry(self) -> None:
        self._retry_timer = None
        self.save_now()

    def _report(self, state: str, detail: str) -> None:
        self.state = state
        self.error = detail
        self.on_status(state, detail)


def _describe_error(error: OSError, path: Path) -> str:
    import errno

    if error.errno == errno.ENOSPC:
        return "The disk is full."
    if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return "Canvas isn’t allowed to write to that folder."
    if error.errno == errno.ENOENT:
        return "The folder it was in is gone."
    return str(error.strerror or error)


# ── recovery at launch ──────────────────────────────────────────────────

@dataclass
class Recovered:
    document: Document
    path: Path | None
    pre_recovery: dict[str, Any] | None


def recover(journal: Journal, store: DocumentStore, folder: Path | None = None) -> list[Recovered]:
    """Rebuild every document whose journal holds changes its file lacks.

    A journal whose file has since been saved at a later revision is stale and
    is dropped. A journal that cannot be replayed is left alone for a later
    attempt rather than deleted.
    """
    found: list[Recovered] = []
    for record in journal.pending():
        path = record.path
        base: dict[str, Any] | None = None
        assets: dict[str, bytes] = {}
        if path is not None and path.exists():
            try:
                saved = store.load(path)
            except PackageError:
                saved = None
            if saved is not None:
                if saved.id != record.document_id or saved.revision > int(record.header.get("base_revision", 0)) \
                        and "base_document" not in record.header:
                    journal.discard(record.document_id)
                    continue
                base = saved.to_dict()
                assets = saved.embedded_assets
        if base is None:
            base = record.header.get("base_document")
        if base is None:
            continue
        try:
            document = Document.from_dict(record.replay(copy.deepcopy(base)))
        except (ValueError, KeyError, TypeError):
            continue
        document.embedded_assets = assets
        if path is None:
            path = reserve_untitled(folder or documents_folder())
        pre = base if path is not None and path.exists() else None
        found.append(Recovered(document, path, pre))
    return found
