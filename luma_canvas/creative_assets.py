# SPDX-License-Identifier: Apache-2.0
"""Canvas document actions behind the creative Assets panel.

The shared panel owns the presentation. These operations use the document's
styles and embedded photos and keep every edit in Canvas's undo history.
"""

from __future__ import annotations

from dataclasses import dataclass

from .editing import EditError, Editor
from .model import Asset, Document, Style
from .paint import parse_hex, solid


@dataclass(frozen=True)
class AssetCatalog:
    colors: tuple[Style, ...]
    texts: tuple[Style, ...]
    photos: tuple[Asset, ...]
    components: tuple[str, ...] = ("button", "badge", "card")


def asset_catalog(document: Document, *, photo_names: tuple[str, ...] = ()) -> AssetCatalog:
    """Expose only existing document assets; a fixture can order its six photos."""
    photos = [asset for asset in document.assets
              if asset.media_type.startswith("image/") and not asset.missing]
    if photo_names:
        by_name = {asset.name: asset for asset in photos}
        photos = [by_name[name] for name in photo_names if name in by_name]
    return AssetCatalog(
        tuple(style for style in document.styles if style.kind == "color"),
        tuple(style for style in document.styles if style.kind == "text"),
        tuple(photos),
    )


def apply_color(editor: Editor, style_id: str) -> None:
    style = next((item for item in editor.document.styles
                  if item.id == style_id and item.kind == "color"), None)
    if style is None:
        raise EditError("Color style is unavailable")
    color = parse_hex(str(style.value.get("value", "")))
    if color is None:
        raise EditError("Color style has an invalid color")
    targets = [item.id for item in editor.document.selection if item.kind != "image"]
    if not targets:
        raise EditError("Select a layer that can have a fill")
    editor.set_paint(targets, "fill", solid(color), label=f"Apply {style.name} color")


def apply_text_style(editor: Editor, style_id: str) -> None:
    style = next((item for item in editor.document.styles
                  if item.id == style_id and item.kind == "text"), None)
    if style is None:
        raise EditError("Text style is unavailable")
    targets = [item for item in editor.document.selection if item.kind == "text"]
    if not targets:
        raise EditError("Select some text first")
    family = str(style.value["family"])
    size = float(style.value["size"])
    weight = int(style.value["weight"])
    with editor.transaction(f"Apply {style.name} text style"):
        for item in targets:
            metadata = dict(item.metadata or {})
            metadata.update(font_family=family, font_size=size, font_weight=weight)
            editor.set_property([item.id], "metadata", metadata)
            line_height = metadata.get("line_height", {"unit": "percent", "value": 120})
            multiplier = (float(line_height.get("value", 120)) / 100
                          if isinstance(line_height, dict) else 1.2)
            editor.set_geometry(item.id, height=max(1, round(size * multiplier)))


def choose_photo(editor: Editor, asset_id: str) -> None:
    asset = next((item for item in editor.document.assets if item.id == asset_id), None)
    if asset is None or not asset.media_type.startswith("image/") or asset.missing:
        raise EditError("Photo is unavailable")
    chosen = next((item for item in editor.document.selection if item.kind == "image"), None)
    if chosen is not None:
        with editor.transaction(f"Replace {chosen.name}"):
            editor._editable(chosen.id).asset_id = asset.id
            editor.document.touch()
        return
    selected = editor.document.selection
    frame = next((item for item in selected if item.kind == "frame"), None)
    if frame is None and selected:
        frame = next((parent for parent in editor.document.ancestors(selected[0].id)
                      if parent.kind == "frame"), None)
    if frame is None:
        frame = next((item for item in editor.document.objects if item.kind == "frame"), None)
    x, y = (frame.x + 60, frame.y + 60) if frame else (60, 60)
    editor.create("image", x, y, 400, 300, parent_id=frame.id if frame else None,
                  name="Photo", asset_id=asset.id, radius=16, fill=None)


def insert_component_preset(editor: Editor, key: str) -> None:
    """Insert one of v70's three starter components into the active frame."""
    if key not in {"button", "badge", "card"}:
        raise EditError("Component preset is unavailable")
    selected = editor.document.selection
    frame = next((item for item in selected if item.kind == "frame"), None)
    if frame is None and selected:
        frame = next((parent for parent in editor.document.ancestors(selected[0].id)
                      if parent.kind == "frame"), None)
    if frame is None:
        frame = next((item for item in editor.document.objects if item.kind == "frame"), None)
    if frame is None:
        raise EditError("Add a frame first")
    x, y = frame.x + 80, frame.y + 80
    photo = None
    if key == "card":
        photo = next((item for item in editor.document.assets
                      if item.name == "life-ari-street.webp" and not item.missing), None)
        if photo is None:
            photo = next((item for item in editor.document.assets
                          if item.media_type.startswith("image/") and not item.missing), None)
        if photo is None:
            raise EditError("Add a photo to this document first")
    with editor.transaction(f"Insert {key} preset"):
        if key == "card":
            image = editor.create("image", x, y, 320, 400, parent_id=frame.id,
                                  name="Photo card", use_style=False, asset_id=photo.id,
                                  radius=24, fill=None,
                                  effects=[{"type": "drop_shadow", "x": 0, "y": 30,
                                            "blur": 40, "color": "#00000047"}])
            ids = [image.id]
        else:
            is_button = key == "button"
            width, height = (240, 64) if is_button else (150, 44)
            shape = editor.create("rectangle", x, y, width, height, parent_id=frame.id,
                                  name="Button" if is_button else "Badge", use_style=False,
                                  radius=32 if is_button else 22,
                                  fill="#6f7cff" if is_button else "#f2b544")
            label = editor.create("text", x, y + (17 if is_button else 9), width,
                                  29 if is_button else 24, parent_id=frame.id,
                                  name="Button label" if is_button else "Badge label",
                                  use_style=False, text="Get Luma" if is_button else "NEW",
                                  fill="#ffffff" if is_button else "#16161a",
                                  metadata={"font_family": "Figtree", "font_size": 24 if is_button else 20,
                                            "font_weight": 600 if is_button else 700,
                                            "align_h": "center",
                                            "letter_spacing": {"unit": "percent", "value": 0 if is_button else 10}})
            ids = [shape.id, label.id]
        editor.document.select(ids)
