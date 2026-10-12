# SPDX-License-Identifier: MPL-2.0
"""Copy and paste of layers.

Canvas puts three flavors on the clipboard: its own JSON (every copied layer
with all its descendants, the styles and embedded images they use, so a
paste into another document is complete), SVG and PNG. It reads its own
JSON, SVG (as editable layers), PNG and JPEG images and plain text.

Where a paste lands follows Figma:

* With a frame (or group) selected, the layers go inside it, keeping their
  position relative to where they were copied from when that fits, else
  centered in it.
* Otherwise they go into the selected layer's parent, at their original
  position. If identical layers already sit exactly there (paste right after
  copy), the paste is offset so it is visible.
* Paste over selection (Ctrl+Shift+V) centers the paste on the selection,
  or keeps the exact original coordinates when nothing is selected.

Nothing here touches GTK; `LayoutCanvas` wires these to `Gdk.Clipboard`.
"""

from __future__ import annotations

import base64
import copy
import json
from typing import Any, Iterable

from .model import (CONTAINER_KINDS, Asset, Document, LayoutObject, Style, _object_from_dict, asset_bytes,
                    new_id, paint_asset_ids)

MIME = "application/x-luma-canvas+json"
FORMAT = "org.projectluma.canvas.layers"
PASTE_OFFSET = 10.0


def roots_of(document: Document, ids: Iterable[str]) -> list[LayoutObject]:
    """Selected layers without those already inside another selected layer."""
    chosen = [item for item in document.objects if item.id in set(ids)]
    chosen_ids = {item.id for item in chosen}
    return [item for item in chosen
            if not any(parent.id in chosen_ids for parent in document.ancestors(item.id))]


def _bounds(items: list[LayoutObject]) -> tuple[float, float, float, float] | None:
    if not items:
        return None
    return (min(item.x for item in items), min(item.y for item in items),
            max(item.x + item.width for item in items), max(item.y + item.height for item in items))


