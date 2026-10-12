# SPDX-License-Identifier: Apache-2.0
"""Pure document data for LumaUI's creative layer tree.

Canvas stores objects in paint order. The shared LayerTree presents the last
painted sibling first, including each container's children.
"""

from __future__ import annotations

from dataclasses import dataclass

from .layers import children_by_parent
from .model import Document


CREATIVE_ICONS = {
    "frame": "hash", "page": "hash", "section": "hash", "spread": "hash",
    "group": "layers", "layout": "layers", "mask": "layers",
    "component": "component", "instance": "component",
    "rectangle": "square", "ellipse": "circle", "polygon": "pentagon",
    "star": "star", "line": "minus", "path": "pen-tool", "vector": "pen-tool",
    "boolean": "shapes", "text": "type", "image": "image",
}


@dataclass(frozen=True)
class CreativeLayerSpec:
    id: str
    name: str
    icon: str
    visible: bool
    children: tuple["CreativeLayerSpec", ...] = ()


def layer_specs(document: Document) -> tuple[CreativeLayerSpec, ...]:
    """Return the document's visible tree structure in v70 panel order."""
    grouped = children_by_parent(document)

    def under(parent_id: str | None) -> tuple[CreativeLayerSpec, ...]:
        return tuple(
            CreativeLayerSpec(item.id, item.name,
                              CREATIVE_ICONS.get(item.kind, "square"),
                              item.visible, under(item.id))
            for item in reversed(grouped.get(parent_id, ()))
        )

    return under(None)


def kit_layers(document: Document) -> list:
    """Build the shared C Layer objects through the AppKit convenience API."""
    from luma_appkit import Layer

    def convert(spec: CreativeLayerSpec):
        return Layer(spec.id, spec.name, spec.icon, visible=spec.visible, expanded=len(document.objects)<250,
                     children=[convert(child) for child in spec.children])

    return [convert(spec) for spec in layer_specs(document)]


def layer_move_target(document: Document, moving_id: str,
                      parent_id: str | None, top_index: int) -> tuple[str | None, str | None]:
    """Translate LayerTree's top-first drop slot to Editor.reorder's paint slot.

    The shared tree reports an index among siblings with the moving layer
    removed. Canvas stores siblings bottom-first and ``Editor.reorder`` takes
    the id of the next painted sibling, or ``None`` for the top of the stack.
    """
    if document.object(moving_id) is None:
        raise ValueError("unknown moving layer")
    if parent_id is not None and document.object(parent_id) is None:
        raise ValueError("unknown target parent")
    if parent_id == moving_id or any(parent.id == moving_id for parent in document.ancestors(parent_id)):
        raise ValueError("a layer cannot move into itself")
    siblings = [item for item in reversed(children_by_parent(document).get(parent_id, ()))
                if item.id != moving_id]
    index = max(0, min(int(top_index), len(siblings)))
    before_id = siblings[index - 1].id if index else None
    return parent_id, before_id
