# SPDX-License-Identifier: Apache-2.0
"""Atomic, readable, recoverable Canvas package storage."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import zipfile

from .model import Document, InvalidDocument, SCHEMA_VERSION, UnsupportedDocumentVersion


MIME_TYPE = "application/vnd.projectluma.canvas+zip"
# The name the format carried while the application was called Layouts.
# Documents written then are still opened; nothing a person saved stops
# working because the product was renamed.
LEGACY_MIME_TYPES = ("application/vnd.projectluma.layouts+zip",)
DOCUMENT_SUFFIX = ".canvas"
LEGACY_DOCUMENT_SUFFIXES = (".layouts",)


APP_ID = ("org.projectluma.Canvas.Hotfix"
          if os.environ.get("LUMA_CANVAS_HOTFIX") == "1" else "org.projectluma.Canvas")


class PackageError(ValueError):
    """A package could not be read. `reason` is a sentence a person can read."""

    reason = "The file is damaged."


class DocumentDamaged(PackageError):
    reason = "The file is damaged."


class DocumentTooNew(PackageError):
    reason = "It was made by a newer version of Canvas."


class NotACanvasDocument(PackageError):
    reason = "It isn’t a Canvas document."


def _xdg(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


def state_home() -> Path:
    """Canvas's own state: the crash journal and recovery packages."""
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_ID


def data_home() -> Path:
    """Canvas's own data: the version history."""
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_ID


def documents_folder() -> Path:
    """`$(xdg-user-dir DOCUMENTS)/Canvas`, where untitled documents live.

    Reads user-dirs.dirs directly so it works without GLib (tests, workers).
    """
    # Flatpak's data directory survives sandbox exits; its private home does not.
    if os.environ.get("FLATPAK_ID") == APP_ID:
        return data_home() / "Documents"
    config = _xdg("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"
    documents = Path.home() / "Documents"
    try:
        for line in config.read_text("utf-8").splitlines():
            line = line.strip()
            if line.startswith("XDG_DOCUMENTS_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                if value and Path(value).is_absolute() and Path(value) != Path.home():
                    documents = Path(value)
                break
    except OSError:
        pass
    return documents / "Canvas"


def next_free_path(folder: Path, stem: str = "Untitled", suffix: str = ".canvas") -> Path:
    """`Untitled.canvas`, then `Untitled 2.canvas`, and so on."""
    candidate = folder / f"{stem}{suffix}"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} {number}{suffix}"
        number += 1
    return candidate


def write_package(snapshot: dict, embedded_assets: dict[str, bytes], destination: Path) -> Path:
    """Write a package from an immutable snapshot, atomically.

    Same-directory temporary file, fsync, rename, fsync of the directory: a
    crash at any point leaves either the old file or the new one, never a
    half-written package under the document's name. Safe on a worker thread.
    """
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", _canonical_json({
                "format": "Luma Canvas",
                "mime_type": MIME_TYPE,
                "schema_version": snapshot.get("schema_version", SCHEMA_VERSION),
                "document_id": snapshot.get("id"),
                "revision": snapshot.get("revision", 0),
                "created_by": "Canvas 0.1.0",
            }))
            archive.writestr("document.json", _canonical_json(snapshot))
            for asset in snapshot.get("assets", []):
                name = asset.get("embedded_name")
                # Bytes are keyed by asset id; pastes from older builds keyed
                # them by embedded name, and those must not be dropped.
                data = embedded_assets.get(asset.get("id")) if name else None
                if data is None and name:
                    data = embedded_assets.get(name)
                if data is not None:
                    archive.writestr(f"assets/{asset['id']}/{Path(name).name}", data)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _canonical_json(value: dict, *, chunk: int = 50, pause: float = 0.001) -> bytes:
    """`json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)`, byte
    for byte, with a document's layers encoded a slice at a time.

    Packages are written on the save worker while the window edits. One
    encoder call over twenty thousand layers holds the interpreter lock long
    enough to freeze the window; between slices the main thread runs (a real
    pause: sleep(0) does not make the other thread run first)."""
    import time
    objects = value.get("objects") if isinstance(value, dict) else None
    if not isinstance(objects, list) or len(objects) <= chunk:
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def nested(item: Any, depth: int) -> str:
        text = json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True)
        return text.replace("\n", "\n" + "  " * depth)

    lines = []
    for key in sorted(value):
        if key == "objects":
            parts: list[str] = []
            for start in range(0, len(objects), chunk):
                parts.extend("    " + nested(item, 2) for item in objects[start:start + chunk])
                if pause:
                    time.sleep(pause)
            lines.append('  "objects": [\n' + ",\n".join(parts) + "\n  ]")
        else:
            lines.append("  " + json.dumps(key, ensure_ascii=False) + ": " + nested(value[key], 1))
    return ("{\n" + ",\n".join(lines) + "\n}\n").encode("utf-8")


