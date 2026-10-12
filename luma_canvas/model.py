# SPDX-License-Identifier: Apache-2.0
"""Versioned, presentation-independent Canvas document graph.

Pages, frames, paths, text, components, and publication structures are nodes in
one graph.  The GTK surface is a view of this model; it is never the source of
document truth.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import UTC, datetime
import copy
import pickle
import uuid
from typing import Any, Iterable

from .paint import default_current_style, normalized_style


# Schema 2 adds paint stacks: `fills` and `strokes` on every layer, bottom to
# top. `fill`/`fill_gradient` and `stroke`/`stroke_gradient` stay as a mirror of
# the bottom paint for older readers. Schema 1 documents migrate on open.
SCHEMA_VERSION = 2
ARRANGEMENTS = {"design", "vector", "publication"}
OBJECT_KINDS = {
    "page", "spread", "frame", "section", "group", "path", "text", "image",
    "mask", "component", "instance", "layout", "table", "guide", "comment",
    "slice", "rectangle", "ellipse", "polygon", "star", "line", "boolean",
}
# Zoom is 1% to 25600%, as in Figma.
ZOOM_MIN = 0.01
ZOOM_MAX = 256.0
LINE_CAPS = {"none", "round", "square", "line_arrow", "triangle_arrow",
             "reverse_triangle", "circle", "diamond"}
# An instance contains the children it inherited from its component, so it
# is a container in the same sense a group is.
CONTAINER_KINDS = {"page", "spread", "frame", "section", "group", "component",
                   "instance", "layout", "mask", "boolean"}


class InvalidDocument(ValueError):
    pass


class UnsupportedDocumentVersion(InvalidDocument):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


# ── change tracking ──────────────────────────────────────────────────────
#
# A document with tens of thousands of layers cannot be copied whole on every
# edit. Instead every attribute write on a layer tells the open trackers which
# layer changed, and the first write to a watched layer keeps a copy of it as
# it was. Layers are never changed in place: code assigns a new list or dict
# (`item.path = [...]`), never `item.path.append(...)`. Tests run with
# CANVAS_VERIFY_CHANGES=1, which checks that against a full comparison.

_TRACKERS: list["ChangeTracker"] = []


class ChangeTracker:
    """The ids of layers written since `watch` (or creation), and the layers
    of the watched list as they were before their first write."""

    __slots__ = ("ids", "captured", "instances", "_before")

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.captured: dict[str, bytes] = {}
        # Which layer was written for each id. The same document read a second
        # time (a version thumbnail, recovery) makes other layers with the same
        # ids; a reader compares these against its own document's layers.
        self.instances: dict[str, Any] = {}
        self._before: dict[str, Any] | None = None

    def watch(self, objects: Iterable[Any] | None = None) -> None:
        """Start over; with `objects`, keep copies of those before they change."""
        self.ids = set()
        self.captured = {}
        self.instances = {}
        self._before = {item.id: item for item in objects} if objects is not None else None

    @property
    def before(self) -> dict[str, Any]:
        return self._before or {}

    def open(self) -> "ChangeTracker":
        if self not in _TRACKERS:
            _TRACKERS.append(self)
        return self

    def close(self) -> None:
        if self in _TRACKERS:
            _TRACKERS.remove(self)

    def take(self) -> set[str]:
        """The ids written since the last `take`, and start a new set."""
        ids, self.ids = self.ids, set()
        self.instances = {}
        return ids

    def take_changed(self) -> dict[str, Any]:
        """The layers written since the last call, by id: the value is the
        layer that was written, or None when only the id is known."""
        changed, self.instances = self.instances, {}
        self.ids = set()
        return changed

    def _touch(self, item: Any, object_id: str) -> None:
        self.ids.add(object_id)
        self.instances[object_id] = item
        before = self._before
        if before is not None and object_id not in self.captured and before.get(object_id) is item:
            self.captured[object_id] = pickle.dumps(item, pickle.HIGHEST_PROTOCOL)


def _tracked_setattr(self: Any, name: str, value: Any) -> None:
    if _TRACKERS:
        try:
            object_id = self.id
        except AttributeError:  # the first field while the layer is being made
            object_id = None
        if object_id is not None:
            for tracker in _TRACKERS:
                tracker._touch(self, object_id)
        object.__setattr__(self, name, value)
        if name == "id":
            for tracker in _TRACKERS:
                tracker.ids.add(value)
                tracker.instances[value] = self
        return
    object.__setattr__(self, name, value)


# Index of layer positions by id, per objects list (the list itself is kept
# with its index, so a reused id() never matches another list).
_OBJECT_INDEX: dict[int, tuple[list[Any], dict[str, int]]] = {}


@dataclass(slots=True)
class WorkspaceState:
    navigation_width: int = 204
    inspector_width: int = 258
    navigation_open: bool = True
    inspector_open: bool = True
    navigation_tab: str = "layers"
    inspector_sections: dict[str, bool] = field(default_factory=dict)

    def validate(self) -> None:
        if not 180 <= self.navigation_width <= 520:
            raise InvalidDocument("navigation width is outside 180…520")
        if not 220 <= self.inspector_width <= 560:
            raise InvalidDocument("inspector width is outside 220…560")
        if self.navigation_tab not in {"pages", "layers", "assets"}:
            raise InvalidDocument("unknown navigation tab")


@dataclass(slots=True)
class Style:
    id: str
    name: str
    kind: str
    value: dict[str, Any]

    def validate(self) -> None:
        if self.kind not in {"color", "text", "effect", "spacing", "grid", "object", "table"}:
            raise InvalidDocument(f"unsupported style kind: {self.kind}")


@dataclass(slots=True)
class Asset:
    id: str
    name: str
    media_type: str
    linked_uri: str | None = None
    embedded_name: str | None = None
    width: int = 0
    height: int = 0
    missing: bool = False

    def validate(self) -> None:
        if not self.id or not self.name or not self.media_type:
            raise InvalidDocument("assets require id, name, and media type")
        if self.linked_uri is None and self.embedded_name is None:
            raise InvalidDocument(f"asset {self.name} has neither linked nor embedded content")


@dataclass(slots=True)
class PrototypeConnection:
    id: str
    source_id: str
    destination_id: str
    trigger: str = "click"
    transition: str = "dissolve"
    duration_ms: int = 220
    action: str = "navigate"
    easing: str = "ease-in-out"
    delay_ms: int = 800
    key: str = "Return"
    overlay_position: str = "center"
    overlay_x: float = 0
    overlay_y: float = 0
    close_outside: bool = True
    scrim_opacity: float = 0.25
    preserve_scroll: bool = False
    variable: str = ""
    value: str = ""
    condition: str = "equals"
    else_destination_id: str | None = None
    url: str = ""
    interaction_id: str = ""
    script: list[dict[str, Any]] = field(default_factory=list)

    def referenced_ids(self) -> set[str]:
        result={self.source_id,self.destination_id}
        if self.else_destination_id:result.add(self.else_destination_id)
        def visit(steps,depth=0):
            if depth>24:raise InvalidDocument('prototype script is nested too deeply')
            if not isinstance(steps,list) or len(steps)>1024:
                raise InvalidDocument('prototype actions must be a bounded list')
            for step in steps:
                if not isinstance(step,dict):raise InvalidDocument('prototype action must be an object')
                if step.get('action') not in {'navigate','back','open-overlay','close-overlay','swap-overlay','scroll-to','set-variable','conditional','open-link','change-variant','noop'}:
                    raise InvalidDocument('unsupported prototype script action')
                if step.get('destination_id'):result.add(step['destination_id'])
                blocks=step.get('blocks',[])
                if not isinstance(blocks,list) or len(blocks)>1024:raise InvalidDocument('invalid prototype branches')
                for block in blocks:
                    if not isinstance(block,dict):raise InvalidDocument('prototype branch must be an object')
                    visit(block.get('actions',[]),depth+1)
        visit(self.script)
        return result

    def validate(self, object_ids: set[str]) -> None:
        if not isinstance(self.script,list):raise InvalidDocument("prototype script must be a list")
        if not self.referenced_ids()<=object_ids:
            raise InvalidDocument("prototype connection references a missing object")
        if self.trigger not in {"click", "hover", "drag", "keyboard", "timed", "mouse-enter", "mouse-leave", "press", "release", "while-hover", "while-press"}:
            raise InvalidDocument("unsupported prototype trigger")
        import math
        if not isinstance(self.duration_ms, (int, float)) or not math.isfinite(self.duration_ms) or not 0 <= self.duration_ms <= 10000:
            raise InvalidDocument("prototype duration must be between 0 and 10000 ms")
        if not isinstance(self.delay_ms, (int, float)) or not math.isfinite(self.delay_ms) or not 0 <= self.delay_ms <= 60000:
            raise InvalidDocument("prototype delay must be between 0 and 60000 ms")
        if self.action not in {"navigate", "back", "open-overlay", "close-overlay", "swap-overlay", "scroll-to", "set-variable", "conditional", "open-link", "change-variant"}:
            raise InvalidDocument("unsupported prototype action")
        if self.easing not in {"linear", "ease-in", "ease-out", "ease-in-out", "spring"}:
            raise InvalidDocument("unsupported prototype easing")
        if not math.isfinite(self.scrim_opacity) or not 0 <= self.scrim_opacity <= 1:
            raise InvalidDocument("prototype scrim opacity must be between 0 and 1")
        if self.overlay_position not in {"center", "top-left", "top", "top-right", "left", "right", "bottom-left", "bottom", "bottom-right", "manual"}:
            raise InvalidDocument("unsupported overlay position")
        if not all(math.isfinite(v) for v in (self.overlay_x, self.overlay_y)):
            raise InvalidDocument("overlay position must be finite")
        if self.condition not in {"equals", "not-equals"}:
            raise InvalidDocument("unsupported prototype condition")
        if self.else_destination_id is not None and self.else_destination_id not in object_ids:
            raise InvalidDocument("prototype alternative destination is missing")


@dataclass(slots=True)
class LayoutObject:
    id: str
    kind: str
    name: str
    parent_id: str | None = None
    x: float = 0.0
    y: float = 0.0
    width: float = 100.0
    height: float = 100.0
    rotation: float = 0.0
    opacity: float = 1.0
    visible: bool = True
    locked: bool = False
    fill: str | None = "#ffffff"
    stroke: str | None = None
    stroke_width: float = 0.0
    radius: float = 0.0
    text: str = ""
    path: list[dict[str, float | str | bool]] = field(default_factory=list)
    layout: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, str] = field(default_factory=dict)
    effects: list[dict[str, Any]] = field(default_factory=list)
    style_ids: list[str] = field(default_factory=list)
    component_source_id: str | None = None
    component_variant: str | None = None
    text_flow_next_id: str | None = None
    mask_id: str | None = None
    asset_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Mirroring, so the design's flip controls have something to change.
    flip_h: bool = False
    flip_v: bool = False
    # A rectangle's corners, clockwise from the top left. Empty means every
    # corner uses `radius`.
    corner_radii: list[float] = field(default_factory=list)
    # How a stroke sits on its path, and how it ends and turns.
    stroke_align: str = "center"
    stroke_cap: str = "butt"
    stroke_join: str = "miter"
    stroke_dash: list[float] = field(default_factory=list)
    # Gradient paints. When set, they are drawn instead of the solid `fill` or
    # `stroke`, which then hold the first stop's color for older readers.
    fill_gradient: dict[str, Any] | None = None
    stroke_gradient: dict[str, Any] | None = None
    # Polygon and star geometry; open paths' end decorations.
    point_count: int = 0
    inner_ratio: float = 0.382
    start_cap: str = "none"
    end_cap: str = "none"
    # Frames clip their children unless this is off (Figma's "Clip content").
    clip_content: bool = True
    # What an instance changed about its component, by field name. An instance
    # keeps its overrides when the component changes underneath it.
    overrides: dict[str, Any] = field(default_factory=dict)
    # A page's master, and the print facts a page carries.
    master_id: str | None = None
    columns: int = 1
    column_gap: float = 12.0
    baseline: float = 0.0
    bleed: float = 0.0
    margins: dict[str, float] = field(default_factory=dict)
    # Auto-layout children can grow. Constraints say how they follow a resize.
    grow: float = 0.0
    # Anything a format carried that Canvas cannot yet name. It is written back
    # out untouched, so importing a file Canvas half-understands does not
    # quietly destroy the parts it does not.
    extras: dict[str, Any] = field(default_factory=dict)
    # Paint stacks (schema 2), bottom to top: every paint has `type`, `opacity`,
    # `visible` and `blend`. Read them through `paint.paint_stack`.
    fills: list[dict[str, Any]] = field(default_factory=list)
    strokes: list[dict[str, Any]] = field(default_factory=list)

    # Every write reports the change to open trackers (see ChangeTracker).
    __setattr__ = _tracked_setattr

    def __setstate__(self, state: Any) -> None:
        """Rebuild a layer from pickled state without reporting anything.

        History, the clipboard and the change checks all rebuild layers from
        bytes. Those layers are new objects that happen to carry an existing
        id; reporting their fields would make a tracker believe the document's
        layer was edited (and hide which layer really was).
        """
        setter = object.__setattr__
        values = state[0] if isinstance(state, tuple) else state
        for name, value in (values or {}).items():
            setter(self, name, value)
        slots = state[1] if isinstance(state, tuple) and len(state) > 1 else None
        for name, value in (slots or {}).items():
            setter(self, name, value)

    def validate(self) -> None:
        if self.stroke_align not in {"inside", "center", "outside"}:
            raise InvalidDocument(f"{self.name} has an unsupported stroke alignment")
        if self.stroke_cap not in {"butt", "round", "square"}:
            raise InvalidDocument(f"{self.name} has an unsupported stroke cap")
        if self.stroke_join not in {"miter", "round", "bevel"}:
            raise InvalidDocument(f"{self.name} has an unsupported stroke join")
        if self.start_cap not in LINE_CAPS or self.end_cap not in LINE_CAPS:
            raise InvalidDocument(f"{self.name} has an unsupported line end")
        if self.point_count and not 3 <= self.point_count <= 60:
            raise InvalidDocument(f"{self.name} needs 3 to 60 points")
        if not 0.0 <= self.inner_ratio <= 1.0:
            raise InvalidDocument(f"{self.name} has a star ratio outside 0…1")
        if self.corner_radii and len(self.corner_radii) != 4:
            raise InvalidDocument(f"{self.name} needs four corner radii or none")
        if self.columns < 1:
            raise InvalidDocument(f"{self.name} cannot have fewer than one column")
        if self.kind not in OBJECT_KINDS:
            raise InvalidDocument(f"unsupported object kind: {self.kind}")
        if not self.id or not self.name:
            raise InvalidDocument("objects require stable IDs and names")
        if self.width < 0 or self.height < 0:
            raise InvalidDocument(f"{self.name} has a negative size")
        if not 0.0 <= self.opacity <= 1.0:
            raise InvalidDocument(f"{self.name} opacity is outside 0…1")
        if self.stroke_width < 0 or self.radius < 0:
            raise InvalidDocument(f"{self.name} contains negative appearance geometry")
        if self.kind == "path" and len(self.path) < 2:
            raise InvalidDocument(f"path {self.name} needs at least two nodes")
        if self.kind == "instance" and not self.component_source_id:
            raise InvalidDocument(f"instance {self.name} has no component source")
        direction = self.layout.get("direction")
        if direction is not None and direction not in {"horizontal", "vertical", "wrap"}:
            raise InvalidDocument(f"{self.name} has an invalid auto-layout direction")

    def clone(self, *, parent_id: str | None = None) -> "LayoutObject":
        value = copy.deepcopy(self)
        value.id = new_id(self.kind)
        value.name = f"{self.name} copy"
        value.parent_id = self.parent_id if parent_id is None else parent_id
        return value


def asset_bytes(embedded: dict[str, bytes], asset: "Asset") -> bytes | None:
    """An asset's embedded bytes.

    Bytes are keyed by asset id (what packages store and load); older pastes
    keyed them by embedded name, which saving silently dropped, so both are read.
    """
    data = embedded.get(asset.id)
    if data is None and asset.embedded_name:
        data = embedded.get(asset.embedded_name)
    return data


def canonical_asset_keys(document: "Document") -> bool:
    """Re-key bytes stored under an embedded name by their asset id."""
    changed = False
    for asset in document.assets:
        if asset.id not in document.embedded_assets and asset.embedded_name \
                and asset.embedded_name in document.embedded_assets:
            document.embedded_assets[asset.id] = document.embedded_assets.pop(asset.embedded_name)
            changed = True
    return changed


def paint_asset_ids(item: "LayoutObject") -> set[str]:
    """Every asset a layer draws: its image and the image paints in its stacks."""
    found = {item.asset_id} if item.asset_id else set()
    metadata = item.metadata or {}
    stacks = [metadata.get(key) or [] for key in ("extra_fills", "extra_strokes")]
    stacks += [getattr(item, "fills", None) or [], getattr(item, "strokes", None) or []]
    for stack in stacks:
        for paint in stack:
            if isinstance(paint, dict) and paint.get("type") == "image" and paint.get("asset_id"):
                found.add(str(paint["asset_id"]))
    for paint in (item.fill_gradient, item.stroke_gradient):
        if isinstance(paint, dict) and paint.get("type") == "image" and paint.get("asset_id"):
            found.add(str(paint["asset_id"]))
    return found


def _object_from_dict(value: dict[str, Any]) -> "LayoutObject":
    """Build an object, keeping anything this version cannot name.

    Refusing an unknown key would make every document written by a newer
    Canvas unopenable, and would throw away whatever an importer preserved
    from a format Canvas only half understands.
    """
    known = {slot for slot in LayoutObject.__slots__}
    accepted = {key: item for key, item in value.items() if key in known}
    unknown = {key: item for key, item in value.items() if key not in known}
    item = layer_from_values(accepted)
    if unknown:
        object.__setattr__(item, "extras", {**item.extras, **unknown})
    if "fills" not in value or "strokes" not in value:
        migrate_paint_stacks(item)
    return item


LEGACY_PAINT_METADATA = ("extra_fills", "hidden_fill", "extra_strokes", "hidden_stroke")


def migrate_paint_stacks(item: "LayoutObject") -> bool:
    """Schema 1 to 2: the fill, its hidden color and extra fills become `fills`
    (likewise strokes); the scalar fields stay as the bottom paint's mirror."""
    from .paint import paint_stack
    metadata = item.metadata or {}
    if item.fills or item.strokes or not (item.fill is not None or item.fill_gradient or item.stroke is not None
                                          or item.stroke_gradient or any(key in metadata for key in LEGACY_PAINT_METADATA)):
        return False
    item.fills = paint_stack(item, "fill")
    item.strokes = paint_stack(item, "stroke")
    if any(key in metadata for key in LEGACY_PAINT_METADATA):
        item.metadata = {key: value for key, value in metadata.items() if key not in LEGACY_PAINT_METADATA}
    return True


