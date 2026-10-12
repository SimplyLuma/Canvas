# SPDX-License-Identifier: MPL-2.0
"""The Layers panel: a virtualized layer tree that updates in place.

CANVAS-SPEC 11.1 and 14.4. A `Gtk.ListView` over a `Gtk.TreeListModel` of the
page, top-most layer first. Each layer is one `LayerItem` for its whole life:
a rename, a hide or a lock changes that item's properties and its row updates;
only a structural change (add, remove, reorder, reparent) touches the stores,
and then only the stretch that moved. Rows are never rebuilt on a property
change, so focus, hover and scroll position survive editing.

The ordering and diffing rules are plain functions at the top of this module
so they can be tested without a display.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable, Sequence

from .model import CONTAINER_KINDS, Document

# Kinds that never paint on their own and stay out of the tree.
HIDDEN_KINDS = frozenset({"guide", "comment"})

KIND_ICONS = {
    "frame": "luma-canvas-frame-symbolic",
    "page": "luma-canvas-frame-symbolic",
    "section": "luma-canvas-frame-symbolic",
    "spread": "luma-canvas-frame-symbolic",
    "group": "luma-canvas-group-symbolic",
    "layout": "luma-canvas-group-symbolic",
    "mask": "luma-canvas-group-symbolic",
    "component": "luma-canvas-component-symbolic",
    "instance": "luma-canvas-component-symbolic",
    "rectangle": "luma-canvas-rectangle-symbolic",
    "ellipse": "luma-canvas-ellipse-symbolic",
    "polygon": "luma-canvas-polygon-symbolic",
    "star": "luma-canvas-star-symbolic",
    "line": "luma-canvas-line-symbolic",
    "path": "luma-canvas-pen-symbolic",
    "vector": "luma-canvas-pen-symbolic",
    "boolean": "luma-canvas-boolean-union-symbolic",
    "text": "luma-canvas-text-symbolic",
    "image": "luma-canvas-image-symbolic",
}

KIND_NAMES = {
    "frame": "Frame", "page": "Page", "section": "Section", "spread": "Spread",
    "group": "Group", "layout": "Group", "mask": "Mask", "component": "Component",
    "instance": "Instance", "rectangle": "Rectangle", "ellipse": "Ellipse",
    "polygon": "Polygon", "star": "Star", "line": "Line", "path": "Vector",
    "vector": "Vector", "text": "Text", "image": "Image", "boolean": "Boolean",
}


def masked_layers(siblings: Sequence) -> set[str]:
    """Siblings drawn through a mask below them (paint order in, ids out)."""
    masked: set[str] = set()
    active = False
    for item in siblings:
        if active:
            masked.add(item.id)
        if (item.metadata or {}).get("mask") and item.visible:
            active = True
    return masked


def layer_icon(layer, kind: str) -> str:
    """The row glyph: booleans show their operation."""
    if layer is not None and kind == "boolean":
        operation = (layer.metadata or {}).get("boolean_operation") or "union"
        return f"luma-canvas-boolean-{operation}-symbolic"
    return KIND_ICONS.get(kind, "luma-canvas-shape-symbolic")


def children_by_parent(document: Document) -> dict[str | None, list]:
    """Children of each parent in paint order (last paints on top)."""
    known = {item.id for item in document.objects}
    grouped: dict[str | None, list] = defaultdict(list)
    for item in document.objects:
        if item.kind in HIDDEN_KINDS:
            continue
        parent = item.parent_id if item.parent_id in known else None
        grouped[parent].append(item)
    return grouped


def layer_rows(document: Document, expanded: Iterable[str] | None = None) -> list[tuple[str, int]]:
    """The visible rows as (id, depth), top-most first; children of
    `expanded` containers follow their parent. `None` expands everything."""
    grouped = children_by_parent(document)
    expanded_set = None if expanded is None else set(expanded)
    rows: list[tuple[str, int]] = []

    def walk(parent: str | None, depth: int) -> None:
        for item in reversed(grouped.get(parent, [])):
            rows.append((item.id, depth))
            if item.kind in CONTAINER_KINDS and (expanded_set is None or item.id in expanded_set):
                walk(item.id, depth + 1)

    walk(None, 0)
    return rows


def focus_scope(document: Document, selected_ids: Iterable[str]) -> tuple[set[str], set[str]]:
    """The containers the selection is working in, and every layer inside them.

    A selected frame or group is its own scope; any other layer's scope is its
    nearest container. Rows outside every scope are dimmed in the tree so the
    work in hand stands out. Nothing selected means no scope.
    """
    roots: set[str] = set()
    for object_id in selected_ids:
        item = document.object(object_id)
        if item is None:
            continue
        if item.kind in CONTAINER_KINDS:
            roots.add(item.id)
            continue
        parent = document.object(item.parent_id) if item.parent_id else None
        if parent is not None:
            roots.add(parent.id)
        else:
            roots.add(item.id)
    members: set[str] = set(roots)
    if not roots:
        return roots, members
    # One pass over the layers for every root's contents (a huge document has
    # tens of thousands; walking them once per level would stall selection).
    grouped: dict[str | None, list[str]] = defaultdict(list)
    for item in document.objects:
        grouped[item.parent_id].append(item.id)
    for root in roots:
        pending = [root]
        while pending:
            children = grouped.get(pending.pop(), ())
            members.update(children)
            pending.extend(children)
        members.update(ancestor.id for ancestor in document.ancestors(root))
    return roots, members


def splice_plan(current: Sequence[str], desired: Sequence[str],
                force: Iterable[str] = ()) -> tuple[int, int, list[str]] | None:
    """The one splice that turns `current` into `desired`: (position, number
    removed, ids added), or None when they already match. Ids in `force` are
    kept out of the shared prefix and suffix so their rows are recreated."""
    forced = set(force)
    if list(current) == list(desired) and not forced.intersection(current):
        return None
    start = 0
    limit = min(len(current), len(desired))
    while start < limit and current[start] == desired[start] and current[start] not in forced:
        start += 1
    end_current, end_desired = len(current), len(desired)
    while (end_current > start and end_desired > start
           and current[end_current - 1] == desired[end_desired - 1]
           and current[end_current - 1] not in forced):
        end_current -= 1
        end_desired -= 1
    return start, end_current - start, list(desired[start:end_desired])


def drop_target(document: Document, moving: Sequence[str], target_id: str,
                zone: str) -> tuple[str | None, str | None] | None:
    """Where a drop on a row puts the moved layers, as (parent id, before id)
    for `Editor.reorder`, or None when the drop is not allowed.

    `zone` is "above" or "below" the target row (the tree shows top-most first,
    so "above" means painting over it) or "into" a container (on top of its
    children).
    """
    target = document.object(target_id)
    if target is None or target_id in moving:
        return None
    for object_id in moving:
        if object_id == target_id or any(parent.id == object_id for parent in document.ancestors(target_id)):
            return None
    if zone == "into":
        if target.kind not in CONTAINER_KINDS:
            return None
        return target.id, None
    parent = target.parent_id
    siblings = [item for item in document.objects if item.parent_id == parent and item.id not in moving]
    index = next(i for i, item in enumerate(siblings) if item.id == target_id)
    if zone == "below":
        return parent, target_id
    # Above the target in the tree means after it in paint order.
    following = siblings[index + 1] if index + 1 < len(siblings) else None
    return parent, following.id if following else None


# ── GTK ─────────────────────────────────────────────────────────────────

try:
    import gi  # noqa: F401
    from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango
except (ImportError, ValueError):  # pure helpers above stay importable for tests
    Gtk = None  # type: ignore[assignment]


if Gtk is not None:
    from luma_appkit import InlineRenameField

    LAYER_PROPS = ("name", "kind", "shown", "locked", "mask", "masked")

    class LayerItem(GObject.Object):
        __gtype_name__ = "CanvasLayerItem"

        name = GObject.Property(type=str, default="")
        kind = GObject.Property(type=str, default="")
        shown = GObject.Property(type=bool, default=True)
        locked = GObject.Property(type=bool, default=False)
        mask = GObject.Property(type=bool, default=False)
        masked = GObject.Property(type=bool, default=False)

        def __init__(self, object_id: str, **properties) -> None:
            super().__init__(**properties)
            self.object_id = object_id
            # Plain mirrors of the properties, so a sync compares Python
            # values instead of crossing into GObject for every layer.
            self.values = (properties.get("name", ""), properties.get("kind", ""),
                           properties.get("shown", True), properties.get("locked", False),
                           properties.get("mask", False), properties.get("masked", False))

    class ChildLayers(GObject.Object, Gio.ListModel):
        """The layers inside one container (or the page), top-most first.

        A row's item is made when the list reads it, so the thousands of layers
        inside collapsed frames of a huge document cost nothing until shown."""

        __gtype_name__ = "CanvasChildLayers"

        def __init__(self, owner: "LayersModel", parent: str | None) -> None:
            super().__init__()
            self.owner = owner
            self.parent = parent
            self.ids: list[str] = owner._ids_for(parent)
            # Once the tree has read this list it must hear about every change.
            self.observed = False

        def do_get_item_type(self):
            return LayerItem.__gtype__

        def do_get_n_items(self) -> int:
            self.observed = True
            return len(self.ids)

        def do_get_item(self, position: int):
            self.observed = True
            if 0 <= position < len(self.ids):
                return self.owner.item(self.ids[position])
            return None

    class LayerItems(dict):
        """Rows made so far, by layer id. Reading a layer that has no row yet
        makes it; `in` is true for every layer in the tree."""

        def __init__(self, owner: "LayersModel") -> None:
            super().__init__()
            self.owner = owner

        def __missing__(self, object_id: str) -> "LayerItem":
            item = self.owner.item(object_id)
            if item is None:
                raise KeyError(object_id)
            return item

        def __contains__(self, object_id: object) -> bool:
            return dict.__contains__(self, object_id) or object_id in self.owner._layers

    class LayersModel:
        """One `LayerItem` per layer the list has shown, updated in place;
        structural changes splice only the stretch that moved, and only in
        lists the tree has read."""

        def __init__(self) -> None:
            self._grouped: dict[str | None, list] = {}
            self._layers: dict[str, object] = {}
            self._masked: dict[str | None, set[str]] = {}
            # Containers the tree asked about, and whether they could expand
            # then (GTK fixes a row's expandability until it is re-inserted).
            self._asked: dict[str, bool] = {}
            self.items = LayerItems(self)
            self._models: dict[str | None, ChildLayers] = {}
            self.root = self._model_for(None)
            self.tree = Gtk.TreeListModel.new(self.root, False, False, self._children)

        def _ids_for(self, parent: str | None) -> list[str]:
            return [layer.id for layer in reversed(self._grouped.get(parent, []))]

        def _model_for(self, parent: str | None) -> "ChildLayers":
            model = self._models.get(parent)
            if model is None:
                model = self._models[parent] = ChildLayers(self, parent)
            return model

        def _parent_of(self, layer) -> str | None:
            return layer.parent_id if layer.parent_id in self._layers else None

        def _expandable(self, object_id: str) -> bool:
            layer = self._layers.get(object_id)
            return layer is not None and layer.kind in CONTAINER_KINDS and bool(self._grouped.get(object_id))

        def _children(self, item: "LayerItem"):
            expandable = self._expandable(item.object_id)
            self._asked[item.object_id] = expandable
            return self._model_for(item.object_id) if expandable else None

        def _masked_in(self, parent: str | None) -> set[str]:
            masked = self._masked.get(parent)
            if masked is None:
                masked = self._masked[parent] = masked_layers(self._grouped.get(parent, []))
            return masked

        def _values(self, layer) -> tuple:
            return (layer.name, layer.kind, layer.visible, layer.locked, bool((layer.metadata or {}).get("mask")),
                    layer.id in self._masked_in(self._parent_of(layer)))

        def item(self, object_id: str) -> "LayerItem | None":
            """The row item for a layer, made now if it has none yet."""
            existing = dict.get(self.items, object_id)
            if existing is not None:
                return existing
            layer = self._layers.get(object_id)
            if layer is None:
                return None
            values = self._values(layer)
            created = LayerItem(object_id, **dict(zip(LAYER_PROPS, values)))
            dict.__setitem__(self.items, object_id, created)
            return created

        def sync(self, document: Document) -> None:
            self._grouped = children_by_parent(document)
            self._layers = {layer.id: layer for siblings in self._grouped.values() for layer in siblings}
            self._masked = {}
            for object_id, item in list(dict.items(self.items)):
                layer = self._layers.get(object_id)
                if layer is None:
                    dict.__delitem__(self.items, object_id)
                    continue
                values = self._values(layer)
                if item.values != values:
                    old_values, item.values = item.values, values
                    # The mirror first: rows repaint from it on each notify.
                    for prop, old, new in zip(LAYER_PROPS, old_values, values):
                        if old != new:
                            item.set_property(prop, new)
            forced: dict[str | None, set[str]] = {}
            for object_id, asked in list(self._asked.items()):
                if self._expandable(object_id) != asked:
                    del self._asked[object_id]
                    layer = self._layers.get(object_id)
                    if layer is not None:
                        forced.setdefault(self._parent_of(layer), set()).add(object_id)
            for parent, model in list(self._models.items()):
                wanted = [] if parent is not None and parent not in self._layers else self._ids_for(parent)
                plan = splice_plan(model.ids, wanted, forced.get(parent, ()))
                if plan is None:
                    continue
                position, removed, added = plan
                model.ids = wanted
                if model.observed:
                    model.items_changed(position, removed, len(added))
                elif parent is not None and not wanted:
                    del self._models[parent]

        def rows(self) -> Iterable[tuple[int, "Gtk.TreeListRow"]]:
            for position in range(self.tree.get_n_items()):
                yield position, self.tree.get_row(position)

        def row_of(self, object_id: str, *, expand: bool = False) -> "Gtk.TreeListRow | None":
            """A layer's tree row, found through its ancestors' rows (expanding
            them when `expand`), never by scanning every row."""
            layer = self._layers.get(object_id)
            if layer is None:
                return None
            chain: list[str] = []
            cursor = layer
            while cursor is not None:
                chain.append(cursor.id)
                parent_id = self._parent_of(cursor)
                cursor = self._layers.get(parent_id) if parent_id is not None else None
            chain.reverse()
            row = None
            for depth, current in enumerate(chain):
                model = self._model_for(chain[depth - 1] if depth else None)
                try:
                    index = model.ids.index(current)
                except ValueError:
                    return None
                row = self.tree.get_child_row(index) if row is None else row.get_child_row(index)
                if row is None:
                    return None
                if depth < len(chain) - 1 and not row.get_expanded():
                    if not expand or not row.is_expandable():
                        return None
                    row.set_expanded(True)
            return row

        def position_of(self, object_id: str) -> int | None:
            row = self.row_of(object_id)
            return row.get_position() if row is not None else None

        def reveal(self, document: Document, object_id: str) -> int | None:
            """Expand every ancestor of a layer and return its row."""
            row = self.row_of(object_id, expand=True)
            return row.get_position() if row is not None else None

    class LayersPanel(Gtk.Box):
        """Layers and Assets tabs, the layer tree, and the pages group.

        The panel sits on the window ground, not in an island. It knows nothing
        about editing: the window passes callbacks.
        TODO(kit-request canvas-02-creative-layer-tree): adapt model rows to
        LayerTree and remove the old visual row rules.
        """

        __gtype_name__ = "CanvasLayersPanel"

        def __init__(self, *, on_select: Callable[[list[str]], None],
                     on_rename: Callable[[str, str], bool],
                     on_toggle: Callable[[str, list[str], bool], None],
                     on_reorder: Callable[[list[str], str, str], None],
                     on_hover: Callable[[str | None], None],
                     on_context: Callable[[Gtk.Widget, float, float, str | None], None],
                     on_asset_color: Callable[[str], None],
                     on_asset_text: Callable[[str], None],
                     segments: Callable, context) -> None:
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self.add_css_class("canvas-layers-panel")
            self.on_select, self.on_rename, self.on_toggle = on_select, on_rename, on_toggle
            self.on_reorder, self.on_hover, self.on_context = on_reorder, on_hover, on_context
            self.on_asset_color, self.on_asset_text = on_asset_color, on_asset_text
            self.document: Document | None = None
            self._syncing = False
            self._renaming: tuple[str, Gtk.Stack, Gtk.Label] | None = None
            self._sweep: dict | None = None
            self._expand_timer = 0
            self.rows_by_id: dict[str, Gtk.Widget] = {}
            self.row_widgets: dict[str, dict] = {}
            self._scope_roots: set[str] = set()
            self._scope_members: set[str] = set()

            tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            tabs.add_css_class("canvas-panel-tabs-row")
            self.tabs, self.tab_buttons = segments((("layers", "Layers"), ("assets", "Assets")), "layers",
                                                   self._tab_changed)
            tabs.append(self.tabs)
            self.append(tabs)

            self.search = Gtk.SearchEntry(placeholder_text="Find layers")
            self.search.add_css_class("canvas-layers-search")
            self.search.set_visible(False)
            self.search.connect("search-changed", lambda *_: self._search_changed())
            self.search.connect("stop-search", lambda *_: self.hide_search())
            self.append(self.search)

            self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.NONE)
            self.append(self.stack)

            # Layers tab: the tree, then the pages group pinned at the bottom.
            layers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.model = LayersModel()
            self.selection = Gtk.MultiSelection.new(self.model.tree)
            self.selection.connect("selection-changed", self._selection_changed)
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup_row)
            factory.connect("bind", self._bind_row)
            factory.connect("unbind", self._unbind_row)
            self.list = Gtk.ListView(model=self.selection, factory=factory)
            self.list.add_css_class("canvas-layer-list")
            self.list.set_single_click_activate(False)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._list_key)
            self.list.add_controller(keys)
            empty_click = Gtk.GestureClick(button=3)
            empty_click.connect("pressed", self._list_context)
            self.list.add_controller(empty_click)
            self.tree_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
            self.tree_scroll.add_css_class("canvas-layers-scroll")
            self.tree_scroll.set_child(self.list)
            self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.results.add_css_class("canvas-search-results")
            self.results.connect("row-activated", lambda _list, row: self.on_select([row.object_id]))
            self.results_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
            self.results_scroll.set_child(self.results)
            self.results_scroll.set_visible(False)
            layers.append(self.tree_scroll)
            layers.append(self.results_scroll)

            pages_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            pages_head.add_css_class("canvas-group-head")
            pages_label = Gtk.Label(label="Pages", xalign=0, hexpand=True)
            pages_label.add_css_class("canvas-group-label")
            pages_head.append(pages_label)
            layers.append(pages_head)
            self.pages = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
            self.pages.add_css_class("canvas-page-list")
            layers.append(self.pages)
            self.stack.add_named(layers, "layers")

            # TODO(kit-request canvas-06-creative-assets): compose the v70
            # swatches, text styles, components and photo choices here.
            # Assets tab: the document's color and text styles.
            self.assets = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.assets.add_css_class("canvas-asset-list")
            assets_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
            assets_scroll.set_child(self.assets)
            self.stack.add_named(assets_scroll, "assets")

        # ── data ────────────────────────────────────────────────────────

        def sync(self, document: Document, pages: Sequence[str] = ("Page 1",), current_page: int = 0) -> None:
            self.document = document
            self._syncing = True
            try:
                self.model.sync(document)
                self._sync_selection(document.selected_ids, scroll=False)
            finally:
                self._syncing = False
            self._sync_pages(pages, current_page)
            if self.stack.get_visible_child_name() == "assets":
                self._sync_assets()
            if self.search.get_visible() and self.search.get_text():
                self._search_changed()

        def select(self, object_ids: Sequence[str]) -> None:
            """Canvas selection changed: select rows, reveal and scroll to the
            first, without taking focus."""
            if self.document is None:
                return
            self._syncing = True
            try:
                self._sync_selection(object_ids, scroll=True)
            finally:
                self._syncing = False

        def _sync_selection(self, object_ids: Sequence[str], *, scroll: bool) -> None:
            document = self.document
            wanted = list(dict.fromkeys(object_ids))
            # The scope is set before rows are revealed, so rows bound while
            # revealing are painted once, with it.
            roots, members = focus_scope(document, wanted)
            repaint: list[dict] = []
            if roots != self._scope_roots or members != self._scope_members:
                self._scope_roots, self._scope_members = roots, members
                repaint = list(self.row_widgets.values())
            for object_id in wanted:
                self.model.reveal(document, object_id)
            for widgets in repaint:
                self._paint_row(widgets)
            selected = Gtk.Bitset.new_empty()
            first = None
            for object_id in wanted:
                position = self.model.position_of(object_id)
                if position is not None:
                    selected.add(position)
                    if first is None:
                        first = position
            current = self.selection.get_selection()
            if not current.equals(selected):
                mask = Gtk.Bitset.new_range(0, self.model.tree.get_n_items())
                self.selection.set_selection(selected, mask)
            if scroll and first is not None:
                self.list.scroll_to(first, Gtk.ListScrollFlags.NONE, None)

        def _selection_changed(self, *_args) -> None:
            if self._syncing:
                return
            bitset = self.selection.get_selection()
            ids = []
            for index in range(bitset.get_size()):
                row = self.model.tree.get_row(bitset.get_nth(index))
                if row is not None:
                    ids.append(row.get_item().object_id)
            self.on_select(ids)

        def _sync_pages(self, pages: Sequence[str], current: int) -> None:
            existing = []
            child = self.pages.get_first_child()
            while child is not None:
                existing.append(child)
                child = child.get_next_sibling()
            if [getattr(row, "page_name", None) for row in existing] != list(pages):
                for row in existing:
                    self.pages.remove(row)
                for name in pages:
                    row = Gtk.ListBoxRow()
                    row.page_name = name
                    label = Gtk.Label(label=name, xalign=0)
                    label.set_ellipsize(Pango.EllipsizeMode.END)
                    row.set_child(label)
                    row.add_css_class("canvas-page-row")
                    self.pages.append(row)
            row = self.pages.get_row_at_index(current)
            if row is not None:
                self.pages.select_row(row)

        def _sync_assets(self) -> None:
            while (child := self.assets.get_first_child()) is not None:
                self.assets.remove(child)
            document = self.document
            if document is None:
                return
            colors = [style for style in document.styles if style.kind == "color"]
            texts = [style for style in document.styles if style.kind == "text"]
            if not colors and not texts:
                note = Gtk.Label(label="Color and text styles you create appear here.", xalign=0, wrap=True)
                note.add_css_class("canvas-panel-note")
                self.assets.append(note)
                return
            for title, styles in (("Color styles", colors), ("Text styles", texts)):
                if not styles:
                    continue
                head = Gtk.Label(label=title, xalign=0)
                head.add_css_class("canvas-group-label")
                self.assets.append(head)
                head.get_parent().set_activatable(False)
                for style in styles:
                    line = Gtk.Button()
                    line.add_css_class("canvas-asset-row")
                    contents = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    line.set_child(contents)
                    if style.kind == "color":
                        contents.append(_swatch(str(style.value.get("value", "#000000")), 14))
                        line.connect("clicked", lambda _button, key=style.id: self.on_asset_color(key))
                    else:
                        glyph = Gtk.Image(icon_name="luma-canvas-text-symbolic", pixel_size=14)
                        contents.append(glyph)
                        line.connect("clicked", lambda _button, key=style.id: self.on_asset_text(key))
                    name = Gtk.Label(label=style.name, xalign=0, hexpand=True)
                    name.set_ellipsize(Pango.EllipsizeMode.END)
                    contents.append(name)
                    detail = (str(style.value.get("value", "")).upper() if style.kind == "color"
                              else f"{style.value.get('size', 13):g}")
                    meta = Gtk.Label(label=detail)
                    meta.add_css_class("canvas-row-meta")
                    contents.append(meta)
                    self.assets.append(line)

        def _tab_changed(self, value: str) -> None:
            self.stack.set_visible_child_name(value)
            if value == "assets":
                self._sync_assets()

        # ── search ──────────────────────────────────────────────────────

        def show_search(self) -> None:
            self.tab_buttons["layers"].set_active(True)
            self.search.set_visible(True)
            self.search.grab_focus()

        def hide_search(self) -> None:
            self.search.set_text("")
            self.search.set_visible(False)
            self.results_scroll.set_visible(False)
            self.tree_scroll.set_visible(True)

        def _search_changed(self) -> None:
            query = self.search.get_text().strip().casefold()
            self.results_scroll.set_visible(bool(query))
            self.tree_scroll.set_visible(not query)
            while (child := self.results.get_first_child()) is not None:
                self.results.remove(child)
            if not query or self.document is None:
                return
            matches = [item for item in reversed(self.document.objects)
                       if query in item.name.casefold() and item.kind not in HIDDEN_KINDS]
            if not matches:
                empty = Gtk.Label(label=f"No layers named “{self.search.get_text().strip()}”", xalign=0, wrap=True)
                empty.add_css_class("canvas-panel-note")
                self.results.append(empty)
                return
            for item in matches[:500]:
                row = Gtk.ListBoxRow()
                row.object_id = item.id
                line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                line.add_css_class("canvas-layer-row")
                line.append(Gtk.Image(icon_name=KIND_ICONS.get(item.kind, "luma-canvas-shape-symbolic"), pixel_size=14))
                label = Gtk.Label(label=item.name, xalign=0, hexpand=True)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                line.append(label)
                row.set_child(line)
                self.results.append(row)

        # ── rows ────────────────────────────────────────────────────────

        def _setup_row(self, _factory, list_item) -> None:
            expander = Gtk.TreeExpander()
            expander.set_indent_for_icon(True)
            expander.set_indent_for_depth(True)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.add_css_class("canvas-layer-row")
            glyph = Gtk.Image(pixel_size=14)
            glyph.add_css_class("canvas-layer-glyph")
            row.append(glyph)
            name_stack = Gtk.Stack(hexpand=True, hhomogeneous=False, vhomogeneous=False)
            label = Gtk.Label(xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            name_stack.add_named(label, "label")
            row.append(name_stack)
            lock = self._toggle_button("lock")
            eye = self._toggle_button("eye")
            row.append(lock)
            row.append(eye)
            expander.set_child(row)
            list_item.set_child(expander)
            widgets = {"expander": expander, "row": row, "glyph": glyph, "stack": name_stack,
                       "label": label, "lock": lock, "eye": eye, "handlers": []}
            list_item.canvas_widgets = widgets

            double = Gtk.GestureClick(button=1)
            double.connect("pressed", lambda gesture, count, _x, _y:
                           self.begin_rename(widgets["object_id"]) if count == 2 and widgets.get("object_id") else None)
            label.add_controller(double)
            context = Gtk.GestureClick(button=3)
            context.connect("pressed", lambda _g, _n, x, y: self._row_context(widgets, x, y))
            row.add_controller(context)
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda *_: self._row_hover(widgets, True))
            hover.connect("leave", lambda *_: self._row_hover(widgets, False))
            row.add_controller(hover)

            source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            source.connect("prepare", lambda _source, _x, _y: self._drag_prepare(widgets))
            row.add_controller(source)
            target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
            target.connect("motion", lambda _t, _x, y: self._drop_motion(widgets, y))
            target.connect("leave", lambda *_: self._drop_clear(widgets))
            target.connect("drop", lambda _t, value, _x, y: self._drop(widgets, value, y))
            row.add_controller(target)

        def _row_hover(self, widgets: dict, inside: bool) -> None:
            widgets["hover"] = inside
            self.on_hover(widgets.get("object_id") if inside else None)
            self._paint_toggles(widgets)

        def _paint_toggles(self, widgets: dict) -> None:
            """Eye and lock take space only on hover or when not default, so
            names are not cut short for controls nobody is using."""
            item = widgets.get("item")
            hovering = widgets.get("hover", False)
            widgets["eye"].set_visible(hovering or (item is not None and not item.props.shown)
                                       or self._sweep is not None)
            widgets["lock"].set_visible(hovering or (item is not None and item.props.locked)
                                        or self._sweep is not None)

        def _toggle_button(self, which: str) -> Gtk.Button:
            button = Gtk.Button()
            button.set_child(Gtk.Image(pixel_size=14))
            button.add_css_class("canvas-row-toggle")
            button.add_css_class(which)
            button.set_valign(Gtk.Align.CENTER)
            button.set_focus_on_click(False)
            button.which = which
            drag = Gtk.GestureDrag()
            drag.connect("drag-begin", lambda gesture, _x, _y: self._sweep_begin(gesture, button))
            drag.connect("drag-update", lambda _g, _dx, dy: self._sweep_update(button, dy))
            drag.connect("drag-end", lambda *_: self._sweep_end())
            button.add_controller(drag)
            # Keyboard activation still toggles the one row.
            button.connect("clicked", lambda _b: self._toggle_one(button))
            return button

        def _bind_row(self, _factory, list_item) -> None:
            widgets = list_item.canvas_widgets
            tree_row = list_item.get_item()
            item: LayerItem = tree_row.get_item()
            widgets["expander"].set_list_row(tree_row)
            widgets["object_id"] = item.object_id
            widgets["item"] = item
            widgets["lock"].object_id = item.object_id
            widgets["eye"].object_id = item.object_id
            self.rows_by_id[item.object_id] = widgets["row"]
            self.row_widgets[item.object_id] = widgets
            widgets["handlers"].append(item.connect("notify", lambda *_: self._paint_row(widgets)))
            self._paint_row(widgets)

        def _unbind_row(self, _factory, list_item) -> None:
            widgets = list_item.canvas_widgets
            item = widgets.get("item")
            for handler in widgets["handlers"]:
                item.disconnect(handler)
            widgets["handlers"].clear()
            if item is not None and self.rows_by_id.get(item.object_id) is widgets["row"]:
                del self.rows_by_id[item.object_id]
                self.row_widgets.pop(item.object_id, None)
            widgets["item"] = None
            widgets["object_id"] = None

        def _paint_row(self, widgets: dict) -> None:
            item = widgets["item"]
            if item is None:
                return
            # Plain Python mirrors of the item's properties; a row whose state
            # did not change makes no toolkit calls (selection repaints rows).
            name, kind, shown, locked, masking, masked = item.values
            layer = self.document.object(item.object_id) if self.document is not None else None
            icon = "luma-canvas-mask-symbolic" if masking else layer_icon(layer, kind)
            top_frame = layer is not None and layer.parent_id is None and layer.kind in {"frame", "page", "section"}
            scoped = bool(self._scope_members)
            classes = (("hidden-layer", not shown), ("locked-layer", locked),
                       ("top-level-frame", top_frame), ("mask-layer", bool(masking)),
                       ("masked-layer", bool(masked)),
                       ("scope-root", item.object_id in self._scope_roots),
                       ("in-scope", scoped and item.object_id in self._scope_members),
                       ("out-of-scope", scoped and item.object_id not in self._scope_members))
            state = (item.object_id, name, icon, bool(masking), shown, locked, classes)
            if widgets.get("painted") != state:
                widgets["painted"] = state
                widgets["label"].set_label(name)
                row = widgets["row"]
                widgets["glyph"].set_from_icon_name(icon)
                widgets["glyph"].set_tooltip_text("Mask" if masking else None)
                for css, on in classes:
                    (row.add_css_class if on else row.remove_css_class)(css)
                eye, lock = widgets["eye"], widgets["lock"]
                eye.get_child().set_from_icon_name("luma-canvas-eye-off-symbolic" if not shown
                                                   else "luma-canvas-eye-symbolic")
                lock.get_child().set_from_icon_name("luma-canvas-lock-symbolic" if locked
                                                    else "luma-canvas-unlock-symbolic")
                label = "Show layer" if not shown else "Hide layer"
                eye.set_tooltip_text(label)
                eye.update_property([Gtk.AccessibleProperty.LABEL], [label])
                label = "Unlock layer" if locked else "Lock layer"
                lock.set_tooltip_text(label)
                lock.update_property([Gtk.AccessibleProperty.LABEL], [label])
                (eye.add_css_class if not shown else eye.remove_css_class)("active")
                (lock.add_css_class if locked else lock.remove_css_class)("active")
            self._paint_toggles(widgets)

        # ── rename ──────────────────────────────────────────────────────

        def begin_rename(self, object_id: str | None = None) -> bool:
            if object_id is None:
                ids = self.selected_ids()
                if not ids:
                    return False
                object_id = ids[0]
            if self.document is None:
                return False
            position = self.model.reveal(self.document, object_id)
            if position is not None:
                self.list.scroll_to(position, Gtk.ListScrollFlags.NONE, None)
            row = self.rows_by_id.get(object_id)
            if row is None:
                GLib.idle_add(lambda: (self.begin_rename(object_id), False)[1])
                return True
            stack = row.get_first_child().get_next_sibling()
            label = stack.get_child_by_name("label")
            previous = stack.get_child_by_name("rename")
            if previous is not None:
                stack.remove(previous)

            def finish(text: str | None) -> bool:
                if text is not None and text.strip() and text.strip() != label.get_label():
                    if not self.on_rename(object_id, text.strip()):
                        return False
                stack.set_visible_child_name("label")
                GLib.idle_add(lambda: (stack.get_child_by_name("rename") and stack.remove(stack.get_child_by_name("rename")), False)[1])
                return True

            field = InlineRenameField(label.get_label(), commit=lambda text: finish(text), cancel=lambda: finish(None))
            field.add_css_class("canvas-layer-rename")
            keys = Gtk.EventControllerKey()
            keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            keys.connect("key-pressed", lambda _c, keyval, _code, _state: self._rename_tab(keyval, field, object_id))
            field.add_controller(keys)
            stack.add_named(field, "rename")
            stack.set_visible_child_name("rename")
            field.begin()
            return True

        def _rename_tab(self, keyval: int, field, object_id: str) -> bool:
            if keyval not in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
                return False
            field.finish()
            document = self.document
            item = document.object(object_id) if document else None
            if item is None:
                return True
            siblings = list(reversed([other for other in document.objects if other.parent_id == item.parent_id
                                      and other.kind not in HIDDEN_KINDS]))
            index = siblings.index(item) + (1 if keyval == Gdk.KEY_Tab else -1)
            if 0 <= index < len(siblings):
                GLib.idle_add(lambda: (self.begin_rename(siblings[index].id), False)[1])
            return True

        # ── keyboard, context ───────────────────────────────────────────

        def selected_ids(self) -> list[str]:
            bitset = self.selection.get_selection()
            return [self.model.tree.get_row(bitset.get_nth(index)).get_item().object_id
                    for index in range(bitset.get_size())]

        def _list_key(self, _controller, keyval: int, _code: int, state) -> bool:
            focus_row = None
            ids = self.selected_ids()
            if ids:
                position = self.model.position_of(ids[0])
                focus_row = self.model.tree.get_row(position) if position is not None else None
            if keyval == Gdk.KEY_F2:
                return self.begin_rename()
            if focus_row is None:
                return False
            if keyval == Gdk.KEY_Right and focus_row.is_expandable():
                focus_row.set_expanded(True)
                return True
            if keyval == Gdk.KEY_Left:
                if focus_row.get_expanded():
                    focus_row.set_expanded(False)
                    return True
                parent = focus_row.get_parent()
                if parent is not None:
                    self.on_select([parent.get_item().object_id])
                    return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                if state & Gdk.ModifierType.SHIFT_MASK:
                    parent = focus_row.get_parent()
                    if parent is not None:
                        self.on_select([parent.get_item().object_id])
                    return True
                children = focus_row.get_children()
                if children is not None and children.get_n_items():
                    focus_row.set_expanded(True)
                    self.on_select([children.get_item(i).object_id for i in range(children.get_n_items())])
                return True
            return False

        def _row_context(self, widgets: dict, x: float, y: float) -> None:
            object_id = widgets.get("object_id")
            if object_id and object_id not in self.selected_ids():
                self.on_select([object_id])
            self.on_context(widgets["row"], x, y, object_id)

        def _list_context(self, _gesture, _count, x: float, y: float) -> None:
            picked = self.list.pick(x, y, Gtk.PickFlags.DEFAULT)
            while picked is not None and "canvas-layer-row" not in picked.get_css_classes():
                picked = picked.get_parent() if picked is not self.list else None
            if picked is None:
                self.on_context(self.list, x, y, None)

        # ── eye and lock, one row or a sweep across several ─────────────

        def _toggle_one(self, button) -> None:
            if self._sweep is not None:
                return
            item = self.model.items.get(getattr(button, "object_id", ""))
            if item is None:
                return
            if button.which == "eye":
                self.on_toggle("visible", [item.object_id], not item.props.shown)
            else:
                self.on_toggle("locked", [item.object_id], not item.props.locked)

        def _sweep_begin(self, gesture, button) -> None:
            item = self.model.items.get(getattr(button, "object_id", ""))
            if item is None:
                return
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            key = "visible" if button.which == "eye" else "locked"
            # Every row the sweep crosses takes the first row's new state.
            value = (not item.props.shown) if key == "visible" else (not item.props.locked)
            self._sweep = {"which": button.which, "key": key, "value": value,
                           "ids": [item.object_id], "buttons": [button]}
            button.add_css_class("sweeping")

        def _sweep_update(self, button, dy: float) -> None:
            if self._sweep is None:
                return
            ok, point = button.compute_point(self.list, Graphene_point(button.get_width() / 2, button.get_height() / 2 + dy))
            if not ok:
                return
            picked = self.list.pick(point.x, point.y, Gtk.PickFlags.DEFAULT)
            while picked is not None and not hasattr(picked, "which"):
                picked = picked.get_parent()
                if picked is self.list:
                    picked = None
            if picked is not None and picked.which == self._sweep["which"]:
                object_id = getattr(picked, "object_id", None)
                if object_id and object_id not in self._sweep["ids"]:
                    self._sweep["ids"].append(object_id)
                    self._sweep["buttons"].append(picked)
                    picked.add_css_class("sweeping")

        def _sweep_end(self) -> None:
            sweep, self._sweep = self._sweep, None
            if sweep is None:
                return
            for button in sweep["buttons"]:
                button.remove_css_class("sweeping")
            self.on_toggle(sweep["key"], sweep["ids"], sweep["value"])

        # ── drag and drop reorder ───────────────────────────────────────

        def _drag_prepare(self, widgets: dict):
            object_id = widgets.get("object_id")
            if not object_id:
                return None
            selected = self.selected_ids()
            moving = selected if object_id in selected else [object_id]
            return Gdk.ContentProvider.new_for_value("\n".join(moving))

        def _zone(self, widgets: dict, y: float) -> str:
            height = max(1, widgets["row"].get_height())
            item = widgets.get("item")
            container = item is not None and item.props.kind in CONTAINER_KINDS
            if container and height * 0.25 <= y <= height * 0.75:
                return "into"
            return "above" if y < height / 2 else "below"

        def _drop_motion(self, widgets: dict, y: float):
            row = widgets["row"]
            zone = self._zone(widgets, y)
            for name in ("drop-above", "drop-below", "drop-into"):
                (row.add_css_class if name == f"drop-{zone}" else row.remove_css_class)(name)
            if zone == "into" and not self._expand_timer:
                def expand() -> bool:
                    self._expand_timer = 0
                    object_id = widgets.get("object_id")
                    position = self.model.position_of(object_id) if object_id else None
                    if position is not None:
                        self.model.tree.get_row(position).set_expanded(True)
                    return False
                self._expand_timer = GLib.timeout_add(600, expand)
            elif zone != "into" and self._expand_timer:
                GLib.source_remove(self._expand_timer)
                self._expand_timer = 0
            return Gdk.DragAction.MOVE

        def _drop_clear(self, widgets: dict) -> None:
            for name in ("drop-above", "drop-below", "drop-into"):
                widgets["row"].remove_css_class(name)
            if self._expand_timer:
                GLib.source_remove(self._expand_timer)
                self._expand_timer = 0

        def _drop(self, widgets: dict, value, y: float) -> bool:
            zone = self._zone(widgets, y)
            self._drop_clear(widgets)
            target = widgets.get("object_id")
            if not target or not isinstance(value, str):
                return False
            moving = [object_id for object_id in value.split("\n") if object_id]
            self.on_reorder(moving, target, zone)
            return True

    def Graphene_point(x: float, y: float):
        from gi.repository import Graphene
        point = Graphene.Point()
        point.x, point.y = x, y
        return point

    def _swatch(color: str, size: int) -> Gtk.Widget:
        """A document color, drawn in code: document data, not UI palette."""
        area = Gtk.DrawingArea(content_width=size, content_height=size, valign=Gtk.Align.CENTER)
        area.add_css_class("canvas-swatch")
        rgba = Gdk.RGBA()
        if not rgba.parse(color):
            rgba.parse("#000000")

        def draw(_area, cr, width, height) -> None:
            radius = 4
            import math
            cr.new_sub_path()
            cr.arc(width - radius, radius, radius, -math.pi / 2, 0)
            cr.arc(width - radius, height - radius, radius, 0, math.pi / 2)
            cr.arc(radius, height - radius, radius, math.pi / 2, math.pi)
            cr.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
            cr.close_path()
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)
            cr.fill()

        area.set_draw_func(draw)
        return area
