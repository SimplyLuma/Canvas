# SPDX-License-Identifier: MPL-2.0
"""Import a Figma file in a separate process.

Canvas's window is Python, so a long import on a thread competes with it for
the interpreter lock and the window stutters. In a child process it cannot:
the window keeps drawing at full rate, Cancel ends the child at once, a
hostile file can at worst exhaust the child's own memory limit, and nothing
it does can take the window down with it.

The child writes length-prefixed frames to stdout. Every frame is a pickle
of plain data only (dicts, lists, strings, numbers, bytes); the parent reads
them with an unpickler that refuses every class, so the stream can carry no
code. The document travels in batches the parent turns back into layers a
few thousand at a time.

Run as `python3 -m luma_canvas.fig.worker PATH`.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import pickle
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

from . import errors
from .report import ImportReport

FRAME = struct.Struct("<Q")
MAX_FRAME = 1 << 31
BATCH = 2000
MEMORY_LIMIT = 8 << 30
PROGRESS_INTERVAL = 0.05


# ── the child ────────────────────────────────────────────────────────────

def _send(stream, value: Any) -> None:
    payload = pickle.dumps(value, pickle.HIGHEST_PROTOCOL)
    stream.write(FRAME.pack(len(payload)))
    stream.write(payload)


def child(path: str, out=None) -> int:
    out = out or sys.stdout.buffer
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = MEMORY_LIMIT if hard == resource.RLIM_INFINITY else min(MEMORY_LIMIT, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ImportError, ValueError, OSError):
        pass
    from . import import_fig

    last = [0.0]

    def progress(fraction: float, stage: str) -> None:
        now = time.monotonic()
        if now - last[0] >= PROGRESS_INTERVAL or fraction >= 1.0:
            last[0] = now
            _send(out, ("progress", float(fraction), stage))
            out.flush()

    try:
        result = import_fig(Path(path), progress=progress)
    except errors.FigImportError as error:
        _send(out, ("error", type(error).__name__, error.reason, str(error)))
        out.flush()
        return 1
    except MemoryError:
        _send(out, ("error", "FigmaFileTooLarge", errors.FigmaFileTooLarge.reason, "out of memory"))
        out.flush()
        return 1
    document = result.document
    head = document.to_dict()
    pages = head.pop("pages")
    objects = head.pop("objects")
    head["pages"] = [{key: value for key, value in page.items() if key != "objects"} for page in pages]
    _send(out, ("document", head, result.editor, result.version, result.seconds))
    shown = document.current_page_id
    for index, page in enumerate(pages):
        layers = objects if page["id"] == shown else page["objects"]
        for start in range(0, len(layers), BATCH):
            _send(out, ("layers", index, layers[start:start + BATCH]))
    for asset_id, data in document.embedded_assets.items():
        _send(out, ("asset", asset_id, bytes(data)))
    _send(out, ("done", result.report.to_dict()))
    out.flush()
    return 0


# ── the parent ───────────────────────────────────────────────────────────

class _PlainUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):  # noqa: D401 - pickle API
        raise pickle.UnpicklingError(f"refusing {module}.{name}")


def _load(payload: bytes) -> Any:
    return _PlainUnpickler(io.BytesIO(payload)).load()


def _read_exactly(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, 1 << 20))
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _source_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def import_in_process(path: Path, *, progress: Callable[[float, str], None] | None = None,
                      cancel: threading.Event | None = None, python: str | None = None):
    """Import in a child process; returns a `FigImportResult` like `import_fig`."""
    from . import FigImportResult
    from ..model import Asset, Document, LayoutObject, Page, PrototypeConnection, Style, WorkspaceState
    from ..paint import normalized_style

    started = time.monotonic()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [_source_root(), environment.get("PYTHONPATH")]))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # The child's messages go to a file, not a pipe nobody drains while
    # frames are read, which could fill and stall it.
    log = tempfile.TemporaryFile()
    process = subprocess.Popen(
        [python or sys.executable, "-m", "luma_canvas.fig.worker", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, env=environment)
    stop = threading.Event()

    def watch_cancel() -> None:
        while not stop.wait(0.05):
            if cancel is not None and cancel.is_set():
                process.kill()
                return
    watcher = threading.Thread(target=watch_cancel, name="canvas-fig-cancel", daemon=True)
    watcher.start()
    document = None
    pages: list[Page] = []
    layers: list[list[Any]] = []
    try:
        stream = process.stdout
        while True:
            try:
                size = FRAME.unpack(_read_exactly(stream, FRAME.size))[0]
                if size > MAX_FRAME:
                    raise errors.DamagedFigmaFile("an import frame is too large")
                message = _load(_read_exactly(stream, size))
            except EOFError:
                if cancel is not None and cancel.is_set():
                    raise errors.ImportCancelled("cancelled") from None
                process.wait()
                log.seek(0)
                detail = log.read()[-4000:].decode("utf-8", "replace")
                if process.returncode and process.returncode < 0 or "MemoryError" in detail:
                    raise errors.FigmaFileTooLarge(detail or "the import process was stopped") from None
                raise errors.FigImportError(detail or "the import process ended early") from None
            kind = message[0]
            if kind == "progress":
                if progress is not None:
                    progress(float(message[1]), str(message[2]))
            elif kind == "error":
                error_class = getattr(errors, str(message[1]), errors.FigImportError)
                if not (isinstance(error_class, type) and issubclass(error_class, errors.FigImportError)):
                    error_class = errors.FigImportError
                raise error_class(str(message[3]), reason=str(message[2]))
            elif kind == "document":
                head, editor, version, seconds = message[1], message[2], message[3], message[4]
                page_values = head.pop("pages")
                data = dict(head)
                data["objects"] = []
                data["styles"] = [Style(**item) for item in data.get("styles", [])]
                data["assets"] = [Asset(**item) for item in data.get("assets", [])]
                data["connections"] = [PrototypeConnection(**item) for item in data.get("connections", [])]
                data["workspaces"] = {key: WorkspaceState(**state) for key, state in data.get("workspaces", {}).items()}
                data["current_style"] = normalized_style(data.get("current_style"))
                pages = [Page(**{**value, "objects": []}) for value in page_values]
                data["pages"] = pages
                layers = [[] for _ in pages]
                document = Document(**data)
            elif kind == "layers" and document is not None:
                index = int(message[1])
                if 0 <= index < len(pages):
                    if pages[index].id == document.current_page_id:
                        layers[index].extend(LayoutObject(**item) for item in message[2])
                    else:
                        layers[index].extend(message[2])
            elif kind == "asset" and document is not None:
                document.embedded_assets[str(message[1])] = bytes(message[2])
            elif kind == "done" and document is not None:
                for index, page in enumerate(pages):
                    if page.id == document.current_page_id:
                        document.objects = layers[index]
                    else:
                        page.objects = layers[index]
                report = ImportReport.from_dict(message[1])
                process.wait()
                return FigImportResult(document, report, time.monotonic() - started, editor, version)
            else:
                raise errors.DamagedFigmaFile(f"unexpected import frame {kind!r}")
    except (pickle.UnpicklingError, TypeError, ValueError, struct.error) as error:
        if isinstance(error, errors.FigImportError):
            raise
        raise errors.FigImportError(f"the import process sent damaged data ({error})") from error
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        log.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 -m luma_canvas.fig.worker PATH", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(child(sys.argv[1]))
