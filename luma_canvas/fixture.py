# SPDX-License-Identifier: Apache-2.0
"""Read Studio's Canvas sample into an isolated, in-memory document.

The JSON is a transcription of v70's ``seed()``.  Image bytes are read only
from the explicitly supplied Studio assets directory and kept in memory;
this module never opens a Canvas document or creates a user-data directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Asset, Document, LayoutObject, Page, Style
from .paint import Color, gradient, parse_hex


def _paint(value: str) -> tuple[str, dict | None]:
    if value == "linear-gradient(160deg, #1d2346, #0b0d18)":
        return "#1d2346", gradient(
            "linear", [(0, parse_hex("#1d2346")), (1, parse_hex("#0b0d18"))],
            [[0.67, 0.03], [0.33, 0.97], [1.14, 0.20]],
        )
    if value == "radial-gradient(circle, rgba(111,124,255,.75), rgba(111,124,255,0) 68%)":
        return "#6f7cff", gradient(
            "radial", [(0, Color(111 / 255, 124 / 255, 1, .75)),
                       (.68, Color(111 / 255, 124 / 255, 1, 0)),
                       (1, Color(111 / 255, 124 / 255, 1, 0))],
        )
    if parse_hex(value) is None:
        raise ValueError(f"unrecognised Canvas fixture paint: {value}")
    return value, None


def load_fixture(path: Path, *, assets_dir: Path) -> Document:
    """Load the v70 fixture. Missing image bytes fail instead of showing fakes."""
    data = json.loads(path.read_text("utf-8"))
    document = Document.empty(data["name"])
    document.pages = [Page(f"fixture-page-{index}", name)
                      for index, name in enumerate(data["pages"], 1)]
    document.current_page_id = next(page.id for page in document.pages
                                    if page.name == data["page"])
    document.styles = [Style(f"fixture-color-{index}", value.upper(), "color", {"value": value})
                       for index, value in enumerate(data["swatches"], 1)]
    document.styles.extend(
        Style(f"fixture-text-{index}", spec["name"], "text",
              {"family": spec["font"], "weight": spec["weight"], "size": spec["size"]})
        for index, spec in enumerate(data["text_styles"], 1)
    )
    known: dict[str, LayoutObject] = {}
    for node in data["nodes"]:
        parent = known.get(node.get("parent"))
        if node.get("parent") and parent is None:
            raise ValueError(f"unknown fixture parent: {node['parent']}")
        kind = {"rect": "rectangle"}.get(node["t"], node["t"])
        size = float(node.get("fs", 32))
        line_height = float(node.get("lh", 1.2))
        fill, fill_gradient = _paint(node.get("color", "#ffffff") if kind == "text"
                                     else node.get("fill", "#d9d9d9"))
        metadata = {}
        if kind == "text":
            metadata = {
                "font_family": node.get("ff", "Figtree"),
                "font_size": size,
                "font_weight": int(node.get("fw", 400)),
                "line_height": {"unit": "percent", "value": line_height * 100},
                "letter_spacing": {"unit": "percent", "value": float(node.get("ls", 0)) * 100},
                "align_h": node.get("align", "left"),
            }
        effects = []
        if kind == "frame":
            # v70's .cv-frame casts a shadow onto the dotted workspace.
            effects.append({"type": "drop_shadow", "x": 0, "y": 10, "blur": 40,
                            "spread": -10, "color": "#00000073"})
        if node.get("shadow"):
            effects.append({"type": "drop_shadow", "x": 0, "y": 30, "blur": 40,
                            "color": "#00000047"})
        item = LayoutObject(
            id=node["id"], kind=kind, name=node["name"], parent_id=node.get("parent"),
            x=float(node["x"]) + (parent.x if parent else 0),
            y=float(node["y"]) + (parent.y if parent else 0),
            width=float(node["w"]), height=float(node.get("h", round(size * line_height))),
            rotation=float(node.get("rot", 0)), radius=float(node.get("r", 0)),
            fill=fill if kind != "image" else None,
            fill_gradient=fill_gradient,
            effects=effects,
            text=node.get("text", ""), metadata=metadata,
            asset_id=node.get("asset"),
        )
        known[item.id] = item
        document.objects.append(item)
    for asset_id, filename in data["assets"].items():
        image_path = assets_dir / filename
        if image_path.name != filename or not image_path.is_file():
            raise FileNotFoundError(image_path)
        document.assets.append(Asset(asset_id, filename, "image/webp", embedded_name=filename))
        document.embedded_assets[asset_id] = image_path.read_bytes()
    document.select(data["selected_ids"])
    document.validate()
    return document


class FixtureSession:
    """The document-session boundary in fixture mode: edits stay in memory."""

    path = None
    pristine = True
    untitled = False
    has_saved = False
    location_label = "Fixture"
    last_opened = None
    last_saved = None

    def __init__(self, document: Document) -> None:
        self.document = document

    @property
    def display_name(self) -> str:
        return self.document.name

    def note_change(self) -> None:
        pass

    def save_now(self) -> None:
        pass

    def flush(self, _timeout: float = 5.0) -> bool:
        return True

    def close(self) -> None:
        pass

    def opened(self) -> None:
        pass

    def rename(self, name: str) -> None:
        """The fixture title may change in memory, with no document store."""
        value = name.strip()
        if not value:
            raise ValueError("Enter a document name")
        self.document.name = value

    def duplicate(self) -> None:
        raise RuntimeError("fixture mode cannot duplicate a document on disk")

    def __getattr__(self, name: str):
        raise AttributeError(name)