def _read_unchanged(data: dict) -> bool:
    """Whether reading `data` leaves the document exactly as the file has it."""
    if int(data.get("schema_version", 0)) != SCHEMA_VERSION:
        return False
    for item in data.get("objects", ()):
        if "fills" not in item or "strokes" not in item:
            return False
        if item.get("kind") in {"group", "layout"} and (item.get("fill") or item.get("stroke")
                                                        or item.get("fills") or item.get("strokes")
                                                        or item.get("effects") or item.get("fill_gradient")
                                                        or item.get("stroke_gradient")):
            return False
    return True


class DocumentStore:
    def __init__(self, recovery_root: Path | None = None) -> None:
        self.recovery_root = recovery_root or state_home() / "recovery"

    @staticmethod
    def save(document: Document, destination: Path) -> Path:
        document.validate()
        return write_package(document.to_dict(), dict(document.embedded_assets), destination)

    @classmethod
    def load(cls, source: Path) -> Document:
        return cls.load_with_snapshot(source)[0]

    @staticmethod
    def load_with_snapshot(source: Path) -> tuple[Document, dict | None]:
        """The document, and the dictionary it was read from when that is
        exactly what the document holds.

        A session can keep that dictionary as what the file knows instead of
        copying the whole document again, which costs a third of a second on
        the main thread for a document of tens of thousands of layers. It is
        `None` when reading migrated the document (an older schema, or paint
        that moved off a group), because then the two no longer match.
        """
        snapshot: dict | None = None
        try:
            with zipfile.ZipFile(source, "r") as archive:
                names = set(archive.namelist())
                if not {"manifest.json", "document.json"}.issubset(names):
                    raise DocumentDamaged("Canvas package is missing required metadata")
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("mime_type") not in (MIME_TYPE, *LEGACY_MIME_TYPES):
                    raise NotACanvasDocument("not a Canvas document")
                data = json.loads(archive.read("document.json"))
                # The dictionary was read here and is not changed again.
                document = Document.from_dict(data, copy_input=False)
                if _read_unchanged(data):
                    snapshot = data
                for asset in document.assets:
                    if not asset.embedded_name:
                        continue
                    member = f"assets/{asset.id}/{Path(asset.embedded_name).name}"
                    if member in names:
                        document.embedded_assets[asset.id] = archive.read(member)
                    else:
                        asset.missing = True
        except UnsupportedDocumentVersion as error:
            raise DocumentTooNew(str(error)) from error
        except PackageError:
            raise
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, InvalidDocument,
                KeyError, TypeError, UnicodeDecodeError, EOFError) as error:
            raise DocumentDamaged(f"cannot open Canvas package: {error}") from error
        try:
            document.validate()
        except InvalidDocument as error:
            raise DocumentDamaged(str(error)) from error
        return document, snapshot

    def autosave(self, document: Document, source: Path | None) -> Path:
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        recovery = self.recovery_root / f"{document.id}.canvas-recovery"
        self.save(document, recovery)
        sidecar = recovery.with_suffix(recovery.suffix + ".json")
        sidecar.write_bytes(_canonical_json({
            "document_id": document.id,
            "name": document.name,
            "source": str(source) if source else None,
            "saved_at": datetime.now(UTC).isoformat(),
            "revision": document.revision,
        }))
        return recovery

    def recoverable(self) -> list[tuple[Path, Document, dict]]:
        results: list[tuple[Path, Document, dict]] = []
        # Recovery packages written before Canvas used its application ID for
        # its state folder are still found.
        roots = [self.recovery_root]
        legacy = Path.home() / ".local/state/luma-canvas/recovery"
        if legacy != self.recovery_root and self.recovery_root == state_home() / "recovery":
            roots.append(legacy)
        paths = sorted(path for root in roots if root.exists() for path in root.glob("*.canvas-recovery"))
        for path in paths:
            try:
                document = self.load(path)
                metadata_path = path.with_suffix(path.suffix + ".json")
                metadata = json.loads(metadata_path.read_text("utf-8")) if metadata_path.exists() else {}
            except (PackageError, OSError, json.JSONDecodeError):
                continue
            results.append((path, document, metadata))
        return results

    def discard_recovery(self, document_id: str) -> None:
        path = self.recovery_root / f"{document_id}.canvas-recovery"
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".json").unlink(missing_ok=True)

    def snapshot(self, document: Document, versions_root: Path) -> Path:
        versions_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.save(document, versions_root / f"{document.id}-{stamp}-r{document.revision}{DOCUMENT_SUFFIX}")
