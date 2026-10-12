# SPDX-License-Identifier: MPL-2.0
"""Import Figma `.fig` files as Canvas documents.

    result = import_fig(path, progress=callback, cancel=event)
    result.document   # a Canvas Document, never saved over the .fig
    result.report     # ImportReport: what was approximated or left out

Nothing here imports GTK: the importer runs on a worker thread, and its
tests run anywhere Python does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Callable

from .container import DEFAULT_LIMITS, Limits, open_fig
from .convert import Converter
from .document import decode
from .errors import (CloudOnlyFigmaFile, DamagedFigmaFile, EmptyFigmaFile, EncryptedFigmaFile, FigImportError,
                     FigmaFileTooLarge, ImportCancelled, NotAFigmaFile, UnsupportedFigmaFile)
from .report import ImportReport

__all__ = [
    "FigImportResult", "import_fig", "import_fig_document", "FigImportError", "NotAFigmaFile",
    "CloudOnlyFigmaFile", "EmptyFigmaFile", "EncryptedFigmaFile", "DamagedFigmaFile",
    "UnsupportedFigmaFile", "FigmaFileTooLarge", "ImportCancelled", "ImportReport", "Limits",
]

# Stages and the share of the progress bar each takes.
STAGES = (("read", 0.10), ("decode", 0.45), ("convert", 0.45))


@dataclass
class FigImportResult:
    document: object
    report: ImportReport
    seconds: float
    editor: str
    version: int


def import_fig(path: Path | str, *, progress: Callable[[float, str], None] | None = None,
               cancel: threading.Event | Callable[[], bool] | None = None,
               limits: Limits = DEFAULT_LIMITS, name: str | None = None) -> FigImportResult:
    """Read a `.fig` file into a new Canvas document (safe on a worker thread)."""
    started = time.monotonic()
    path = Path(path)
    report = ImportReport()

    def cancelled() -> bool:
        if cancel is None:
            return False
        return cancel.is_set() if isinstance(cancel, threading.Event) else bool(cancel())

    def check() -> None:
        if cancelled():
            raise ImportCancelled("cancelled")

    def stage(index: int) -> Callable[[float], None]:
        base = sum(share for _name, share in STAGES[:index])
        share = STAGES[index][1]
        label = STAGES[index][0]

        def update(fraction: float) -> None:
            check()
            if progress is not None:
                progress(min(1.0, base + share * max(0.0, min(1.0, fraction))), label)
        return update

    stage(0)(0.0)
    archive = open_fig(path, limits, check)
    fig = None
    try:
        stage(0)(1.0)
        fig = decode(archive, progress=stage(1))
        stage(1)(1.0)
        converter = Converter(fig, report, name=name or path.stem, check=check, progress=stage(2))
        document = converter.run()
        stage(2)(1.0)
    except RecursionError as error:
        raise DamagedFigmaFile("the document is nested too deeply") from error
    finally:
        archive.close()
        if fig is not None:
            fig.release()
    report.stats["seconds"] = round(time.monotonic() - started, 3)
    report.stats["editor"] = archive.editor
    report.stats["version"] = archive.version
    return FigImportResult(document, report, time.monotonic() - started, archive.editor, archive.version)


def import_fig_document(path: Path) -> object:
    """The importer registry's reader: the document alone."""
    return import_fig(path).document
