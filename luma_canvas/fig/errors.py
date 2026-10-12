# SPDX-License-Identifier: MPL-2.0
"""Why a Figma file could not be imported, in sentences a person can act on."""

from __future__ import annotations


class FigImportError(ValueError):
    """An import that cannot continue. `reason` is shown to the person."""

    reason = "Canvas couldn’t read this Figma file."

    def __init__(self, detail: str = "", reason: str | None = None) -> None:
        super().__init__(detail or reason or self.reason)
        if reason is not None:
            self.reason = reason


class NotAFigmaFile(FigImportError):
    reason = "It isn’t a Figma file."


class CloudOnlyFigmaFile(FigImportError):
    reason = ("This file only exists in Figma. In Figma, use File › Save local copy… "
              "and open the file it saves.")


class EmptyFigmaFile(FigImportError):
    reason = ("The file is empty. If it is still downloading or syncing, wait for it to finish. "
              "If it only exists in Figma, use File › Save local copy… in Figma.")


class EncryptedFigmaFile(FigImportError):
    reason = "The file is encrypted. Save an unencrypted local copy from Figma and open that."


class DamagedFigmaFile(FigImportError):
    reason = "The Figma file is damaged."


class UnsupportedFigmaFile(FigImportError):
    reason = "This kind of Figma file isn’t supported."


class FigmaFileTooLarge(FigImportError):
    reason = "The Figma file is larger than Canvas can safely open."


class ImportCancelled(FigImportError):
    reason = "The import was cancelled."
