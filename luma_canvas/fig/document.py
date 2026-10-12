# SPDX-License-Identifier: MPL-2.0
"""A decoded Figma document: its nodes as a tree, its blobs and its images."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import kiwi
from .container import FigArchive
from .errors import DamagedFigmaFile, UnsupportedFigmaFile

Guid = tuple[int, int]

# Fields the importer never reads. They are walked over without building
# values; the key is kept (as True) so the report can still say they existed.
SKIPPED_FIELDS = (
    "TextData.glyphs", "DerivedTextData.glyphs", "TextData.decorations", "DerivedTextData.decorations",
    "TextData.logicalIndexToCharacterOffsetMap", "DerivedTextData.logicalIndexToCharacterOffsetMap",
    "TextData.hyperlinkBoxes", "DerivedTextData.hyperlinkBoxes", "TextData.mentionBoxes",
    "DerivedTextData.mentionBoxes", "NodeChange.pluginData",
    "NodeChange.pluginRelaunchData", "NodeChange.objectAnimations", "NodeChange.editInfo",
    "NodeChange.widgetSyncedState", "NodeChange.renderedSyncedState", "NodeChange.chatMessages",
    "NodeChange.codeSnapshot", "NodeChange.codeSnapshotLayers", "NodeChange.collaborativeSourceCode",
    "NodeChange.sourceCode", "NodeChange.derivedImmutableFrameData", "NodeChange.annotations",
    "NodeChange.libraryMoveHistory", "Message.localUndoStack", "Message.localRedoStack",
    "Message.userChanges", "Message.sceneGraphQueries", "NodeChange.overrideStash",
    "NodeChange.overrideStashV2", "NodeChange.thumbnailInfo",
)


def guid_of(value: Any) -> Guid | None:
    if not isinstance(value, dict):
        return None
    session, local = value.get("sessionID"), value.get("localID")
    if not isinstance(session, int) or not isinstance(local, int):
        return None
    if session == 0xFFFFFFFF or local == 0xFFFFFFFF:
        return None
    return session, local


def guid_text(guid: Guid) -> str:
    return f"{guid[0]}:{guid[1]}"


@dataclass
class FigDocument:
    archive: FigArchive
    nodes: dict[Guid, dict[str, Any]]
    children: dict[Guid, list[dict[str, Any]]]
    blobs: list[bytes]
    root: dict[str, Any] | None
    schema_names: set[str] = field(default_factory=set)

    @property
    def editor(self) -> str:
        return self.archive.editor

    def blob(self, index: Any) -> bytes | None:
        if isinstance(index, int) and 0 <= index < len(self.blobs):
            return self.blobs[index]
        return None

    def node(self, guid: Guid | None) -> dict[str, Any] | None:
        return self.nodes.get(guid) if guid is not None else None

    def kids(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        return self.children.get(node["_guid"], [])

    def release(self) -> None:
        """Free the decoded tree a node at a time.

        Dropping a tree of millions of objects at once frees them in one
        uninterrupted run that holds the interpreter lock for most of a
        second; freeing piecemeal lets the window keep drawing meanwhile.
        """
        self.children.clear()
        nodes = self.nodes
        while nodes:
            nodes.popitem()
        self.blobs.clear()
        self.root = None


def decode(archive: FigArchive, *, progress: Callable[[float], None] | None = None) -> FigDocument:
    try:
        schema = kiwi.decode_schema(archive.schema)
    except kiwi.KiwiError as error:
        raise DamagedFigmaFile(f"the schema is damaged ({error})") from error
    if schema.definition("Message") is None or schema.definition("NodeChange") is None:
        raise UnsupportedFigmaFile("the schema has no Message or NodeChange")
    names = {definition.name for definition in schema.definitions}
    skip = [name for name in SKIPPED_FIELDS if name.split(".", 1)[0] in names]
    try:
        decoder = kiwi.Decoder(schema, skip=skip, watch={"Message.nodeChanges": progress} if progress else None)
        message = decoder.decode(archive.message, "Message")
    except kiwi.KiwiError as error:
        raise DamagedFigmaFile(f"the document is damaged ({error})") from error
    if not isinstance(message, dict):
        raise DamagedFigmaFile("the document is not a message")

    blobs = [bytes(blob.get("bytes") or b"") if isinstance(blob, dict) else b""
             for blob in message.get("blobs") or []]
    nodes: dict[Guid, dict[str, Any]] = {}
    order: dict[Guid, int] = {}
    for index, node in enumerate(message.get("nodeChanges") or []):
        if not isinstance(node, dict):
            continue
        guid = guid_of(node.get("guid"))
        if guid is None or node.get("phase") == "REMOVED":
            continue
        node["_guid"] = guid
        parent = node.get("parentIndex")
        if isinstance(parent, dict):
            node["_parent"] = guid_of(parent.get("guid"))
            position = parent.get("position")
            node["_position"] = position if isinstance(position, str) else ""
        else:
            node["_parent"] = None
            node["_position"] = ""
        nodes[guid] = node
        order.setdefault(guid, index)

    children: dict[Guid, list[dict[str, Any]]] = {}
    root = None
    for guid, node in nodes.items():
        parent = node["_parent"]
        if parent is None or parent not in nodes or parent == guid:
            if node.get("type") == "DOCUMENT" and root is None:
                root = node
            continue
        children.setdefault(parent, []).append(node)
    for siblings in children.values():
        siblings.sort(key=lambda item: (item["_position"], order[item["_guid"]]))
    if root is None:
        root = next((node for node in nodes.values() if node.get("type") == "DOCUMENT"), None)
    return FigDocument(archive, nodes, children, blobs, root, names)
