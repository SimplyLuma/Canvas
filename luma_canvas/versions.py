# SPDX-License-Identifier: MPL-2.0
"""Local version history, so no save, revert or recovery can destroy work.

Versions live in `$XDG_DATA_HOME/org.projectluma.Canvas/versions/<document id>/`,
keyed by the document's id so they follow it through renames and moves. Each
version is the document's JSON, compressed, plus a thumbnail when one could be
drawn. Embedded assets are stored once per content hash and shared by every
version that uses them.

Retention (CANVAS-SPEC 13.3): every automatic version for 24 hours, then one
per hour for 7 days, then one per day for 30 days, then one per week. Named
versions are kept until deleted; "Last opened" versions of the last 10 sessions
are kept. Past 1 GB per document the oldest automatic versions go first.

No GTK here: the window calls this from a worker thread.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

HOUR = 3600.0
DAY = 24 * HOUR
WEEK = 7 * DAY
CAP_BYTES = 1024 ** 3

# What made a version. Automatic and restore points are subject to retention.
AUTOMATIC = "automatic"          # every ten minutes of editing
RESTORE_POINT = "restore-point"  # before revert, restore, recovery, reload
OPENED = "opened"                # "Last opened": the state a session began with
NAMED = "named"                  # Ctrl+S, or any version given a name


@dataclass
class Version:
    id: str
    created: float
    kind: str
    name: str | None = None
    revision: int = 0
    size: int = 0
    assets: dict[str, str] = field(default_factory=dict)   # asset id -> sha256
    thumbnail: bool = False

    @property
    def title(self) -> str:
        if self.name:
            return self.name
        return {OPENED: "Last opened", RESTORE_POINT: "Before restoring",
                NAMED: "Version"}.get(self.kind, "Autosaved")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def retained(versions: Iterable[Version], now: float, *, opened_sessions: int = 10) -> set[str]:
    """Which versions the retention policy keeps. Pure, so it can be tested
    with an injected clock."""
    versions = sorted(versions, key=lambda version: version.created, reverse=True)
    keep: set[str] = set()
    opened = [version for version in versions if version.kind == OPENED]
    keep.update(version.id for version in opened[:opened_sessions])
    buckets: set[tuple[str, int]] = set()
    for version in versions:
        if version.kind == NAMED or version.name:
            keep.add(version.id)
            continue
        if version.kind == OPENED:
            continue
        age = now - version.created
        if age <= DAY:
            keep.add(version.id)
            continue
        if age <= WEEK:
            bucket = ("hour", int(version.created // HOUR))
        elif age <= 30 * DAY:
            bucket = ("day", int(version.created // DAY))
        else:
            bucket = ("week", int(version.created // WEEK))
        # Newest first, so the version kept for each bucket is its latest.
        if bucket not in buckets:
            buckets.add(bucket)
            keep.add(version.id)
    return keep


class VersionStore:
    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time,
                 cap_bytes: int = CAP_BYTES) -> None:
        self.root = root
        self.clock = clock
        self.cap_bytes = cap_bytes

    # ── storage ─────────────────────────────────────────────────────────

    def folder(self, document_id: str) -> Path:
        safe = "".join(character for character in document_id if character.isalnum() or character in "-_")
        return self.root / (safe or "document")

    def _index_path(self, document_id: str) -> Path:
        return self.folder(document_id) / "index.json"

    def list(self, document_id: str) -> list[Version]:
        """Newest first."""
        try:
            raw = json.loads(self._index_path(document_id).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        versions = []
        for entry in raw.get("versions", []):
            try:
                versions.append(Version(**entry))
            except TypeError:
                continue
        return sorted(versions, key=lambda version: version.created, reverse=True)

    def _write_index(self, document_id: str, versions: list[Version]) -> None:
        payload = {"document_id": document_id,
                   "versions": [asdict(version) for version in sorted(versions, key=lambda v: v.created)]}
        _atomic_write(self._index_path(document_id),
                      (json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8"))

    def create(self, snapshot: dict[str, Any], *, kind: str = AUTOMATIC, name: str | None = None,
               embedded_assets: dict[str, bytes] | None = None,
               thumbnail: bytes | None = None) -> Version:
        document_id = str(snapshot["id"])
        folder = self.folder(document_id)
        version = Version(id=uuid.uuid4().hex[:16], created=self.clock(), kind=kind, name=name,
                          revision=int(snapshot.get("revision", 0)))
        data = gzip.compress(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        _atomic_write(folder / f"{version.id}.json.gz", data)
        version.size = len(data)
        for asset_id, content in (embedded_assets or {}).items():
            digest = hashlib.sha256(content).hexdigest()
            target = folder / "assets" / digest
            if not target.exists():
                _atomic_write(target, content)
            version.assets[asset_id] = digest
        if thumbnail:
            _atomic_write(folder / f"{version.id}.png", thumbnail)
            version.thumbnail = True
        versions = self.list(document_id)
        if kind == NAMED and not name:
            version.name = f"Version {sum(1 for item in versions if item.kind == NAMED) + 1}"
        versions.append(version)
        self._write_index(document_id, versions)
        self.prune(document_id)
        return version

    def load(self, document_id: str, version_id: str) -> tuple[dict[str, Any], dict[str, bytes]]:
        folder = self.folder(document_id)
        version = next((item for item in self.list(document_id) if item.id == version_id), None)
        if version is None:
            raise KeyError(version_id)
        snapshot = json.loads(gzip.decompress((folder / f"{version_id}.json.gz").read_bytes()))
        assets = {}
        for asset_id, digest in version.assets.items():
            try:
                assets[asset_id] = (folder / "assets" / digest).read_bytes()
            except OSError:
                continue
        return snapshot, assets

    def thumbnail_path(self, document_id: str, version_id: str) -> Path | None:
        path = self.folder(document_id) / f"{version_id}.png"
        return path if path.exists() else None

    def rename(self, document_id: str, version_id: str, name: str) -> None:
        versions = self.list(document_id)
        for version in versions:
            if version.id == version_id:
                version.name = name.strip() or None
        self._write_index(document_id, versions)

    def delete(self, document_id: str, version_id: str) -> None:
        versions = [version for version in self.list(document_id) if version.id != version_id]
        self._remove_files(document_id, [version_id])
        self._write_index(document_id, versions)
        self._collect_assets(document_id, versions)

    def latest(self, document_id: str, kind: str | None = None) -> Version | None:
        return next((version for version in self.list(document_id)
                     if kind is None or version.kind == kind), None)

    # ── retention ───────────────────────────────────────────────────────

    def prune(self, document_id: str) -> list[str]:
        versions = self.list(document_id)
        keep = retained(versions, self.clock())
        doomed = [version.id for version in versions if version.id not in keep]
        survivors = [version for version in versions if version.id in keep]
        # Over the cap: the oldest automatic versions go first.
        total = sum(version.size for version in survivors)
        for version in sorted(survivors, key=lambda item: item.created):
            if total <= self.cap_bytes:
                break
            if version.kind in {AUTOMATIC, RESTORE_POINT} and not version.name:
                doomed.append(version.id)
                total -= version.size
        if doomed:
            remaining = [version for version in versions if version.id not in set(doomed)]
            self._remove_files(document_id, doomed)
            self._write_index(document_id, remaining)
            self._collect_assets(document_id, remaining)
        return doomed

    def _remove_files(self, document_id: str, version_ids: Iterable[str]) -> None:
        folder = self.folder(document_id)
        for version_id in version_ids:
            (folder / f"{version_id}.json.gz").unlink(missing_ok=True)
            (folder / f"{version_id}.png").unlink(missing_ok=True)

    def _collect_assets(self, document_id: str, versions: list[Version]) -> None:
        used = {digest for version in versions for digest in version.assets.values()}
        assets = self.folder(document_id) / "assets"
        if not assets.exists():
            return
        for path in assets.iterdir():
            if path.name not in used:
                path.unlink(missing_ok=True)
