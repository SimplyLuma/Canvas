# SPDX-License-Identifier: MPL-2.0
"""The crash journal: every committed change reaches disk within a second.

A document's file is rewritten a second and a half after editing stops. Between
those saves each committed change is appended here as a small patch against the
last state written to the journal, so a crash, a power cut or `kill -9` costs
at most the change made in the last second.

The file is JSON lines. The first line is a header naming the document, the
file the patches apply to and the revision that file held; when the document
has never been written (a new untitled document) the header carries the base
document itself. Every following line is one patch. A line cut short by a
crash is ignored, so a torn write never makes the journal unreadable.

This module has no GTK dependency so it can be tested and run on any thread.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Keys that describe how the document is being looked at rather than what it
# is. They travel in patches (so recovery restores the view) but a change to
# them alone is not an edit: it never creates a file or schedules a save.
VIEW_KEYS = frozenset({
    "selected_ids", "canvas_x", "canvas_y", "zoom", "modified_at", "revision",
    "workspaces", "arrangement",
})


def diff_documents(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """A patch that turns `before` into `after`.

    Objects are keyed by id so moving one rectangle writes one object, not the
    whole list. Everything else is replaced key by key.
    """
    patch: dict[str, Any] = {}
    changed = {key: value for key, value in after.items()
               if key != "objects" and before.get(key) != value}
    removed_keys = [key for key in before if key not in after and key != "objects"]
    if changed:
        patch["set"] = changed
    if removed_keys:
        patch["unset"] = removed_keys
    old_objects = {item["id"]: item for item in before.get("objects", [])}
    new_objects = after.get("objects", [])
    upsert = {item["id"]: item for item in new_objects
              if old_objects.get(item["id"]) != item}
    new_ids = [item["id"] for item in new_objects]
    present = set(new_ids)
    removed = [object_id for object_id in old_objects if object_id not in present]
    if upsert:
        patch["upsert"] = upsert
    if removed:
        patch["remove"] = removed
    if upsert or removed or [item["id"] for item in before.get("objects", [])] != new_ids:
        patch["order"] = new_ids
    return patch


def is_edit(patch: dict[str, Any]) -> bool:
    """Whether a patch changes the document's content, not only its view."""
    if any(key in patch for key in ("upsert", "remove", "unset")):
        return True
    if "order" in patch:
        return True
    return any(key not in VIEW_KEYS for key in patch.get("set", {}))


def apply_patch(document: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply one patch to a document dictionary, returning a new dictionary."""
    result = copy.deepcopy(document)
    for key, value in patch.get("set", {}).items():
        result[key] = copy.deepcopy(value)
    for key in patch.get("unset", []):
        result.pop(key, None)
    if "order" in patch or "upsert" in patch or "remove" in patch:
        objects = {item["id"]: item for item in result.get("objects", [])}
        for object_id in patch.get("remove", []):
            objects.pop(object_id, None)
        for object_id, item in patch.get("upsert", {}).items():
            objects[object_id] = copy.deepcopy(item)
        order = patch.get("order") or list(objects)
        result["objects"] = [objects[object_id] for object_id in order if object_id in objects]
    return result


@dataclass
class JournalRecord:
    header: dict[str, Any]
    patches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def document_id(self) -> str:
        return str(self.header.get("document_id", ""))

    @property
    def path(self) -> Path | None:
        value = self.header.get("path")
        return Path(value) if value else None

    def replay(self, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """The document as it was after the last journaled change."""
        document = base if base is not None else self.header.get("base_document")
        if document is None:
            raise ValueError("journal has no base document")
        for patch in self.patches:
            document = apply_patch(document, patch)
        return document


def patch_line(patch: dict[str, Any], *, chunk: int = 50, pause: float = 0.001) -> str:
    """One journal line for a patch, the same JSON `json.dumps(..., sort_keys=True)`
    writes, encoded a slice of layers at a time.

    The save worker writes these while the window edits. Python's JSON encoder
    holds the interpreter lock for a whole call, so one call over a patch of
    twenty thousand layers froze the window; between slices the lock is given
    back to the main thread. The pause is real (sleep(0) does not make the
    other thread run first)."""
    import time
    upsert = patch.get("upsert")
    if not upsert or len(upsert) <= chunk:
        return json.dumps({"type": "patch", **patch}, ensure_ascii=False, sort_keys=True) + "\n"
    rest = {key: value for key, value in patch.items() if key != "upsert"}
    head = json.dumps({"type": "patch", **rest}, ensure_ascii=False, sort_keys=True)
    entries = sorted(upsert.items())
    parts: list[str] = []
    for start in range(0, len(entries), chunk):
        parts.extend(json.dumps(key, ensure_ascii=False) + ": "
                     + json.dumps(value, ensure_ascii=False, sort_keys=True)
                     for key, value in entries[start:start + chunk])
        if pause:
            time.sleep(pause)
    body = '"upsert": {' + ", ".join(parts) + "}"
    # Keys stay sorted: "upsert" sorts after every other patch key.
    return (head[:-1] + ", " + body + "}" if len(head) > 2 else "{" + body + "}") + "\n"


class Journal:
    """Per-document journals in `root` (`$XDG_STATE_HOME/<app>/journal`)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, document_id: str) -> Path:
        safe = "".join(character for character in document_id if character.isalnum() or character in "-_")
        return self.root / f"{safe or 'document'}.log"

    def start(self, document_id: str, *, path: Path | None, base_revision: int,
              base_document: dict[str, Any] | None = None) -> None:
        """Begin a fresh journal whose patches apply to a known saved state.

        Called after every successful save (which truncates the journal) and
        when a document is first opened.
        """
        header = {"type": "header", "document_id": document_id,
                  "path": str(path) if path else None, "base_revision": base_revision}
        if base_document is not None:
            header["base_document"] = base_document
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(document_id)
        temporary = target.with_suffix(".log.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def append(self, document_id: str, patches: list[dict[str, Any]]) -> None:
        """Append patches and fsync once for the batch."""
        if not patches:
            return
        target = self.path_for(document_id)
        if not target.exists():
            raise FileNotFoundError(target)
        with target.open("a", encoding="utf-8") as stream:
            for patch in patches:
                stream.write(patch_line(patch))
            stream.flush()
            os.fsync(stream.fileno())

    def read(self, document_id: str) -> JournalRecord | None:
        return self._read(self.path_for(document_id))

    @staticmethod
    def _read(path: Path) -> JournalRecord | None:
        try:
            lines = path.read_text("utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        record: JournalRecord | None = None
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # A torn last line from a crash mid-append.
                break
            kind = entry.pop("type", None)
            if kind == "header" and record is None:
                record = JournalRecord(entry)
            elif kind == "patch" and record is not None:
                record.patches.append(entry)
        return record

    def pending(self) -> list[JournalRecord]:
        """Journals holding changes that never reached their file."""
        if not self.root.exists():
            return []
        found = []
        for path in sorted(self.root.glob("*.log")):
            record = self._read(path)
            if record is not None and record.patches:
                found.append(record)
        return found

    def discard(self, document_id: str) -> None:
        self.path_for(document_id).unlink(missing_ok=True)
