# SPDX-License-Identifier: MPL-2.0
"""What an embedded image is: its media type and pixel size, from its header.

Figma keeps images by the SHA-1 of their bytes with no file name extension.
Only the first bytes are read, so a damaged or hostile image costs nothing
here; decoding happens later in GDK, which has its own limits.
"""

from __future__ import annotations

import struct


def sniff(data: bytes) -> tuple[str, int, int]:
    """(media type, width, height); width and height are 0 when unknown."""
    head = bytes(data[:64])
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        return "image/png", width, height
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg", *_jpeg_size(data)
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        width, height = struct.unpack("<HH", head[6:10])
        return "image/gif", width, height
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", *_webp_size(head)
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif", 0, 0
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic", 0, 0
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff", 0, 0
    if head[:2] == b"BM" and len(head) >= 26:
        width, height = struct.unpack("<ii", head[18:26])
        return "image/bmp", abs(width), abs(height)
    stripped = head.lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return "image/svg+xml", 0, 0
    return "application/octet-stream", 0, 0


EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
    "image/avif": ".avif", "image/heic": ".heic", "image/tiff": ".tif", "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}


def _jpeg_size(data: bytes) -> tuple[int, int]:
    offset = 2
    length = min(len(data), 1 << 20)
    while offset + 9 < length:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        segment = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
            return width, height
        if segment < 2:
            break
        offset += 2 + segment
    return 0, 0


def _webp_size(head: bytes) -> tuple[int, int]:
    chunk = head[12:16]
    try:
        if chunk == b"VP8 " and len(head) >= 30:
            width, height = struct.unpack("<HH", head[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L" and len(head) >= 25:
            bits = struct.unpack("<I", head[21:25])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X" and len(head) >= 30:
            width = int.from_bytes(head[24:27], "little") + 1
            height = int.from_bytes(head[27:30], "little") + 1
            return width, height
    except struct.error:
        pass
    return 0, 0
