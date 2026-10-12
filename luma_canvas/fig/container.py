# SPDX-License-Identifier: MPL-2.0
"""The `.fig` container: a ZIP package or a bare `fig-kiwi` canvas.

Figma's "Save local copy" writes a ZIP holding `canvas.fig`, `meta.json`,
`thumbnail.png` and `images/<sha1>`. Older copies, clipboard data and some
tools write the canvas alone. The canvas starts with an eight byte magic
(`fig-kiwi` for design files, `fig-jam.` for FigJam, other `fig-…` values
for newer editors), a little-endian uint32 version, then length-prefixed
chunks: the compressed Kiwi schema, the compressed document message and,
in some versions, a preview image. Chunks are raw DEFLATE or Zstandard.

Every size is checked before anything is allocated or inflated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import struct
from typing import Callable
import zipfile
import zlib

from .errors import (CloudOnlyFigmaFile, DamagedFigmaFile, EmptyFigmaFile, EncryptedFigmaFile,
                     FigmaFileTooLarge, NotAFigmaFile, UnsupportedFigmaFile)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
EDITORS = {
    b"fig-kiwi": "design", b"fig-jam.": "figjam", b"fig-deck": "slides",
    b"fig-buzz": "buzz", b"fig-site": "sites", b"fig-make": "make",
}
EDITOR_NAMES = {"design": "Figma design", "figjam": "FigJam", "slides": "Figma Slides",
                "buzz": "Figma Buzz", "sites": "Figma Sites", "make": "Figma Make"}


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 4 << 30
    schema_bytes: int = 64 << 20
    message_bytes: int = 1536 << 20
    image_bytes: int = 256 << 20
    images_total_bytes: int = 4 << 30
    zip_members: int = 250_000
    chunks: int = 16


DEFAULT_LIMITS = Limits()


@dataclass
class FigArchive:
    """The parts of a `.fig` file, decompressed but not yet decoded."""

    editor: str
    version: int
    schema: bytes
    message: bytes
    meta: dict = field(default_factory=dict)
    thumbnail: bytes | None = None
    image_names: dict[str, str] = field(default_factory=dict)   # sha1 hex -> ZIP member
    source: Path | None = None
    _zip: zipfile.ZipFile | None = None
    limits: Limits = DEFAULT_LIMITS
    _images_read: int = 0

    def image(self, sha1: str) -> bytes | None:
        """An embedded image's bytes by its hash, or None when it is not in the file."""
        member = self.image_names.get(sha1.lower())
        if member is None or self._zip is None:
            return None
        info = self._zip.getinfo(member)
        if info.file_size > self.limits.image_bytes:
            raise FigmaFileTooLarge(f"image {sha1} is {info.file_size} bytes")
        if self._images_read + info.file_size > self.limits.images_total_bytes:
            raise FigmaFileTooLarge("the file's images are too large in total")
        try:
            data = self._zip.read(member)
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError, RuntimeError, NotImplementedError):
            return None
        self._images_read += len(data)
        return data

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None


def _decompress(data: bytes, limit: int, what: str) -> bytes:
    if data[:4] == ZSTD_MAGIC:
        return _zstd(data, limit, what)
    try:
        inflater = zlib.decompressobj(-15)
        out = inflater.decompress(data, limit + 1)
    except zlib.error:
        try:
            inflater = zlib.decompressobj(15)
            out = inflater.decompress(data, limit + 1)
        except zlib.error as error:
            raise DamagedFigmaFile(f"the {what} can’t be decompressed ({error})") from error
    if len(out) > limit or inflater.unconsumed_tail:
        raise FigmaFileTooLarge(f"the {what} is larger than {limit} bytes")
    return out


def _zstd(data: bytes, limit: int, what: str) -> bytes:
    try:
        from compression import zstd as stdlib_zstd  # Python 3.14
    except ImportError:
        stdlib_zstd = None
    if stdlib_zstd is not None:
        output = bytearray()
        remaining = data
        try:
            while remaining:
                decompressor = stdlib_zstd.ZstdDecompressor()
                output += decompressor.decompress(remaining, max_length=limit + 1 - len(output))
                if len(output) > limit:
                    raise FigmaFileTooLarge(f"the {what} is larger than {limit} bytes")
                if not decompressor.eof:
                    if decompressor.needs_input:
                        raise DamagedFigmaFile(f"the {what} is truncated")
                    raise FigmaFileTooLarge(f"the {what} is larger than {limit} bytes")
                remaining = decompressor.unused_data
        except stdlib_zstd.ZstdError as error:
            raise DamagedFigmaFile(f"the {what} can’t be decompressed ({error})") from error
        return bytes(output)
    try:
        import zstandard  # python3-zstandard
    except ImportError as error:
        raise UnsupportedFigmaFile(
            "Zstandard support is missing",
            reason="This Figma file needs Zstandard support, which isn’t installed.") from error
    try:
        reader = zstandard.ZstdDecompressor().stream_reader(data, read_across_frames=True)
        output = reader.read(limit + 1)
    except zstandard.ZstdError as error:
        raise DamagedFigmaFile(f"the {what} can’t be decompressed ({error})") from error
    if len(output) > limit:
        raise FigmaFileTooLarge(f"the {what} is larger than {limit} bytes")
    return output