def serialize(document: Document, ids: Iterable[str]) -> dict[str, Any] | None:
    roots = roots_of(document, ids)
    if not roots:
        return None
    family: list[LayoutObject] = []
    for root in roots:
        family.append(root)
        family.extend(document.descendants(root.id))
    order = {item.id: index for index, item in enumerate(document.objects)}
    family.sort(key=lambda item: order[item.id])
    style_ids = {style_id for item in family for style_id in item.style_ids}
    for item in family:
        for key in ("fill_style_id", "stroke_style_id"):
            if item.metadata.get(key):
                style_ids.add(item.metadata[key])
    asset_ids = {asset_id for item in family for asset_id in paint_asset_ids(item)}
    assets = []
    for asset in document.assets:
        if asset.id not in asset_ids:
            continue
        entry = {"id": asset.id, "name": asset.name, "media_type": asset.media_type,
                 "linked_uri": asset.linked_uri, "embedded_name": asset.embedded_name,
                 "width": asset.width, "height": asset.height}
        data = asset_bytes(document.embedded_assets, asset)
        if data:
            entry["data"] = base64.b64encode(data).decode("ascii")
        assets.append(entry)
    from dataclasses import asdict
    return {
        "format": FORMAT,
        "version": 1,
        "document_id": document.id,
        "roots": [root.id for root in roots],
        "objects": [asdict(item) for item in family],
        "styles": [asdict(style) for style in document.styles if style.id in style_ids],
        "assets": assets,
        "bounds": list(_bounds(roots) or (0, 0, 0, 0)),
    }


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decode(data: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return None
    return payload


def _target_parent(document: Document, selection: list[LayoutObject]) -> tuple[str | None, LayoutObject | None]:
    """Where pasted layers go: into a selected container, else beside the selection."""
    if len(selection) == 1 and selection[0].kind in CONTAINER_KINDS and selection[0].kind != "boolean":
        return selection[0].id, selection[0]
    if selection:
        parent = document.object(selection[0].parent_id)
        return (parent.id, parent) if parent is not None else (None, None)
    return None, None


def paste(editor: Any, payload: dict[str, Any], *, over_selection: bool = False,
          view_rect: tuple[float, float, float, float] | None = None,
          label: str | None = None) -> list[str]:
    """Insert a clipboard payload into the editor's document; returns new root ids."""
    document = editor.document
    source_objects = [_object_from_dict(copy.deepcopy(value)) for value in payload.get("objects", [])]
    if not source_objects:
        return []
    source_roots = [item for item in source_objects if item.id in set(payload.get("roots", []))]
    selection = [item for item in document.selection]
    parent_id, parent = _target_parent(document, selection)
    if selection and {item.id for item in selection} == set(payload.get("roots", [])):
        # Pasting what is selected and was just copied: a sibling, not a child of itself.
        found = document.object(selection[0].parent_id)
        parent_id, parent = (found.id, found) if found is not None else (None, None)
    left, top, right, bottom = payload.get("bounds") or _bounds(source_roots) or (0, 0, 0, 0)
    width, height = right - left, bottom - top

    dx = dy = 0.0
    if over_selection and selection:
        sel_left = min(item.x for item in selection)
        sel_top = min(item.y for item in selection)
        sel_right = max(item.x + item.width for item in selection)
        sel_bottom = max(item.y + item.height for item in selection)
        dx = (sel_left + sel_right) / 2 - (left + right) / 2
        dy = (sel_top + sel_bottom) / 2 - (top + bottom) / 2
        parent = document.object(selection[0].parent_id)
        parent_id = parent.id if parent else None
    elif parent is not None and parent.kind in CONTAINER_KINDS and parent in selection:
        inside = (parent.x <= left and parent.y <= top and right <= parent.x + parent.width
                  and bottom <= parent.y + parent.height)
        if not inside:
            dx = parent.x + (parent.width - width) / 2 - left
            dy = parent.y + (parent.height - height) / 2 - top
    elif not over_selection:
        signatures = {(item.kind, item.name, round(item.x, 3), round(item.y, 3),
                       round(item.width, 3), round(item.height, 3), item.parent_id)
                      for item in document.objects}
        if all((root.kind, root.name, round(root.x, 3), round(root.y, 3), round(root.width, 3),
                round(root.height, 3), parent_id) in signatures for root in source_roots):
            dx = dy = PASTE_OFFSET
        elif view_rect is not None and (right < view_rect[0] or left > view_rect[2]
                                        or bottom < view_rect[1] or top > view_rect[3]):
            # The original place is out of view: paste in the middle of the view.
            dx = (view_rect[0] + view_rect[2]) / 2 - (left + right) / 2
            dy = (view_rect[1] + view_rect[3]) / 2 - (top + bottom) / 2

    id_map = {item.id: new_id(item.kind) for item in source_objects}
    root_ids = {root.id for root in source_roots}
    existing_styles = {style.id for style in document.styles}
    new_roots: list[str] = []
    with editor.transaction(label or ("Paste" if len(source_roots) == 1 else f"Paste {len(source_roots)} layers")):
        for value in payload.get("styles", []):
            if value.get("id") not in existing_styles:
                document.styles.append(Style(**value))
        asset_map: dict[str, str] = {}
        for value in payload.get("assets", []):
            if not isinstance(value, dict) or not value.get("id"):
                continue
            encoded = value.get("data")
            try:
                data = base64.b64decode(encoded) if encoded else None
            except (ValueError, TypeError):
                data = None
            fields = {key: value.get(key) for key in ("id", "name", "media_type", "linked_uri",
                                                      "embedded_name", "width", "height")}
            found = adopt_asset(document, Asset(**fields), data)
            if found is not None:
                asset_map[value["id"]] = found.id
        known_assets = {asset.id for asset in document.assets}
        known_styles = {style.id for style in document.styles}
        known_components = {item.id for item in document.objects if item.kind == "component"}
        for item in source_objects:
            old_id = item.id
            item.id = id_map[old_id]
            if old_id in root_ids:
                item.parent_id = parent_id
                new_roots.append(item.id)
            else:
                item.parent_id = id_map.get(item.parent_id, item.parent_id)
            item.x += dx
            item.y += dy
            if item.component_source_id:
                mapped = id_map.get(item.component_source_id, item.component_source_id)
                item.component_source_id = mapped if (mapped in id_map.values() or mapped in known_components) else None
                if item.component_source_id is None and item.kind == "instance":
                    item.kind = "group"
            item.text_flow_next_id = id_map.get(item.text_flow_next_id) if item.text_flow_next_id else None
            item.mask_id = id_map.get(item.mask_id) if item.mask_id else None
            if item.asset_id:
                item.asset_id = asset_map.get(item.asset_id, item.asset_id)
                if item.asset_id not in known_assets:
                    item.asset_id = None
            _drop_missing_image_paints(item, known_assets, asset_map)
            item.style_ids = [value for value in item.style_ids if value in known_styles]
            document.objects.append(item)
        document.select(new_roots)
        document.touch()
    return new_roots


def _fix_image_paint(paint: Any, known_assets: set[str], asset_map: dict[str, str]) -> Any:
    """An image paint pointing at the asset the document keeps for it, or
    marked missing (Canvas's checkerboard) when the picture did not come along.
    Returns a new dict when something changed, else the paint itself."""
    if not isinstance(paint, dict) or paint.get("type") != "image" or not paint.get("asset_id"):
        return paint
    asset_id = asset_map.get(paint["asset_id"], paint["asset_id"])
    if asset_id in known_assets:
        return paint if asset_id == paint["asset_id"] else {**paint, "asset_id": asset_id}
    fixed = {key: value for key, value in paint.items() if key != "asset_id"}
    fixed["missing_asset_id"] = paint["asset_id"]
    return fixed


def _drop_missing_image_paints(item: LayoutObject, known_assets: set[str],
                               asset_map: dict[str, str] | None = None) -> None:
    """Point image paints at the pasted document's assets, in every place a
    layer keeps paints: the `fills` and `strokes` stacks, the legacy extra
    paint metadata and the gradient slots. Lists and dicts are replaced, never
    changed in place, so the payload and undo snapshots stay as they were."""
    asset_map = asset_map or {}

    def fixed_stack(stack: Any) -> Any:
        if not isinstance(stack, list):
            return stack
        fixed = [_fix_image_paint(paint, known_assets, asset_map) for paint in stack]
        return fixed if any(new is not old for new, old in zip(fixed, stack)) else stack

    for key in ("fills", "strokes"):
        stack = getattr(item, key, None)
        fixed = fixed_stack(stack)
        if fixed is not stack:
            setattr(item, key, fixed)
    for key in ("fill_gradient", "stroke_gradient"):
        paint = getattr(item, key, None)
        fixed = _fix_image_paint(paint, known_assets, asset_map)
        if fixed is not paint:
            setattr(item, key, fixed)
    metadata = item.metadata or {}
    changed = {key: fixed_stack(metadata[key]) for key in ("extra_fills", "extra_strokes") if metadata.get(key)}
    if any(changed[key] is not metadata[key] for key in changed):
        item.metadata = {**metadata, **changed}


def _digest(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def find_asset_by_bytes(document: Document, data: bytes) -> Asset | None:
    """An asset of the document whose embedded bytes are exactly `data`,
    whether they are keyed by asset id (packages) or embedded name (old pastes)."""
    wanted = None
    for asset in document.assets:
        existing = asset_bytes(document.embedded_assets, asset)
        if existing is None or len(existing) != len(data):
            continue
        wanted = wanted or _digest(data)
        if existing is data or _digest(existing) == wanted:
            return asset
    return None


def _key_by_id(document: Document, asset: Asset, data: bytes | None) -> None:
    """Make sure the asset's bytes sit under its id, where saving looks.
    Bytes under the old embedded-name key are copied, never dropped."""
    if asset.id in document.embedded_assets:
        return
    existing = asset_bytes(document.embedded_assets, asset)
    if existing is not None:
        document.embedded_assets[asset.id] = existing
    elif data is not None:
        document.embedded_assets[asset.id] = data


def adopt_asset(document: Document, incoming: Asset, data: bytes | None) -> Asset | None:
    """The document's asset for a pasted one: the same id, else the same
    bytes under another id, else the incoming asset added. None when it has
    neither bytes nor a link, so its paints are marked missing."""
    by_id = next((asset for asset in document.assets if asset.id == incoming.id), None)
    if by_id is not None:
        _key_by_id(document, by_id, data)
        return by_id
    if data is not None:
        same = find_asset_by_bytes(document, data)
        if same is not None:
            _key_by_id(document, same, data)
            return same
    if data is None and not incoming.linked_uri:
        return None
    document.assets.append(incoming)
    if data is not None:
        # Keyed by asset id, as packages save and load them.
        document.embedded_assets[incoming.id] = data
    return incoming


def image_layer(editor: Any, data: bytes, media_type: str, width: int, height: int, *,
                center: tuple[float, float], max_size: tuple[float, float] | None = None,
                name: str = "Image") -> LayoutObject:
    """A pasted or dropped picture: an image layer with its bytes embedded once.

    A picture whose bytes the document already has reuses that asset, found by
    its id-keyed bytes (packages) or its embedded name (older pastes)."""
    document = editor.document
    digest = _digest(data)[:24]
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(media_type, "bin")
    embedded_name = f"assets/{digest}.{extension}"
    scale = 1.0
    if max_size is not None and width and height:
        scale = min(1.0, max_size[0] / width, max_size[1] / height)
    w, h = max(1.0, width * scale), max(1.0, height * scale)
    with editor.transaction(f"Paste {name}"):
        asset = find_asset_by_bytes(document, data)
        if asset is None:
            named = next((value for value in document.assets if value.embedded_name == embedded_name), None)
            if named is not None and asset_bytes(document.embedded_assets, named) is None:
                # Its bytes were lost: these are them (the name is their digest).
                asset = named
        if asset is None:
            asset = Asset(new_id("asset"), name, media_type, embedded_name=embedded_name,
                          width=int(width), height=int(height))
            document.assets.append(asset)
        _key_by_id(document, asset, data)
        item = LayoutObject(new_id("image"), "image", name, None, round(center[0] - w / 2),
                            round(center[1] - h / 2), w, h, fill=None, asset_id=asset.id)
        document.objects.append(item)
        document.select([item.id])
        document.touch()
    return item


def svg_payload(svg_text: str) -> dict[str, Any] | None:
    """Pasted SVG as a layers payload (editable layers, not a picture)."""
    import tempfile
    from pathlib import Path
    from .importers import import_svg
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "Pasted.svg"
        path.write_text(svg_text)
        try:
            imported = import_svg(path)
        except ValueError:
            return None
    roots = [item for item in imported.objects if item.parent_id is None]
    for root in roots:
        root.name = "SVG"
    return serialize(imported, [root.id for root in roots])


def svg_text(document: Document, ids: Iterable[str]) -> str | None:
    from .export import svg_text as export_svg_text
    try:
        return export_svg_text(document, [root.id for root in roots_of(document, ids)])
    except (ValueError, OSError):
        return None
