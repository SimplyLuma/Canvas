# SPDX-License-Identifier: Apache-2.0
"""Bounded structure-preserving interchange importers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

from .model import Document, LayoutObject, new_id


def _number(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)", value)
    return float(match.group(1)) if match else default


def _style(element: ET.Element, key: str, default: str | None = None) -> str | None:
    if key in element.attrib:
        return element.attrib[key]
    inline = dict(
        part.split(":", 1) for part in element.attrib.get("style", "").split(";") if ":" in part
    )
    return inline.get(key, default)


def import_svg(source: Path) -> Document:
    """Import common SVG elements without flattening them to pixels.

    Unsupported attributes remain in each object's metadata so a future
    importer can migrate them instead of silently losing source information.
    """
    try:
        root = ET.parse(source).getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"cannot import SVG: {error}") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("the selected XML file is not SVG")
    viewbox = [_number(value) for value in root.attrib.get("viewBox", "").replace(",", " ").split()]
    width = _number(root.attrib.get("width"), viewbox[2] if len(viewbox) == 4 else 800)
    height = _number(root.attrib.get("height"), viewbox[3] if len(viewbox) == 4 else 600)
    document = Document.empty(source.stem)
    frame = LayoutObject(new_id("frame"), "frame", source.stem, x=40, y=40, width=max(1, width), height=max(1, height), fill=None)
    document.objects.append(frame)

    def walk(element: ET.Element, parent_id: str) -> None:
        for child in element:
            tag = child.tag.rsplit("}", 1)[-1]
            fill = _style(child, "fill", "#000000")
            stroke = _style(child, "stroke")
            stroke_width = _number(_style(child, "stroke-width"), 0)
            opacity = max(0.0, min(1.0, _number(_style(child, "opacity"), 1)))
            name = child.attrib.get("id") or tag.title()
            item: LayoutObject | None = None
            if tag == "g":
                item = LayoutObject(new_id("group"), "group", name, parent_id, width=0, height=0, fill=None, metadata={"svg_attributes": dict(child.attrib)})
            elif tag == "rect":
                item = LayoutObject(new_id("rectangle"), "rectangle", name, parent_id, 40 + _number(child.attrib.get("x")), 40 + _number(child.attrib.get("y")), max(1, _number(child.attrib.get("width"), 1)), max(1, _number(child.attrib.get("height"), 1)), fill=fill, stroke=stroke, stroke_width=stroke_width, radius=_number(child.attrib.get("rx")), opacity=opacity)
            elif tag in {"circle", "ellipse"}:
                rx = _number(child.attrib.get("r"), _number(child.attrib.get("rx"), 1)); ry = _number(child.attrib.get("r"), _number(child.attrib.get("ry"), rx))
                cx, cy = _number(child.attrib.get("cx")), _number(child.attrib.get("cy"))
                item = LayoutObject(new_id("ellipse"), "ellipse", name, parent_id, 40 + cx - rx, 40 + cy - ry, max(1, rx * 2), max(1, ry * 2), fill=fill, stroke=stroke, stroke_width=stroke_width, opacity=opacity)
            elif tag == "line":
                x1, y1, x2, y2 = (_number(child.attrib.get(key)) for key in ("x1", "y1", "x2", "y2"))
                item = LayoutObject(new_id("line"), "line", name, parent_id, 40 + min(x1, x2), 40 + min(y1, y2), abs(x2 - x1), abs(y2 - y1), fill=None, stroke=stroke or "#000000", stroke_width=max(1, stroke_width), opacity=opacity, metadata={"direction": [x1, y1, x2, y2]})
            elif tag in {"path", "polyline", "polygon"}:
                raw = child.attrib.get("d") or child.attrib.get("points", "")
                values = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", raw)]
                pairs = list(zip(values[::2], values[1::2]))[:500]
                if len(pairs) < 2: pairs = [(0.0, 0.0), (1.0, 1.0)]
                left, top = min(x for x, _ in pairs), min(y for _, y in pairs)
                right, bottom = max(x for x, _ in pairs), max(y for _, y in pairs)
                nodes = [{"x": x - left, "y": y - top, "type": "corner"} for x, y in pairs]
                if tag == "polygon" or raw.strip().lower().endswith("z"): nodes[-1]["closed"] = True
                item = LayoutObject(new_id("path"), "path", name, parent_id, 40 + left, 40 + top, max(1, right - left), max(1, bottom - top), fill=fill, stroke=stroke, stroke_width=stroke_width, opacity=opacity, path=nodes, metadata={"svg_source": raw, "svg_element": tag})
            elif tag == "text":
                item = LayoutObject(new_id("text"), "text", name, parent_id, 40 + _number(child.attrib.get("x")), 40 + _number(child.attrib.get("y")) - 18, max(1, _number(child.attrib.get("textLength"), 220)), 32, fill=fill, text="".join(child.itertext()), opacity=opacity, metadata={"font_size": _number(_style(child, "font-size"), 16), "svg_attributes": dict(child.attrib)})
            if item is not None:
                document.objects.append(item)
                if tag == "g": walk(child, item.id)
            elif list(child):
                walk(child, parent_id)

    walk(root, frame.id)
    document.select([frame.id])
    document.validate()
    return document


# ── the format registry ──────────────────────────────────────────────────

# Importers are registered, not special-cased. A new format is a function that
# produces a document graph plus an entry here; nothing in the window changes,
# and the formats Canvas cannot read yet are listed rather than pretended at.

@dataclass(frozen=True)
class ImportFormat:
    """One readable format: its name, its suffixes, and what reads it."""

    key: str
    label: str
    suffixes: tuple[str, ...]
    reader: Callable[[Path], Document] | None = None
    note: str = ""

    @property
    def available(self) -> bool:
        return self.reader is not None


_FORMATS: dict[str, ImportFormat] = {}


def register(fmt: ImportFormat) -> ImportFormat:
    _FORMATS[fmt.key] = fmt
    return fmt


def formats() -> list[ImportFormat]:
    return list(_FORMATS.values())


def readable_formats() -> list[ImportFormat]:
    return [fmt for fmt in _FORMATS.values() if fmt.available]


def format_for(path: Path) -> ImportFormat | None:
    suffix = path.suffix.lower()
    return next((fmt for fmt in _FORMATS.values() if suffix in fmt.suffixes), None)


def import_document(source: Path) -> Document:
    """Read any registered format, and say plainly when one is not readable."""
    fmt = format_for(source)
    if fmt is None:
        raise ValueError(f"Canvas does not read {source.suffix or 'that kind of file'} yet")
    if not fmt.available:
        raise ValueError(
            f"{fmt.label} files are not readable yet"
            + (f" — {fmt.note}" if fmt.note else "")
        )
    return fmt.reader(source)


register(ImportFormat("svg", "SVG", (".svg",), import_svg))
# Declared so the interface can list them honestly and so adding one later is
# a reader function rather than a change to the window.
def _read_fig(source: Path) -> Document:
    from .fig import import_fig
    return import_fig(source).document  # type: ignore[return-value]


register(ImportFormat("fig", "Figma", (".fig",), _read_fig,
                      "pages, frames, vectors, text, images and components"))
register(ImportFormat("indd", "InDesign", (".indd",), None,
                      "pages, masters, linked text and styles"))
register(ImportFormat("ai", "Illustrator", (".ai",), None,
                      "paths, gradients and artboards"))
register(ImportFormat("psd", "Photoshop", (".psd",), None,
                      "layers, masks and smart objects"))
register(ImportFormat("sketch", "Sketch", (".sketch",), None,
                      "pages, symbols and shared styles"))