def parse_canvas(data: bytes, limits: Limits = DEFAULT_LIMITS) -> tuple[str, int, bytes, bytes, bytes | None]:
    """(editor, version, schema, message, preview) from a bare canvas."""
    if len(data) < 12:
        raise DamagedFigmaFile("the canvas is shorter than its header")
    magic = bytes(data[:8])
    editor = EDITORS.get(magic)
    if editor is None:
        if magic.startswith(b"fig-"):
            editor = magic[4:].decode("ascii", "replace").strip(".")
        else:
            raise NotAFigmaFile("the canvas has no Figma header")
    version = struct.unpack_from("<I", data, 8)[0]
    chunks: list[bytes] = []
    offset = 12
    while offset + 4 <= len(data) and len(chunks) < limits.chunks:
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if length > len(data) - offset:
            raise DamagedFigmaFile("a chunk runs past the end of the file")
        chunks.append(bytes(data[offset:offset + length]))
        offset += length
    if len(chunks) < 2:
        raise DamagedFigmaFile("the canvas has no document")
    schema = _decompress(chunks[0], limits.schema_bytes, "schema")
    message = _decompress(chunks[1], limits.message_bytes, "document")
    preview = chunks[2] if len(chunks) > 2 and chunks[2][:8] == b"\x89PNG\r\n\x1a\n" else None
    return editor, version, schema, message, preview


def _looks_like_cloud_reference(head: bytes) -> bool:
    text = head.decode("utf-8", "ignore").lower()
    return "figma.com" in text or "figma://" in text


def open_fig(path: Path, limits: Limits = DEFAULT_LIMITS,
             check: Callable[[], None] | None = None) -> FigArchive:
    """Read a `.fig` file's container. Raises a `FigImportError` subclass."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise DamagedFigmaFile(str(error), reason="Canvas couldn’t read the file.") from error
    if size == 0:
        raise EmptyFigmaFile("zero bytes")
    if size > limits.file_bytes:
        raise FigmaFileTooLarge(f"{size} bytes")
    try:
        with path.open("rb") as stream:
            head = stream.read(4096)
    except OSError as error:
        raise DamagedFigmaFile(str(error), reason="Canvas couldn’t read the file.") from error

    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _open_zip(path, limits, check)
    if head.startswith(b"fig-"):
        try:
            data = path.read_bytes()
        except OSError as error:
            raise DamagedFigmaFile(str(error), reason="Canvas couldn’t read the file.") from error
        if check:
            check()
        editor, version, schema, message, preview = parse_canvas(data, limits)
        return FigArchive(editor, version, schema, message, thumbnail=preview, source=path, limits=limits)
    if _looks_like_cloud_reference(head):
        raise CloudOnlyFigmaFile("a link to a file in Figma")
    if head.strip(b" \t\r\n\x00") == b"":
        raise EmptyFigmaFile("only blank bytes")
    raise NotAFigmaFile("unknown header")


def _open_zip(path: Path, limits: Limits, check: Callable[[], None] | None) -> FigArchive:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        raise DamagedFigmaFile(f"not a readable ZIP ({error})") from error
    try:
        infos = archive.infolist()
        if len(infos) > limits.zip_members:
            raise FigmaFileTooLarge(f"{len(infos)} members")
        by_name = {info.filename: info for info in infos}
        canvas = by_name.get("canvas.fig") or next(
            (info for info in infos if info.filename.endswith("/canvas.fig")), None)
        if canvas is None:
            raise NotAFigmaFile("the package has no canvas.fig")
        if canvas.flag_bits & 0x1:
            raise EncryptedFigmaFile("canvas.fig is encrypted")
        if canvas.file_size > limits.message_bytes:
            raise FigmaFileTooLarge(f"canvas.fig is {canvas.file_size} bytes")
        prefix = canvas.filename[: -len("canvas.fig")]
        try:
            data = archive.read(canvas)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as error:
            raise DamagedFigmaFile(f"canvas.fig can’t be read ({error})") from error
        except RuntimeError as error:   # encrypted members raise RuntimeError
            raise EncryptedFigmaFile(str(error)) from error
        if check:
            check()
        editor, version, schema, message, preview = parse_canvas(data, limits)
        meta: dict = {}
        info = by_name.get(prefix + "meta.json")
        if info is not None and info.file_size < (1 << 20):
            try:
                value = json.loads(archive.read(info).decode("utf-8", "replace"))
                meta = value if isinstance(value, dict) else {}
            except (ValueError, zipfile.BadZipFile, zlib.error, RuntimeError, EOFError):
                meta = {}
        thumbnail = preview
        info = by_name.get(prefix + "thumbnail.png")
        if info is not None and info.file_size < (16 << 20):
            try:
                thumbnail = archive.read(info)
            except (zipfile.BadZipFile, zlib.error, RuntimeError, EOFError):
                pass
        image_names = {}
        image_prefix = prefix + "images/"
        for info in infos:
            name = info.filename
            if name.startswith(image_prefix) and not name.endswith("/"):
                image_names[os.path.basename(name).lower()] = name
    except BaseException:
        archive.close()
        raise
    return FigArchive(editor, version, schema, message, meta=meta, thumbnail=thumbnail,
                      image_names=image_names, source=path, _zip=archive, limits=limits)