# Properties a group does not have of its own (Figma): editing them on a
# group edits the layers inside it. Effects are not among them: as in Figma, a
# group's shadow and blur belong to the group and are drawn on it as a whole
# (an inner shadow is the one exception, see `migrate_group_paint`).
GROUP_PAINT_KEYS = ("fill", "fill_gradient", "fills", "stroke", "stroke_gradient", "strokes", "stroke_width", "stroke_align",
                    "stroke_cap", "stroke_join", "stroke_dash")


def paint_targets(document: "Document", items: Iterable["LayoutObject"]) -> list["LayoutObject"]:
    """The layers whose paint a selection's Fill, Stroke and Effects edit.

    A group has no paint: it stands for the layers inside it, through nested
    groups. Every other layer (frames included) stands for itself.
    """
    result: list[LayoutObject] = []
    seen: set[str] = set()
    pending = list(items)
    while pending:
        item = pending.pop(0)
        if item.id in seen:
            continue
        seen.add(item.id)
        if item.kind == "group":
            pending[0:0] = document.children(item.id)
        else:
            result.append(item)
    return result


def group_inner_shadows(effects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A group's inner shadows: the one effect Canvas draws on its layers.

    A shadow or blur on a group is drawn on the group as a whole (Figma), but
    an inner shadow needs a shape to fall inside, so each layer casts its own.
    """
    return [effect for effect in effects if isinstance(effect, dict)
            and str(effect.get("type") or effect.get("kind") or "").replace("-", "_") == "inner_shadow"]


def migrate_group_paint(document: "Document") -> bool:
    """Move paint stored on groups to the layers inside, then clear it."""
    changed = False
    for group in [item for item in document.objects if item.kind == "group"]:
        stroke = group.stroke if group.stroke and group.stroke_width else None
        inner = group_inner_shadows(list(group.effects or []))
        if stroke is None and not inner and group.fill is None and not group.fill_gradient \
                and not group.stroke_gradient and not group.fills and not group.strokes:
            continue
        for leaf in paint_targets(document, document.children(group.id)):
            if stroke is not None and not (leaf.stroke and leaf.stroke_width):
                leaf.stroke, leaf.stroke_width = stroke, group.stroke_width
                leaf.stroke_gradient = copy.deepcopy(group.stroke_gradient)
                leaf.strokes = copy.deepcopy(group.strokes)
                leaf.stroke_align, leaf.stroke_cap = group.stroke_align, group.stroke_cap
                leaf.stroke_join, leaf.stroke_dash = group.stroke_join, list(group.stroke_dash)
            if inner and not leaf.effects:
                leaf.effects = copy.deepcopy(inner)
        group.fill = group.stroke = None
        group.fill_gradient = group.stroke_gradient = None
        group.fills, group.strokes = [], []
        group.stroke_width = 0.0
        if inner:
            group.effects = [effect for effect in group.effects if effect not in inner]
        changed = True
    return changed


@dataclass(slots=True)
class Page:
    """A page: its own top-level layers, background and remembered view.

    The current page's layers live in `Document.objects`, so every tool,
    panel and exporter works on the page being shown; other pages keep theirs
    here as plain dictionaries until they are shown.
    """
    id: str
    name: str
    background: str | None = None
    objects: list[dict[str, Any]] = field(default_factory=list)
    view: dict[str, float] = field(default_factory=dict)
    selected_ids: list[str] = field(default_factory=list)


def _shallow(value: Any) -> dict[str, Any]:
    return {entry.name: getattr(value, entry.name) for entry in fields(value)}


_OBJECT_FIELDS = tuple(entry.name for entry in fields(LayoutObject))


_FIELDS_BY_NAME = {entry.name: entry for entry in fields(LayoutObject)}


def _stored_default(name: str) -> Any:
    entry = _FIELDS_BY_NAME[name]
    if entry.default is not MISSING:
        return entry.default
    if entry.default_factory is not MISSING:  # type: ignore[misc]
        return entry.default_factory()  # type: ignore[misc]
    raise InvalidDocument(f"a layer is missing its {name}")


def layer_from_values(values: dict[str, Any]) -> LayoutObject:
    """A layer straight from stored values.

    Reading a document builds tens of thousands of layers; going through the
    dataclass would report every field to every open change tracker, which
    watches for edits, not for a document being read. The document itself is
    reported when it is put in a window.
    """
    item = LayoutObject.__new__(LayoutObject)
    setter = object.__setattr__
    for name in _OBJECT_FIELDS:
        setter(item, name, values[name] if name in values else _stored_default(name))
    return item


def object_dict(item: LayoutObject) -> dict[str, Any]:
    """A layer's fields as a dict, sharing its values (copy before keeping)."""
    return {key: getattr(item, key) for key in _OBJECT_FIELDS}


def note_changed(object_ids: Iterable[str]) -> None:
    """Report layers that changed without an attribute write (undo loads
    them from their saved bytes) to every open tracker."""
    ids = list(object_ids)
    for tracker in _TRACKERS:
        tracker.ids.update(ids)
        for object_id in ids:
            tracker.instances.setdefault(object_id, None)


@dataclass(slots=True)
class Document:
    id: str
    name: str
    objects: list[LayoutObject]
    schema_version: int = SCHEMA_VERSION
    arrangement: str = "design"
    workspaces: dict[str, WorkspaceState] = field(default_factory=dict)
    styles: list[Style] = field(default_factory=list)
    variables: dict[str, dict[str, Any]] = field(default_factory=dict)
    assets: list[Asset] = field(default_factory=list)
    connections: list[PrototypeConnection] = field(default_factory=list)
    selected_ids: list[str] = field(default_factory=list)
    canvas_x: float = 0.0
    canvas_y: float = 0.0
    zoom: float = 1.0
    # What new objects are drawn with (spec 6.2). Editing a selection's paint
    # updates it; the style chip at the foot of the tool rail shows it.
    current_style: dict[str, Any] = field(default_factory=default_current_style)
    pages: list[Page] = field(default_factory=list)
    current_page_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    modified_at: str = field(default_factory=utc_now)
    revision: int = 0
    embedded_assets: dict[str, bytes] = field(default_factory=dict, repr=False)

    @classmethod
    def empty(cls, name: str = "Untitled") -> "Document":
        document = cls(id=new_id("document"), name=name, objects=[])
        document.workspaces = {name: WorkspaceState() for name in sorted(ARRANGEMENTS)}
        document.validate()
        return document

    # ── pages ────────────────────────────────────────────────────────────

    def ensure_pages(self) -> Page:
        """Documents from before pages get one page holding their layers."""
        if not self.pages:
            page = Page(new_id("page"), "Page 1")
            self.pages.append(page)
            self.current_page_id = page.id
        if self.current_page() is None:
            self.current_page_id = self.pages[0].id
        return self.current_page()  # type: ignore[return-value]

    def current_page(self) -> Page | None:
        return next((page for page in self.pages if page.id == self.current_page_id), None)

    def page_index(self) -> int:
        page = self.current_page()
        return self.pages.index(page) if page is not None else 0

    def show_page(self, page_id: str) -> bool:
        """Swap the shown page's layers into `objects` (no history of its own)."""
        self.ensure_pages()
        target = next((page for page in self.pages if page.id == page_id), None)
        current = self.current_page()
        if target is None or target is current:
            return False
        current.objects = [asdict(item) for item in self.objects]
        current.view = {"zoom": self.zoom, "x": self.canvas_x, "y": self.canvas_y}
        current.selected_ids = list(self.selected_ids)
        self.objects = [_object_from_dict(copy.deepcopy(value)) for value in target.objects]
        target.objects = []
        self.current_page_id = target.id
        if target.view:
            self.zoom = float(target.view.get("zoom", self.zoom))
            self.canvas_x = float(target.view.get("x", self.canvas_x))
            self.canvas_y = float(target.view.get("y", self.canvas_y))
        known = {item.id for item in self.objects}
        self.selected_ids = [value for value in target.selected_ids if value in known]
        return True

    def all_objects(self) -> list[LayoutObject]:
        """Every page's layers (the shown page's are live objects)."""
        result = list(self.objects)
        for page in self.pages:
            if page.id != self.current_page_id:
                result.extend(_object_from_dict(copy.deepcopy(value)) for value in page.objects)
        return result

    def object_on_any_page(self, object_id: str | None) -> LayoutObject | None:
        """Read one definition without materializing every inactive page."""
        active = self.object(object_id)
        if active is not None:
            return active
        for page in self.pages:
            if page.id != self.current_page_id:
                for value in page.objects:
                    if value['id'] == object_id:
                        return _object_from_dict(copy.deepcopy(value))
        return None

    def object(self, object_id: str | None) -> LayoutObject | None:
        """A layer by id, through a position index kept per objects list.

        A hit is checked against the list itself, so a stale index is rebuilt
        rather than trusted."""
        if object_id is None:
            return None
        objects = self.objects
        entry = _OBJECT_INDEX.get(id(objects))
        if entry is not None and entry[0] is objects:
            position = entry[1].get(object_id)
            if position is not None and position < len(objects) and objects[position].id == object_id:
                return objects[position]
            if position is None and len(entry[1]) == len(objects):
                # Not in an index of a list that neither grew nor shrank. Layers
                # are added and removed, never swapped into a slot, so it is
                # not in the document (a lookup of a foreign id stays cheap).
                return None
        index = {item.id: position for position, item in enumerate(objects)}
        if len(_OBJECT_INDEX) >= 4:
            _OBJECT_INDEX.clear()
        _OBJECT_INDEX[id(objects)] = (objects, index)
        position = index.get(object_id)
        return objects[position] if position is not None else None

    def children(self, parent_id: str | None) -> list[LayoutObject]:
        return [item for item in self.objects if item.parent_id == parent_id]

    def descendants(self, parent_id: str) -> list[LayoutObject]:
        grouped: dict[str | None, list[LayoutObject]] = {}
        for item in self.objects:
            grouped.setdefault(item.parent_id, []).append(item)
        result: list[LayoutObject] = []
        pending = [parent_id]
        while pending:
            children = grouped.get(pending.pop(), [])
            result.extend(children)
            pending.extend(child.id for child in children)
        return result

    def ancestors(self, object_id: str) -> list[LayoutObject]:
        result: list[LayoutObject] = []
        current = self.object(object_id)
        while current and current.parent_id:
            current = self.object(current.parent_id)
            if current:
                result.append(current)
        return result

    def select(self, object_ids: Iterable[str]) -> None:
        known = {item.id for item in self.objects}
        self.selected_ids = list(dict.fromkeys(item for item in object_ids if item in known))

    @property
    def selection(self) -> list[LayoutObject]:
        selected = set(self.selected_ids)
        return [item for item in self.objects if item.id in selected]

    def touch(self) -> None:
        self.modified_at = utc_now()
        self.revision += 1

    def validate(self, changed: Iterable[str] | None = None, *, removed: Iterable[str] = (),
                 reordered: bool = True) -> None:
        """Check the whole document, or with `changed` only what an edit could
        have broken: the changed layers, layers left inside removed ones, and
        document-wide rules when layers were added, removed or reordered. An
        edit on a document of tens of thousands of layers checks what it
        touched."""
        if self.schema_version > SCHEMA_VERSION:
            raise UnsupportedDocumentVersion(
                f"document schema {self.schema_version} is newer than supported {SCHEMA_VERSION}"
            )
        if self.schema_version < 1:
            raise InvalidDocument("invalid document schema version")
        if self.arrangement not in ARRANGEMENTS:
            raise InvalidDocument("unknown workspace arrangement")
        if set(self.workspaces) != ARRANGEMENTS:
            raise InvalidDocument("every workspace arrangement needs independent panel state")
        for state in self.workspaces.values():
            state.validate()
        if changed is not None:
            self._validate_changed(set(changed), set(removed), reordered)
            return
        ids = [item.id for item in self.objects]
        if len(ids) != len(set(ids)):
            raise InvalidDocument("object IDs must be unique")
        object_ids = set(ids)
        # One index for every lookup below: a document of tens of thousands of
        # layers (an imported Figma file) validates in linear time.
        by_id = {item.id: item for item in self.objects}
        # Other pages' layers can be the far end of a component, a prototype
        # link or linked text.
        elsewhere = {value.get("id"): value.get("kind") for page in self.pages
                     if page.id != self.current_page_id for value in page.objects}
        all_ids = object_ids | set(elsewhere)
        style_ids = {style.id for style in self.styles}
        asset_ids = {asset.id for asset in self.assets}
        for item in self.objects:
            item.validate()
            if item.parent_id is not None:
                parent = by_id.get(item.parent_id)
                if parent is None:
                    raise InvalidDocument(f"{item.name} references a missing parent")
                if parent.kind not in CONTAINER_KINDS:
                    raise InvalidDocument(f"{item.name} cannot be nested in {parent.kind}")
            if item.component_source_id:
                source = by_id.get(item.component_source_id)
                kind = source.kind if source is not None else elsewhere.get(item.component_source_id)
                if kind != "component":
                    raise InvalidDocument(f"{item.name} references a missing component source")
            if item.text_flow_next_id:
                target = by_id.get(item.text_flow_next_id)
                kind = target.kind if target is not None else elsewhere.get(item.text_flow_next_id)
                if item.kind != "text" or kind != "text":
                    raise InvalidDocument(f"{item.name} has an invalid linked-text target")
            if item.mask_id and (by_id.get(item.mask_id) is None or by_id.get(item.mask_id).kind != "mask"):
                raise InvalidDocument(f"{item.name} has an invalid mask")
            if item.asset_id and item.asset_id not in asset_ids:
                raise InvalidDocument(f"{item.name} references a missing asset")
            if not set(item.style_ids).issubset(style_ids):
                raise InvalidDocument(f"{item.name} references a missing style")
            seen = {item.id}
            cursor = item
            while cursor.parent_id:
                if cursor.parent_id in seen:
                    raise InvalidDocument("object hierarchy contains a cycle")
                seen.add(cursor.parent_id)
                parent = by_id.get(cursor.parent_id)
                if parent is None:
                    break
                cursor = parent
        for style in self.styles:
            style.validate()
        for asset in self.assets:
            asset.validate()
        for connection in self.connections:
            connection.validate(all_ids)
        if not set(self.selected_ids).issubset(object_ids):
            raise InvalidDocument("selection contains a missing object")
        if not ZOOM_MIN <= self.zoom <= ZOOM_MAX:
            raise InvalidDocument("zoom is outside 1%…25600%")

    def _validate_changed(self, changed: set[str], removed: set[str], reordered: bool) -> None:
        objects = self.objects
        if reordered:
            ids = [item.id for item in objects]
            if len(ids) != len(set(ids)):
                raise InvalidDocument("object IDs must be unique")
        if removed:
            # A layer removed while something still sits inside it.
            for item in objects:
                if item.parent_id in removed:
                    raise InvalidDocument(f"{item.name} references a missing parent")
        elsewhere: dict[Any, Any] | None = None

        def kind_elsewhere(object_id: str) -> Any:
            nonlocal elsewhere
            if elsewhere is None:
                elsewhere = {value.get("id"): value.get("kind") for page in self.pages
                             if page.id != self.current_page_id for value in page.objects}
            return elsewhere.get(object_id)

        style_ids = {style.id for style in self.styles}
        asset_ids = {asset.id for asset in self.assets}
        for object_id in changed:
            item = self.object(object_id)
            if item is None:
                continue
            item.validate()
            if item.parent_id is not None:
                parent = self.object(item.parent_id)
                if parent is None:
                    raise InvalidDocument(f"{item.name} references a missing parent")
                if parent.kind not in CONTAINER_KINDS:
                    raise InvalidDocument(f"{item.name} cannot be nested in {parent.kind}")
            if item.component_source_id:
                source = self.object(item.component_source_id)
                kind = source.kind if source is not None else kind_elsewhere(item.component_source_id)
                if kind != "component":
                    raise InvalidDocument(f"{item.name} references a missing component source")
            if item.text_flow_next_id:
                target = self.object(item.text_flow_next_id)
                kind = target.kind if target is not None else kind_elsewhere(item.text_flow_next_id)
                if item.kind != "text" or kind != "text":
                    raise InvalidDocument(f"{item.name} has an invalid linked-text target")
            if item.mask_id:
                mask = self.object(item.mask_id)
                if mask is None or mask.kind != "mask":
                    raise InvalidDocument(f"{item.name} has an invalid mask")
            if item.asset_id and item.asset_id not in asset_ids:
                raise InvalidDocument(f"{item.name} references a missing asset")
            if not set(item.style_ids).issubset(style_ids):
                raise InvalidDocument(f"{item.name} references a missing style")
            seen = {item.id}
            cursor = item
            while cursor.parent_id:
                if cursor.parent_id in seen:
                    raise InvalidDocument("object hierarchy contains a cycle")
                seen.add(cursor.parent_id)
                cursor = self.object(cursor.parent_id)
                if cursor is None:
                    break
        for style in self.styles:
            style.validate()
        for asset in self.assets:
            asset.validate()
        if self.connections and (removed or reordered):
            all_ids = {item.id for item in objects}
            if elsewhere is None:
                kind_elsewhere("")
            all_ids |= set(elsewhere or {})
            for connection in self.connections:
                connection.validate(all_ids)
        for object_id in self.selected_ids:
            if self.object(object_id) is None:
                raise InvalidDocument("selection contains a missing object")
        if not ZOOM_MIN <= self.zoom <= ZOOM_MAX:
            raise InvalidDocument("zoom is outside 1%…25600%")

    def fields_dict(self) -> dict[str, Any]:
        """The document's own fields as plain data: everything `to_dict` gives
        except the layers (an independent copy)."""
        value: dict[str, Any] = {}
        for entry in fields(self):
            name = entry.name
            if name in {"embedded_assets", "objects"}:
                continue
            item = getattr(self, name)
            if name in {"styles", "assets", "connections", "pages"}:
                item = [_shallow(part) for part in item]
            elif name == "workspaces":
                item = {key: _shallow(part) for key, part in item.items()}
            value[name] = item
        return pickle.loads(pickle.dumps(value, pickle.HIGHEST_PROTOCOL))

    def to_dict(self) -> dict[str, Any]:
        """A plain, independent copy of the document (what `asdict` gives).

        Built shallowly and copied in one pickle round trip, which is several
        times faster than `asdict` on documents with tens of thousands of
        layers (an imported Figma file), and snapshots are taken often.
        """
        value: dict[str, Any] = {}
        for entry in fields(self):
            name = entry.name
            if name == "embedded_assets":
                continue
            item = getattr(self, name)
            if name == "objects":
                item = [{key: getattr(layer, key) for key in _OBJECT_FIELDS} for layer in item]
            elif name in {"styles", "assets", "connections", "pages"}:
                item = [_shallow(part) for part in item]
            elif name == "workspaces":
                item = {key: _shallow(part) for key, part in item.items()}
            value[name] = item
        return pickle.loads(pickle.dumps(value, pickle.HIGHEST_PROTOCOL))

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, copy_input: bool = True) -> "Document":
        """The document `value` describes.

        With `copy_input=False` the caller promises not to keep changing
        `value`: its lists and dicts are used as they are instead of being
        copied, which is most of the work of reading a large document. Layers
        are never changed in place (a new list or dict is assigned), so the
        two stay apart as they are edited.
        """
        schema = int(value.get("schema_version", 0))
        if schema > SCHEMA_VERSION:
            raise UnsupportedDocumentVersion(
                f"document schema {schema} is newer than supported {SCHEMA_VERSION}"
            )
        data = copy.deepcopy(value) if copy_input else dict(value)
        data["objects"] = [_object_from_dict(item) for item in data.get("objects", [])]
        data["styles"] = [Style(**item) for item in data.get("styles", [])]
        data["assets"] = [Asset(**item) for item in data.get("assets", [])]
        data["connections"] = [PrototypeConnection(**item) for item in data.get("connections", [])]
        data["workspaces"] = {
            key: WorkspaceState(**state) for key, state in data.get("workspaces", {}).items()
        }
        data["current_style"] = normalized_style(data.get("current_style"))
        data["pages"] = [Page(**page) for page in data.get("pages", [])]
        # Schema 1 layers gained their paint stacks as they were read.
        data["schema_version"] = SCHEMA_VERSION
        document = cls(**data)
        migrate_group_paint(document)
        document.validate()
        return document

    @classmethod
    def demonstration(cls) -> "Document":
        doc = cls.empty("Brand")
        accent_style = Style(new_id("style"), "Meadow rose", "color", {"value": "#bb536f"})
        ink = Style(new_id("style"), "Ink", "color", {"value": "#f4f3f0"})
        heading = Style(new_id("style"), "Headline / 44", "text", {"family": "Figtree", "size": 44, "weight": 700})
        doc.styles.extend([accent_style, ink, heading])
        doc.variables = {
            "Brand": {
                "modes": ["light", "dark"],
                "values": {"surface": {"light": "#f4f3f0", "dark": "#28323b"}, "accent": "#bb536f"},
            }
        }

        brand_board = LayoutObject(new_id("frame"), "frame", "Brand · 1200 × 800", x=120, y=80, width=1200, height=800, fill="#f4f3f0")
        background = LayoutObject(new_id("rectangle"), "rectangle", "Background", brand_board.id, 120, 80, 1200, 800, fill="#f4f3f0")
        meadow = LayoutObject(new_id("image"), "image", "Meadow photograph", brand_board.id, 120, 80, 1200, 430, fill=None, metadata={"preview": "meadow", "alt": "Abstract folded green meadow"})
        plate = LayoutObject(new_id("rectangle"), "rectangle", "Plate", brand_board.id, 198, 408, 424, 196, fill="#28323b", radius=14)
        headline = LayoutObject(
            new_id("text"), "text", "Headline", brand_board.id,
            230, 444, 352, 150, fill="#f4f3f0", text="One system.\nEvery screen.", style_ids=[heading.id],
            metadata={"font_family": "Figtree", "font_size": 44, "font_weight": 700, "leading": 105, "tracking": -35},
        )
        standfirst = LayoutObject(
            new_id("text"), "text", "Standfirst", brand_board.id,
            230, 560, 320, 30, fill="#b6c1c9", text="A design language under it.",
            metadata={"font_family": "Figtree", "font_size": 10, "font_weight": 400},
        )
        accent = LayoutObject(new_id("ellipse"), "ellipse", "Accent", brand_board.id, 682, 552, 118, 118, fill="#bb536f", style_ids=[accent_style.id])
        rule = LayoutObject(new_id("rectangle"), "rectangle", "Rule", brand_board.id, 198, 742, 1044, 2, fill="#d8d5d0")

        # The remaining objects keep the seed honest across design, vector,
        # component, prototype, and publication work without crowding the
        # initially focused Brand artboard.
        component_shelf = LayoutObject(new_id("frame"), "frame", "Component shelf", x=1540, y=80, width=620, height=360, fill="#f7f8fb", radius=18)
        nav = LayoutObject(new_id("layout"), "layout", "Navigation / Auto layout", component_shelf.id, 1580, 118, 540, 58, fill="#ffffff", radius=14, layout={"direction": "horizontal", "gap": 12, "padding": 14, "sizing": "fill"})
        wordmark = LayoutObject(new_id("text"), "text", "Brand wordmark", nav.id, 1600, 136, 120, 24, fill="#18202a", text="LUMA", style_ids=[ink.id], metadata={"font_size": 14, "font_weight": 700})
        button = LayoutObject(new_id("component"), "component", "Button / Primary", component_shelf.id, 1580, 230, 148, 46, fill="#5e91d2", radius=12, layout={"direction": "horizontal", "gap": 8, "padding": 14}, component_variant="default")
        button_label = LayoutObject(new_id("text"), "text", "Button label", button.id, 1594, 244, 124, 18, fill="#ffffff", text="Start a project", metadata={"font_size": 14})
        button_hover = LayoutObject(new_id("component"), "component", "Button / Primary / Hover", x=1800, y=230, width=148, height=46, fill="#4475b4", radius=12, component_variant="hover", metadata={"variant_of": button.id})
        instance = LayoutObject(new_id("instance"), "instance", "Button instance", component_shelf.id, 1930, 230, 148, 46, fill="#5e91d2", radius=12, component_source_id=button.id, component_variant="default")

        mobile = LayoutObject(new_id("frame"), "frame", "Brand · Mobile", x=2260, y=80, width=390, height=720, fill="#f4f3f0", radius=28)
        mobile_title = LayoutObject(new_id("text"), "text", "Mobile title", mobile.id, 2290, 150, 320, 110, fill="#28323b", text="One system.\nEvery screen.", style_ids=[heading.id], metadata={"font_size": 40, "font_weight": 700})
        mobile_instance = LayoutObject(new_id("instance"), "instance", "Mobile CTA", mobile.id, 2310, 660, 300, 48, fill="#5e91d2", radius=12, component_source_id=button.id)

        icon_path = LayoutObject(
            new_id("path"), "path", "Editable compass icon", x=2780, y=110, width=96, height=96,
            fill="#5e91d2", stroke="#18202a", stroke_width=2,
            path=[
                {"x": 48.0, "y": 4.0, "type": "smooth"},
                {"x": 88.0, "y": 48.0, "type": "corner"},
                {"x": 48.0, "y": 92.0, "type": "smooth"},
                {"x": 8.0, "y": 48.0, "type": "corner", "closed": True},
            ],
        )

        poster = LayoutObject(new_id("frame"), "frame", "Brand launch poster", x=120, y=1680, width=520, height=700, fill="#17202b")
        poster_shape = LayoutObject(new_id("ellipse"), "ellipse", "Masked orbital image", poster.id, 196, 1768, 370, 370, fill="#5e91d2")
        poster_mask = LayoutObject(new_id("mask"), "mask", "Orbital crop", poster.id, 196, 1768, 370, 370, fill="#ffffff")
        poster_shape.mask_id = poster_mask.id
        poster_title = LayoutObject(new_id("text"), "text", "Poster headline", poster.id, 166, 2180, 420, 120, fill="#ffffff", text="IDEAS NEED\nROOM TO ORBIT", style_ids=[heading.id], metadata={"font_size": 42, "font_weight": 700})

        spread = LayoutObject(new_id("spread"), "spread", "Editorial spread 4–5", x=760, y=1680, width=940, height=610, fill=None)
        page_left = LayoutObject(new_id("page"), "page", "Page 4", spread.id, 760, 1680, 460, 610, fill="#fffdf8", metadata={"page_number": 4, "master": "Feature"})
        page_right = LayoutObject(new_id("page"), "page", "Page 5", spread.id, 1240, 1680, 460, 610, fill="#fffdf8", metadata={"page_number": 5, "master": "Feature"})
        article_a = LayoutObject(new_id("text"), "text", "Story · opening", page_left.id, 806, 1766, 360, 420, fill="#18202a", text="A slower orbit\n\nDesign systems become useful when they leave room for judgment, rhythm, and surprise.", metadata={"columns": 2, "overflow": False, "font_size": 15})
        article_b = LayoutObject(new_id("text"), "text", "Story · continued", page_right.id, 1286, 1766, 360, 420, fill="#18202a", text="The system continues here, visibly linked from the previous page.", metadata={"columns": 2, "overflow": False, "font_size": 15})
        article_a.text_flow_next_id = article_b.id

        doc.objects.extend([
            brand_board, background, meadow, plate, headline, standfirst, accent, rule,
            component_shelf, nav, wordmark, button, button_label, instance, button_hover,
            mobile, mobile_title, mobile_instance, icon_path,
            poster, poster_shape, poster_mask, poster_title,
            spread, page_left, page_right, article_a, article_b,
        ])
        doc.connections.append(PrototypeConnection(new_id("connection"), instance.id, mobile.id, "click", "smart-animate", 280))
        doc.select([headline.id])
        doc.validate()
        return doc
