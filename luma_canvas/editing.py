# SPDX-License-Identifier: Apache-2.0
"""Transactional editing and named undo history for Canvas."""

from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass, field
import os
import pickle
import time
from typing import Any, Iterator

from .components import OVERRIDABLE, sync_component
from .layout import AutoLayout, apply_auto_layout, reflow
from .model import (_OBJECT_FIELDS, CONTAINER_KINDS, GROUP_PAINT_KEYS, ChangeTracker, Document, InvalidDocument,
                    LayoutObject, migrate_group_paint, new_id, note_changed, paint_targets)
from .paint import assign_paint, normalized_style, remember_edit, style_for_new


class EditError(InvalidDocument):
    pass


# Document fields a history step restores besides its layers. The embedded
# asset bytes are never part of history.
_UNTRACKED_FIELDS = {"objects", "embedded_assets"}
_VOLATILE_FIELDS = {"revision", "modified_at"}
_MISSING = object()


def _document_fields(document: Document) -> bytes:
    values = {name: getattr(document, name) for name in Document.__dataclass_fields__
              if name not in _UNTRACKED_FIELDS}
    return pickle.dumps(values, pickle.HIGHEST_PROTOCOL)


@dataclass(slots=True)
class Snapshot:
    """One side of a history step: the document's fields, its layer order,
    and the layers the step changed (pickled, or None where absent)."""
    fields: bytes
    order: tuple[str, ...]
    objects: dict[str, bytes | None] = field(default_factory=dict)


@dataclass(slots=True)
class HistoryEntry:
    label: str
    before: Snapshot
    after: Snapshot
    # Entries with the same key made within the coalescing window merge into
    # one, so a burst of arrow nudges is one undo step.
    key: str | None = None
    stamp: float = 0.0

    def absorb(self, newer: "HistoryEntry") -> None:
        """Make this entry also cover `newer`, which directly followed it."""
        objects = dict(self.before.objects)
        for object_id, data in newer.before.objects.items():
            objects.setdefault(object_id, data)
        after = dict(self.after.objects)
        after.update(newer.after.objects)
        self.before = Snapshot(self.before.fields, self.before.order, objects)
        self.after = Snapshot(newer.after.fields, newer.after.order, after)
        self.stamp = max(self.stamp, newer.stamp)


# Names of new objects, without numbering (Figma).
KIND_NAMES = {"rectangle": "Rectangle", "ellipse": "Ellipse", "line": "Line",
              "polygon": "Polygon", "star": "Star", "frame": "Frame", "text": "Text",
              "path": "Vector", "group": "Group", "image": "Image"}
# Style keys that teach the current style when edited on a selection.
PAINT_KEYS = {"fill", "stroke", "stroke_width", "stroke_align", "stroke_cap",
              "effects", "fill_gradient", "stroke_gradient"}


class Editor:
    def __init__(self, document: Document, limit: int = 150) -> None:
        self.document = document
        self.limit = limit
        self.undo_stack: list[HistoryEntry] = []
        self.redo_stack: list[HistoryEntry] = []
        self._depth = 0
        self._tracker = ChangeTracker()
        self._before_fields: bytes | None = None
        self._before_order: tuple[str, ...] = ()
        self._verify: dict[str, Any] | None = None
        self._label = ""
        self._key: str | None = None
        self._gesture_open = False
        # "Last used style" (default) or a fixed default style (spec 6.2).
        self.style_follows_edits = True
        self.clock = time.monotonic

    # ── history ───────────────────────────────────────────────────────────
    #
    # A step keeps only the layers it changed (ChangeTracker), so an edit on a
    # document with tens of thousands of layers costs what the edit touches.

    def _begin(self) -> None:
        document = self.document
        self._tracker.watch(document.objects)
        self._tracker.open()
        self._components_sync_finished=False
        self._before_fields = _document_fields(document)
        self._before_order = tuple(item.id for item in document.objects)
        self._verify = document.to_dict() if os.environ.get("CANVAS_VERIFY_CHANGES") else None

    def _finish(self) -> tuple[Snapshot, Snapshot]:
        """Close tracking and describe what changed since `_begin`."""
        self._sync_changed_components()
        tracker = self._tracker
        tracker.close()
        document = self.document
        before_map = tracker.before
        after_map = {item.id: item for item in document.objects}
        # From the list itself: a duplicated id must show as a changed order,
        # so the step's check sees it.
        after_order = tuple(item.id for item in document.objects)
        changed = set(tracker.ids)
        if after_order != self._before_order:
            changed |= before_map.keys() ^ after_map.keys()
        for object_id, item in after_map.items():
            if before_map.get(object_id) is not item:
                changed.add(object_id)
        dumps, protocol = pickle.dumps, pickle.HIGHEST_PROTOCOL
        before_objects: dict[str, bytes | None] = {}
        after_objects: dict[str, bytes | None] = {}
        for object_id in changed:
            old = tracker.captured.get(object_id)
            if old is None and object_id in before_map:
                old = dumps(before_map[object_id], protocol)
            item = after_map.get(object_id)
            new = dumps(item, protocol) if item is not None else None
            if old != new:
                before_objects[object_id] = old
                after_objects[object_id] = new
        before = Snapshot(self._before_fields or b"", self._before_order, before_objects)
        after = Snapshot(_document_fields(document), after_order, after_objects)
        if self._verify is not None:
            self._check_tracking(before, after)
        tracker.watch(None)
        self._before_fields, self._before_order, self._verify = None, (), None
        return before, after

    def _sync_changed_components(self):
        if getattr(self,'_components_sync_finished',False):return
        self._components_sync_finished=True
        before=self._tracker.before;current={o.id:o for o in self.document.objects}
        changed=set(self._tracker.ids)|(before.keys()^current.keys())
        owners=set()
        for identity in changed:
            for graph in (current,before):
                item=graph.get(identity);seen=set()
                while item is not None and item.id not in seen:
                    seen.add(item.id)
                    if item.kind=='component':
                        if item.id in current:owners.add(item.id)
                        break
                    item=graph.get(item.parent_id)
        for owner in owners:sync_component(self.document,owner)

    def _check_tracking(self, before: Snapshot, after: Snapshot) -> None:
        """CANVAS_VERIFY_CHANGES: every layer the tracker did not report must
        be exactly as it was, and every kept "before" must be the layer as the
        step found it (nothing was changed in place)."""
        assert self._verify is not None
        original = {item["id"]: item for item in self._verify["objects"]}
        for object_id, data in before.objects.items():
            if data is None:
                continue
            layer = pickle.loads(data)
            kept = pickle.loads(pickle.dumps({key: getattr(layer, key) for key in _OBJECT_FIELDS},
                                             pickle.HIGHEST_PROTOCOL))
            if original.get(object_id) != kept:
                keys = sorted(key for key in kept if (original.get(object_id) or {}).get(key) != kept[key])
                raise AssertionError(f"layer {object_id} changed in place before its first assignment: {keys}")
        expected = dict(original)
        for object_id, data in after.objects.items():
            if data is None:
                expected.pop(object_id, None)
            else:
                layer = pickle.loads(data)
                expected[object_id] = {key: getattr(layer, key) for key in _OBJECT_FIELDS}
        expected = pickle.loads(pickle.dumps(expected, pickle.HIGHEST_PROTOCOL))
        actual = {item["id"]: item for item in self.document.to_dict()["objects"]}
        for object_id, value in actual.items():
            if expected.get(object_id) != value:
                keys = sorted(key for key in value if (expected.get(object_id) or {}).get(key) != value[key])
                raise AssertionError(f"layer {object_id} changed without an assignment (in place?): {keys}")

    @staticmethod
    def _empty(before: Snapshot, after: Snapshot, *, ignore_volatile: bool) -> bool:
        if before.objects or after.objects or before.order != after.order:
            return False
        if before.fields == after.fields:
            return True
        if not ignore_volatile:
            return False
        old, new = pickle.loads(before.fields), pickle.loads(after.fields)
        return {k: v for k, v in old.items() if k not in _VOLATILE_FIELDS} == \
            {k: v for k, v in new.items() if k not in _VOLATILE_FIELDS}

    def _restore(self, snapshot: Snapshot) -> None:
        """Put the document's layers and fields as `snapshot` has them."""
        document = self.document
        current = {item.id: item for item in document.objects}
        loads = pickle.loads
        objects: list[LayoutObject] = []
        for object_id in snapshot.order:
            data = snapshot.objects.get(object_id, _MISSING)
            if data is _MISSING:
                item = current.get(object_id)
            else:
                item = loads(data) if data is not None else None
            if item is not None:
                objects.append(item)
        for name, value in loads(snapshot.fields).items():
            setattr(document, name, value)
        document.objects = objects
        # Layers loaded from bytes were never written: tell autosave and views.
        note_changed(snapshot.objects)

    @contextlib.contextmanager
    def transaction(self, label: str, *, key: str | None = None,
                    window: float = 0.8) -> Iterator[None]:
        outer = self._depth == 0
        if outer:
            self._begin()
            self._label = label
        self._depth += 1
        try:
            yield
            if outer:
                self.normalize_groups()
                before, after = self._finish()
                try:
                    self._validate_step(before, after)
                except BaseException:
                    self._restore(before)
                    raise
                if not self._empty(before, after, ignore_volatile=False):
                    self._record(label, before, after, key=key, window=window)
        except BaseException:
            if outer and self._before_fields is not None:
                before, _after = self._finish_quietly()
                self._restore(before)
            raise
        finally:
            self._depth -= 1
            if outer:
                self._label = ""

    # Document fields whose change can break layers that did not change.
    _REFERENCED_FIELDS = ("styles", "assets", "pages", "connections", "current_page_id")

    def _validate_step(self, before: Snapshot, after: Snapshot) -> None:
        """Check what a step could have broken: the layers it changed, layers
        left inside removed ones, and document-wide rules when layers were
        added, removed or reordered (the whole document when styles, assets,
        pages or links changed)."""
        document = self.document
        if before.fields != after.fields:
            old, new = pickle.loads(before.fields), pickle.loads(after.fields)
            if any(old.get(name) != new.get(name) for name in self._REFERENCED_FIELDS):
                document.validate()
                return
        reordered = before.order != after.order
        removed = set(before.order) - set(after.order) if reordered else set()
        changed = [object_id for object_id, data in after.objects.items() if data is not None]
        document.validate(changed, removed=removed, reordered=reordered)

    def _finish_quietly(self) -> tuple[Snapshot, Snapshot]:
        """`_finish` without the in-place check (the change is being undone)."""
        self._verify = None
        return self._finish()

    def _record(self, label: str, before: Snapshot, after: Snapshot, *,
                key: str | None = None, window: float = 0.8) -> bool:
        now = self.clock()
        top = self.undo_stack[-1] if self.undo_stack else None
        entry = HistoryEntry(label, before, after, key, now)
        if (key is not None and top is not None and top.key == key
                and now - top.stamp <= window and not self.redo_stack):
            top.absorb(entry)
            top.stamp = now
            return True
        self.undo_stack.append(entry)
        del self.undo_stack[:-self.limit]
        self.redo_stack.clear()
        return True

    # ── gestures ─────────────────────────────────────────────────────────
    #
    # A drag, a scrub or a picker drag changes the document many times a
    # second. Those changes are applied straight to the model while the
    # gesture is open and become one undo entry when it ends.

    @property
    def gesture_open(self) -> bool:
        return self._gesture_open

    def begin_gesture(self, label: str, *, key: str | None = None) -> None:
        if self._gesture_open:
            self._label = label
            return
        if self._depth:
            raise EditError("a gesture cannot start inside a transaction")
        self._begin()
        self._label = label
        self._key = key
        self._gesture_open = True
        self._depth = 1

    def set_gesture_label(self, label: str) -> None:
        if self._gesture_open:
            self._label = label

    def end_gesture(self) -> bool:
        """Close the open gesture; returns whether it changed the document."""
        if not self._gesture_open:
            return False
        label, key = self._label, self._key
        revision, modified_at = self.document.revision, self.document.modified_at
        self._gesture_open = False
        self._depth = 0
        self._label = ""
        self._key = None
        self.normalize_groups()
        self.document.touch()
        before, after = self._finish()
        # touch() changes the revision and time even when nothing else did.
        if self._empty(before, after, ignore_volatile=True):
            self.document.revision = revision
            self.document.modified_at = modified_at
            return False
        try:
            self._validate_step(before, after)
        except BaseException:
            self._restore(before)
            raise
        return self._record(label, before, after, key=key)

    def cancel_gesture(self) -> None:
        """Put the document back the way the gesture found it."""
        if not self._gesture_open:
            return
        self._gesture_open = False
        self._depth = 0
        self._label = ""
        self._key = None
        before, _after = self._finish_quietly()
        self._restore(before)

    @property
    def undo_label(self) -> str | None:
        return self.undo_stack[-1].label if self.undo_stack else None

    @property
    def redo_label(self) -> str | None:
        return self.redo_stack[-1].label if self.redo_stack else None

    def undo(self) -> Document:
        self.end_gesture()
        if self.undo_stack:
            entry = self.undo_stack.pop()
            self._restore(entry.before)
            self.redo_stack.append(entry)
        return self.document

    def redo(self) -> Document:
        self.end_gesture()
        if self.redo_stack:
            entry = self.redo_stack.pop()
            self._restore(entry.after)
            self.undo_stack.append(entry)
        return self.document

    def _editable(self, object_id: str) -> LayoutObject:
        item = self.document.object(object_id)
        if item is None:
            raise EditError(f"unknown object: {object_id}")
        if item.locked or any(parent.locked for parent in self.document.ancestors(item.id)):
            raise EditError(f"{item.name} is locked")
        return item

    def create(
        self, kind: str, x: float, y: float, width: float, height: float,
        *, parent_id: str | None = None, name: str | None = None,
        use_style: bool = True, **fields: Any,
    ) -> LayoutObject:
        """Draw a new object with the current style (spec 6.2).

        Shapes take the current shape style, lines and paths the line style,
        text the text style, frames the frame style (white, never the shape
        color). Extra model fields override the style.
        """
        if parent_id:
            parent = self.document.object(parent_id)
            if parent is None or parent.kind not in CONTAINER_KINDS:
                raise EditError("choose a valid container")
        values: dict[str, Any] = {"fill": "#ffffff"}
        if kind == "page":
            values = {"fill": "#fffdf8"}
        elif kind == "text":
            values = {"fill": "#000000", "text": "Type something"}
        if use_style:
            values.update(style_for_new(self.document.current_style, kind))
            if kind == "text":
                text_style = normalized_style(self.document.current_style)["text"]
                values.setdefault("metadata", {})
                values["metadata"] = {"font_family": text_style.get("font_family", "Figtree"),
                                      "font_size": text_style.get("font_size", 16),
                                      "font_weight": text_style.get("font_weight", 400)}
        elif kind not in {"frame", "page", "text"}:
            values["fill"] = "#d9d9d9"
        values.update(fields)
        minimum = 0.0 if kind == "line" else 1.0
        item = LayoutObject(
            new_id(kind), kind, name or KIND_NAMES.get(kind, kind.title()), parent_id,
            x, y, max(minimum, width), max(minimum, height), **values,
        )
        with self.transaction(f"Create {item.name}"):
            self.document.objects.append(item)
            self.document.select([item.id])
            self.document.touch()
        return item

    # ── current style ────────────────────────────────────────────────────

    def remember_style(self, items: list[LayoutObject], key: str, value: Any) -> None:
        """Teach the current style from an edit on a selection."""
        if not self.style_follows_edits or key not in PAINT_KEYS:
            return
        style = self.document.current_style
        for item in items:
            style = remember_edit(style, item.kind, key, value)
        self.document.current_style = style

    def set_current_style(self, style: dict[str, Any], label: str = "Change current style") -> None:
        with self.transaction(label):
            self.document.current_style = normalized_style(style)
            self.document.touch()

    def set_paint(self, object_ids: list[str], target: str, paint: dict[str, Any] | None, *,
                  label: str | None = None) -> None:
        """Set a fill or stroke paint (solid or gradient) on objects."""
        if target not in {"fill", "stroke"}:
            raise EditError("paint target must be fill or stroke")
        items = [self._editable(item) for item in object_ids]
        items = [self._editable(item.id) for item in paint_targets(self.document, items)]
        with self.transaction(label or f"Change {target}"):
            for item in items:
                self._detach_resource_links(item, {target})
                assign_paint(item, target, paint)
                self.remember_style([item], target, getattr(item, target))
                self.remember_style([item], f"{target}_gradient", getattr(item, f"{target}_gradient"))
                if target == "stroke":
                    self.remember_style([item], "stroke_width", item.stroke_width)
            self._propagate(items, target)
            self.document.touch()

    # ── paint stacks (several fills and strokes, bottom to top) ──────────

    def _paint_items(self, object_ids: list[str], target: str) -> list[LayoutObject]:
        if target not in {"fill", "stroke"}:
            raise EditError("paint target must be fill or stroke")
        items = [self._editable(item) for item in object_ids]
        return [self._editable(item.id) for item in paint_targets(self.document, items)]

    def set_stack_paint(self, object_ids: list[str], target: str, index: int, paint: dict[str, Any] | None, *,
                        label: str | None = None) -> None:
        """Replace (or with `None` remove) the paint at `index` of each layer's stack."""
        from .paint import replace_stack_paint
        items = self._paint_items(object_ids, target)
        with self.transaction(label or f"Change {target}"):
            for item in items:
                self._detach_resource_links(item, {target})
                replace_stack_paint(item, target, index, paint)
                if index == 0:
                    self.remember_style([item], target, getattr(item, target))
                    self.remember_style([item], f"{target}_gradient", getattr(item, f"{target}_gradient"))
            self._propagate(items, f"{target}s")
            self.document.touch()

    def add_stack_paint(self, object_ids: list[str], target: str, paint: dict[str, Any], *,
                        label: str | None = None) -> None:
        """Put a new paint on top of each layer's stack (Figma's + in Fill)."""
        from .paint import paint_stack, set_paint_stack
        items = self._paint_items(object_ids, target)
        with self.transaction(label or f"Add {target}"):
            for item in items:
                self._detach_resource_links(item, {target})
                set_paint_stack(item, target, [*paint_stack(item, target), copy.deepcopy(paint)])
            self._propagate(items, f"{target}s")
            self.document.touch()

    def update_stack_paint(self, object_ids: list[str], target: str, index: int, *, label: str | None = None,
                           **values: Any) -> None:
        """Change a paint's visible, opacity or blend without replacing it."""
        from .paint import BLEND_MODES, update_stack_paint
        if "blend" in values and values["blend"] not in BLEND_MODES:
            raise EditError("unknown blend mode")
        items = self._paint_items(object_ids, target)
        with self.transaction(label or f"Change {target}"):
            for item in items:
                self._detach_resource_links(item, {target})
                update_stack_paint(item, target, index, **values)
            self._propagate(items, f"{target}s")
            self.document.touch()

    def move_stack_paint(self, object_ids: list[str], target: str, source: int, destination: int, *,
                         label: str | None = None) -> None:
        from .paint import move_stack_paint
        items = self._paint_items(object_ids, target)
        with self.transaction(label or f"Reorder {target}s"):
            for item in items:
                move_stack_paint(item, target, source, destination)
            self._propagate(items, f"{target}s")
            self.document.touch()

    def move(self, object_ids: list[str], dx: float, dy: float, *,
             key: str | None = None) -> None:
        items = [self._editable(item) for item in object_ids]
        label = "Move objects" if len(items) > 1 else f"Move {items[0].name}"
        with self.transaction(label, key=key):
            for item in self.with_descendants(items):
                item.x += dx
                item.y += dy
            self.document.touch()

    def with_descendants(self, items: list[LayoutObject]) -> list[LayoutObject]:
        """Objects and everything inside them, once each.

        Coordinates are absolute in schema 1, so moving a frame has to move
        its children with it.
        """
        seen: set[str] = set()
        result: list[LayoutObject] = []
        for item in items:
            for entry in (item, *self.document.descendants(item.id)):
                if entry.id not in seen:
                    seen.add(entry.id)
                    result.append(entry)
        return result

    def _detach_resource_links(self, item, fields):
        bindings = item.metadata.get("variable_bindings", {})
        if any(key in bindings for key in fields):
            item.metadata = {**item.metadata, "variable_bindings":
                             {key:value for key,value in bindings.items() if key not in fields}}
        kinds = set()
        if set(fields) & {"fill", "fill_gradient", "fills"}: kinds.add("color")
        if "metadata" in fields: kinds.add("text")
        if kinds:
            removed = {style.id for style in self.document.styles if style.kind in kinds}
            item.style_ids = [key for key in item.style_ids if key not in removed]

    def set_geometry(self, object_id: str, **values: float) -> None:
        item = self._editable(object_id)
        allowed = {"x", "y", "width", "height", "rotation", "grow"}
        if not set(values).issubset(allowed):
            raise EditError("unsupported geometry property")
        with self.transaction(f"Resize {item.name}"):
            before_width, before_height = item.width, item.height
            self._detach_resource_links(item, values)
            for key, value in values.items():
                setattr(item, key, max(1.0, value) if key in {"width", "height"} else value)
            if item.kind == 'instance' or any(parent.kind == 'instance' for parent in self.document.ancestors(item.id)):
                for key in set(values) & {'width','height','rotation','grow'}:
                    item.overrides={**item.overrides,key:getattr(item,key)}
            # A container that changed size has to tell its children, or the
            # constraints and auto-layout the inspector offers mean nothing.
            self._reflow(item, before_width, before_height)
            self.document.touch()

    def set_property(self, object_ids: list[str], key: str, value: Any) -> None:
        allowed = {"name", "fill", "stroke", "stroke_width", "radius", "corner_radii", "fills", "strokes",
                   "fill_gradient", "stroke_gradient", "point_count", "inner_ratio", "clip_content",
                   "start_cap", "end_cap",
                   "opacity", "visible", "locked", "text", "layout", "constraints",
                   "effects", "metadata", "stroke_align", "stroke_cap", "stroke_join",
                   "stroke_dash", "flip_h", "flip_v", "path", "grow", "columns",
                   "column_gap", "baseline", "bleed", "margins", "text_flow_next_id",
                   "master_id", "component_variant"}
        if key not in allowed:
            raise EditError(f"unsupported property: {key}")
        if key == "locked" and value is False:
            items = [self.document.object(item) for item in object_ids]
            if any(item is None for item in items):
                raise EditError("unknown object")
        else:
            items = [self._editable(item) for item in object_ids]
        if key in GROUP_PAINT_KEYS:
            items = [self._editable(item.id) for item in paint_targets(self.document, items)]
        with self.transaction(f"Change {key}"):
            for item in items:
                resource_fields = {key}
                if key == "metadata" and not any(item.metadata.get(name) != value.get(name) for name in ("font_family", "font_size", "font_weight", "line_height", "letter_spacing")):
                    resource_fields = set()
                self._detach_resource_links(item, resource_fields)
                if key in {"fills", "strokes"}:
                    from .paint import set_paint_stack
                    set_paint_stack(item, key[:-1], copy.deepcopy(value))
                    continue
                setattr(item, key, copy.deepcopy(value))
                if key in {"fill", "stroke"} and value is None:
                    setattr(item, f"{key}_gradient", None)
                self.remember_style([item], key, value)
                if (item.kind == "instance" or any(parent.kind == "instance" for parent in self.document.ancestors(item.id))) and key in OVERRIDABLE:
                    # Changing an instance is an override, not a divergence:
                    # it is remembered so the component can change underneath.
                    item.overrides = {**item.overrides, key: value}
            self._propagate(items, key)
            self.document.touch()

    def _reflow(self, item: LayoutObject, before_width: float,
                before_height: float) -> None:
        """Lay out a container's children after it changed, and its parent's."""
        if item.kind in {"frame", "page", "component", "layout", "instance", "group"}:
            reflow(self.document, item, before_width, before_height)
        parent = self.document.object(item.parent_id) if item.parent_id else None
        if parent is not None and AutoLayout.read(parent):
            apply_auto_layout(self.document, parent)

    def _propagate(self, items: list[LayoutObject], key: str) -> None:
        """Push a component's change into its instances."""
        if key == "locked":
            return
        for item in items:
            if key in OVERRIDABLE and (item.kind == "instance" or any(parent.kind == "instance" for parent in self.document.ancestors(item.id))):
                item.overrides = {**item.overrides, key: copy.deepcopy(getattr(item, key))}
            if AutoLayout.read(item):
                apply_auto_layout(self.document, item)

    def duplicate(self, object_ids: list[str], *, offset: float = 16.0,
                  rename: bool = True) -> list[LayoutObject]:
        roots = [self._editable(item) for item in object_ids]
        clones: list[LayoutObject] = []
        cloned_roots: list[str] = []
        with self.transaction("Duplicate objects" if len(roots) > 1 else f"Duplicate {roots[0].name}"):
            for root in roots:
                family = [root, *self.document.descendants(root.id)]
                id_map = {item.id: new_id(item.kind) for item in family}
                cloned_roots.append(id_map[root.id])
                for source in family:
                    clone = source.clone()
                    clone.id = id_map[source.id]
                    clone.name = f"{source.name} copy" if source is root and rename else source.name
                    clone.parent_id = id_map.get(source.parent_id, source.parent_id)
                    clone.x += offset
                    clone.y += offset
                    if clone.component_source_id in id_map:
                        clone.component_source_id = id_map[clone.component_source_id]
                    if clone.text_flow_next_id in id_map:
                        clone.text_flow_next_id = id_map[clone.text_flow_next_id]
                    clones.append(clone)
                self.document.objects.extend(clones[-len(family):])
            self.document.select(cloned_roots)
            self.document.touch()
        return clones

    def delete(self, object_ids: list[str]) -> None:
        roots = [self._editable(item) for item in object_ids]
        doomed = {item.id for item in roots}
        for root in roots:
            doomed.update(child.id for child in self.document.descendants(root.id))
        referenced = [item.name for item in self.document.all_objects() if item.component_source_id in doomed and item.id not in doomed]
        if referenced:
            raise EditError(f"component is still used by: {', '.join(referenced)}")
        with self.transaction("Delete objects" if len(roots) > 1 else f"Delete {roots[0].name}"):
            self.document.objects = [item for item in self.document.objects if item.id not in doomed]
            self.document.connections = [
                item for item in self.document.connections
                if not item.referenced_ids() & doomed
            ]
            for item in self.document.objects:
                if item.text_flow_next_id in doomed:
                    item.text_flow_next_id = None
                if item.mask_id in doomed:
                    item.mask_id = None
            self.document.select([])
            self.document.touch()

    def reorder(self, object_id: str, before_id: str | None, parent_id: str | None = None) -> None:
        item = self._editable(object_id)
        if parent_id == item.id or parent_id in {child.id for child in self.document.descendants(item.id)}:
            raise EditError("an object cannot contain itself")
        if parent_id:
            parent = self.document.object(parent_id)
            if parent is None or parent.kind not in CONTAINER_KINDS:
                raise EditError("invalid parent container")
        with self.transaction(f"Reorder {item.name}"):
            self.document.objects.remove(item)
            item.parent_id = parent_id
            if before_id is None:
                insert_at = len(self.document.objects)
            else:
                target = self.document.object(before_id)
                if target is None or target.parent_id != parent_id:
                    raise EditError("invalid insertion target")
                insert_at = self.document.objects.index(target)
            self.document.objects.insert(insert_at, item)
            self.document.touch()

    def create_instance(self, component_id: str, x: float, y: float, parent_id: str | None = None) -> LayoutObject:
        source = next((o for o in self.document.all_objects() if o.id==component_id),None)
        if source is None or source.kind != "component":
            raise EditError("instances can only be created from components")
        instance = LayoutObject(
            new_id("instance"), "instance", f"{source.name} instance", parent_id,
            x, y, source.width, source.height, fill=source.fill, stroke=source.stroke,
            stroke_width=source.stroke_width, radius=source.radius,
            component_source_id=source.id, component_variant=source.component_variant,
        )
        with self.transaction(f"Create {source.name} instance"):
            self.document.objects.append(instance)
            from .components import sync_instance
            from .components import global_graph
            global_graph(self.document,lambda graph:sync_instance(graph,graph.object(source.id),instance))
            self.document.select([instance.id])
            self.document.touch()
        return instance

    def reverse_path(self, object_id: str) -> None:
        item = self._editable(object_id)
        if item.kind != "path":
            raise EditError("choose a path")
        with self.transaction(f"Reverse {item.name}"):
            closed = bool(item.path and item.path[-1].get("closed"))
            # A new list of new nodes: layers are never changed in place, so
            # history and autosave see every change (model.ChangeTracker).
            path = [{key: value for key, value in node.items() if key != "closed"} for node in reversed(item.path)]
            if closed and path:
                path[-1]["closed"] = True
            item.path = path
            self.document.touch()

    # ── pages ────────────────────────────────────────────────────────────

    def show_page(self, page_id: str) -> bool:
        """Switch pages. Navigation, not an edit: it makes no undo step."""
        self.end_gesture()
        return self.document.show_page(page_id)

    def add_page(self, name: str | None = None) -> str:
        from .model import Page
        document = self.document
        document.ensure_pages()
        with self.transaction("Add page"):
            page = Page(new_id("page"), name or f"Page {len(document.pages) + 1}")
            document.pages.insert(document.page_index() + 1, page)
            document.show_page(page.id)
            document.touch()
        return page.id

    def rename_page(self, page_id: str, name: str) -> None:
        name = name.strip()
        page = next((value for value in self.document.pages if value.id == page_id), None)
        if page is None or not name:
            raise EditError("choose a page and a name")
        with self.transaction(f"Rename page to {name}"):
            page.name = name
            self.document.touch()

    def move_page(self, page_id: str, index: int) -> None:
        pages = self.document.pages
        page = next((value for value in pages if value.id == page_id), None)
        if page is None:
            raise EditError("unknown page")
        with self.transaction(f"Move {page.name}"):
            pages.remove(page)
            pages.insert(max(0, min(len(pages), index)), page)
            self.document.touch()

    def delete_page(self, page_id: str) -> None:
        document = self.document
        document.ensure_pages()
        if len(document.pages) < 2:
            raise EditError("a document keeps at least one page")
        page = next((value for value in document.pages if value.id == page_id), None)
        if page is None:
            raise EditError("unknown page")
        with self.transaction(f"Delete {page.name}"):
            if page.id == document.current_page_id:
                index = document.pages.index(page)
                neighbor = document.pages[index + 1] if index + 1 < len(document.pages) else document.pages[index - 1]
                document.show_page(neighbor.id)
            document.pages.remove(page)
            document.touch()

    # ── groups (Figma: a group has no geometry of its own) ───────────────

    def normalize_groups(self) -> None:
        """Groups hug their children; a group left empty is removed."""
        from . import geometry
        from .booleans import update_booleans
        document = self.document
        migrate_group_paint(document)
        update_booleans(document.objects)
        changed = True
        while changed:
            changed = False
            groups = [item for item in document.objects if item.kind == "group"]
            by_parent: dict[str, list[LayoutObject]] = {}
            for item in document.objects:
                if item.parent_id:
                    by_parent.setdefault(item.parent_id, []).append(item)
            for group in groups:
                children = by_parent.get(group.id, [])
                if not children:
                    if group.metadata.get("svg_attributes") is None:
                        document.objects.remove(group)
                        document.selected_ids = [value for value in document.selected_ids if value != group.id]
                        changed = True
                    continue
                # A group whose bottom layer is a mask is as big as its mask (Figma).
                masked = children[0] if geometry.is_active_mask(children[0]) else None
                bounds = geometry.world_bounds(masked) if masked is not None else geometry.union_bounds(children)
                if bounds is None:
                    continue
                values = (bounds[0], bounds[1], bounds[2] - bounds[0], bounds[3] - bounds[1])
                if any(abs(a - b) > 1e-9 for a, b in zip((group.x, group.y, group.width, group.height), values)):
                    group.x, group.y, group.width, group.height = values
                    group.rotation = 0.0

    def _common_parent(self, items: list[LayoutObject]) -> str | None:
        order = {item.id: index for index, item in enumerate(self.document.objects)}
        topmost = max(items, key=lambda item: order[item.id])
        return topmost.parent_id

    def _roots(self, object_ids: list[str]) -> list[LayoutObject]:
        chosen = [self._editable(value) for value in dict.fromkeys(object_ids)]
        chosen_ids = {item.id for item in chosen}
        return [item for item in chosen
                if not any(parent.id in chosen_ids for parent in self.document.ancestors(item.id))]

    def group(self, object_ids: list[str], *, kind: str = "group", name: str | None = None) -> LayoutObject:
        """Ctrl+G (or Ctrl+Alt+G with kind="frame"): wrap layers at the topmost one's place."""
        from . import geometry
        roots = self._roots(object_ids)
        if not roots:
            raise EditError("select layers to group")
        document = self.document
        order = {item.id: index for index, item in enumerate(document.objects)}
        roots.sort(key=lambda item: order[item.id])
        parent_id = self._common_parent(roots)
        bounds = geometry.union_bounds(roots)
        label = "Group selection" if kind == "group" else "Frame selection"
        fields: dict[str, Any] = {"fill": None} if kind == "group" else style_for_new(document.current_style, "frame")
        if kind == "frame":
            # Figma: a frame made around a selection does not clip it.
            fields["clip_content"] = False
        container = LayoutObject(new_id(kind), kind, name or ("Group" if kind == "group" else "Frame"), parent_id,
                                 bounds[0], bounds[1], bounds[2] - bounds[0], bounds[3] - bounds[1], **fields)
        with self.transaction(label):
            insert_at = order[roots[-1].id]
            moving = set()
            for root in roots:
                moving.add(root.id)
            # Place the container where the topmost layer was, then the layers
            # inside it in their order.
            remaining = [item for item in document.objects if item.id not in moving]
            before = sum(1 for item in document.objects[:insert_at] if item.id not in moving)
            for root in roots:
                root.parent_id = container.id
            document.objects = remaining[:before] + [container] + roots + remaining[before:]
            document.select([container.id])
            document.touch()
        return container

    def ungroup(self, object_ids: list[str]) -> list[str]:
        """Ctrl+Shift+G: children keep their places and order in the group's parent."""
        document = self.document
        groups = [self._editable(value) for value in object_ids]
        groups = [item for item in groups if item.kind in {"group", "frame", "section", "boolean"}
                  or item.metadata.get("boolean")]
        if not groups:
            raise EditError("select a group to ungroup")
        released: list[str] = []
        with self.transaction("Ungroup" if len(groups) == 1 else "Ungroup layers"):
            for group in groups:
                children = [item for item in document.objects if item.parent_id == group.id]
                for child in children:
                    child.parent_id = group.parent_id
                    released.append(child.id)
                index = document.objects.index(group)
                document.objects.remove(group)
                for child in children:
                    document.objects.remove(child)
                for offset, child in enumerate(children):
                    document.objects.insert(index + offset, child)
                document.connections = [value for value in document.connections
                                        if group.id not in value.referenced_ids()]
            document.select(released)
            document.touch()
        return released

    # ── booleans (live: the operands stay editable children) ─────────────

    BOOLEAN_PAINT = ("fill", "fill_gradient", "fills", "stroke", "stroke_gradient", "strokes", "stroke_width",
                     "stroke_align", "stroke_cap", "stroke_join", "stroke_dash", "effects", "opacity")
    BOOLEAN_METADATA = ("extra_fills", "extra_strokes", "hidden_fill", "hidden_stroke", "stroke_sides", "blend")

    def boolean(self, object_ids: list[str], operation: str) -> LayoutObject:
        """Union, subtract, intersect or exclude the selection into a live boolean
        layer at the top-most layer's place. A selected boolean changes its operation."""
        from . import booleans, geometry
        if operation not in booleans.OPERATIONS:
            raise EditError(f"unknown boolean operation: {operation}")
        roots = self._roots(object_ids)
        document = self.document
        if len(roots) == 1 and roots[0].kind == booleans.BOOLEAN_KIND:
            self.set_boolean_operation([roots[0].id], operation)
            return roots[0]
        if len(roots) < 2:
            raise EditError("select two or more shapes to combine them")
        order = {item.id: index for index, item in enumerate(document.objects)}
        roots.sort(key=lambda item: order[item.id])
        # Figma: subtract takes the bottom shape's look, the others the top shape's.
        source = roots[0] if operation == "subtract" else roots[-1]
        leaf = next((item for item in [source, *document.descendants(source.id)]
                     if item.kind not in geometry.TRANSPARENT_CONTAINERS), source)
        fields: dict[str, Any] = {key: copy.deepcopy(getattr(leaf, key)) for key in self.BOOLEAN_PAINT}
        metadata = {key: copy.deepcopy(leaf.metadata[key]) for key in self.BOOLEAN_METADATA if key in leaf.metadata}
        metadata["boolean_operation"] = operation
        parent_id = self._common_parent(roots)
        bounds = geometry.union_bounds(roots)
        container = LayoutObject(new_id("boolean"), "boolean", booleans.NAMES[operation], parent_id,
                                 bounds[0], bounds[1], bounds[2] - bounds[0], bounds[3] - bounds[1],
                                 metadata=metadata, **fields)
        with self.transaction(booleans.NAMES[operation]):
            insert_at = order[roots[-1].id]
            moving = {root.id for root in roots}
            remaining = [item for item in document.objects if item.id not in moving]
            before = sum(1 for item in document.objects[:insert_at] if item.id not in moving)
            for root in roots:
                root.parent_id = container.id
            document.objects = remaining[:before] + [container] + roots + remaining[before:]
            document.select([container.id])
            document.touch()
        return container

    def set_boolean_operation(self, object_ids: list[str], operation: str) -> None:
        from . import booleans
        if operation not in booleans.OPERATIONS:
            raise EditError(f"unknown boolean operation: {operation}")
        items = [self._editable(value) for value in object_ids]
        items = [item for item in items if item.kind == booleans.BOOLEAN_KIND]
        if not items:
            raise EditError("select a boolean")
        with self.transaction(f"Change to {booleans.NAMES[operation].lower()}"):
            for item in items:
                item.metadata = {**item.metadata, "boolean_operation": operation}
                if item.name in booleans.NAMES.values():
                    item.name = booleans.NAMES[operation]
            self.document.touch()

    def flatten(self, object_ids: list[str]) -> LayoutObject:
        """Ctrl+E. One layer becomes a vector with its shape; a boolean becomes its
        result. Several layers become one vector holding all their outlines."""
        from . import booleans, geometry
        roots = self._roots(object_ids)
        roots = [item for item in roots if item.kind not in {"frame", "page", "section", "component", "instance"}]
        if not roots:
            raise EditError("select shapes to flatten")
        document = self.document
        order = {item.id: index for index, item in enumerate(document.objects)}
        roots.sort(key=lambda item: order[item.id])
        with self.transaction("Flatten"):
            if len(roots) == 1 and not (roots[0].kind == "group"):
                item = roots[0]
                found = booleans.flattened_nodes(item)
                if found is None:
                    raise EditError(f"{item.name} has no shape to flatten")
                nodes, box = found
                if len(nodes) < 2:
                    raise EditError(f"{item.name} has no shape to flatten")
                doomed = {child.id for child in document.descendants(item.id)}
                document.objects = [value for value in document.objects if value.id not in doomed]
                if item.kind != "path":
                    item.name = item.name if item.kind == "boolean" or item.name not in KIND_NAMES.values() else "Vector"
                item.kind = "path"
                item.path = nodes
                item.x, item.y, item.width, item.height = box
                item.radius, item.corner_radii, item.point_count = 0.0, [], 0
                item.metadata = {key: value for key, value in item.metadata.items()
                                 if key not in {"boolean_operation", "boolean_key"}}
                result = item
            else:
                leaves = [leaf for root in roots for leaf in [root, *document.descendants(root.id)]
                          if leaf.visible and leaf.kind not in geometry.TRANSPARENT_CONTAINERS
                          and not any(parent.kind == "boolean" for parent in document.ancestors(leaf.id))]
                subpaths = [shape for leaf in leaves for shape in booleans._world_subpaths(leaf)]
                if not subpaths:
                    raise EditError("nothing to flatten")
                left, top, right, bottom = geometry.subpath_bounds(subpaths)
                top_leaf = leaves[-1]
                fields = {key: copy.deepcopy(getattr(top_leaf, key)) for key in self.BOOLEAN_PAINT}
                result = LayoutObject(new_id("path"), "path", "Vector", self._common_parent(roots), left, top,
                                      max(right - left, 0.01), max(bottom - top, 0.01),
                                      path=booleans.to_nodes(subpaths, (left, top)), **fields)
                doomed = {value.id for root in roots for value in [root, *document.descendants(root.id)]}
                insert_at = order[roots[-1].id]
                before = sum(1 for value in document.objects[:insert_at] if value.id not in doomed)
                remaining = [value for value in document.objects if value.id not in doomed]
                document.objects = remaining[:before] + [result] + remaining[before:]
            document.select([result.id])
            document.touch()
        return result

    # ── masks (Figma: the bottom layer masks the layers above it) ─────────

    def use_as_mask(self, object_ids: list[str], *, mode: str = "alpha") -> LayoutObject:
        """Ctrl+Alt+M. One layer becomes a mask for the layers above it in its
        parent; several layers are grouped into a "Mask group" first, with the
        bottom-most one as the mask. Returns the mask layer."""
        from . import geometry
        if mode not in geometry.MASK_MODES:
            raise EditError(f"unknown mask type: {mode}")
        roots = self._roots(object_ids)
        if not roots:
            raise EditError("select layers to mask")
        document = self.document
        order = {item.id: index for index, item in enumerate(document.objects)}
        roots.sort(key=lambda item: order[item.id])
        with self.transaction("Use as mask"):
            if len(roots) > 1:
                parents = {item.parent_id for item in roots}
                if len(parents) > 1:
                    raise EditError("masks work on layers in the same group or frame")
                group = self.group([item.id for item in roots], name="Mask group")
                group.metadata = {**group.metadata, "mask_group": True}
            mask = roots[0]
            mask.metadata = {**mask.metadata, "mask": mode}
            self.normalize_groups()
            document.select([mask.parent_id] if len(roots) > 1 and mask.parent_id else [mask.id])
            document.touch()
        return mask

    def release_mask(self, object_ids: list[str]) -> list[str]:
        """Ctrl+Alt+M on a mask or a mask group: the mask becomes a plain layer
        again, and a group made by Use as mask goes away."""
        from . import geometry
        document = self.document
        chosen = [self._editable(value) for value in dict.fromkeys(object_ids)]
        masks: list[LayoutObject] = []
        for item in chosen:
            if geometry.mask_mode(item):
                masks.append(item)
            else:
                masks.extend(child for child in document.children(item.id) if geometry.mask_mode(child))
        if not masks:
            raise EditError("select a mask to release")
        released: list[str] = []
        with self.transaction("Release mask"):
            for mask in masks:
                mask.metadata = {key: value for key, value in mask.metadata.items() if key != "mask"}
                released.append(mask.id)
                parent = document.object(mask.parent_id) if mask.parent_id else None
                if parent is not None and parent.metadata.get("mask_group") and \
                        not any(geometry.mask_mode(child) for child in document.children(parent.id)):
                    released.remove(mask.id)
                    released.extend(self.ungroup([parent.id]))
            document.select([value for value in dict.fromkeys(released) if document.object(value)])
            document.touch()
        return released

    def set_mask_mode(self, object_ids: list[str], mode: str) -> None:
        from . import geometry
        if mode not in geometry.MASK_MODES:
            raise EditError(f"unknown mask type: {mode}")
        items = [self._editable(value) for value in object_ids]
        with self.transaction("Change mask type"):
            for item in items:
                if geometry.mask_mode(item):
                    item.metadata = {**item.metadata, "mask": mode}
            self.document.touch()

    def reset_instance_overrides(self, object_id: str) -> None:
        item = self._editable(object_id)
        if item.kind != "instance":
            raise EditError("choose a component instance")
        with self.transaction(f"Reset {item.name} overrides"):
            item.metadata = {key: value for key, value in item.metadata.items() if key != "overrides"}
            item.overrides = {}
            for child in self.document.descendants(item.id):
                child.overrides = {}
                child.metadata={key:value for key,value in child.metadata.items() if key!="component_position_override"}
            from .components import sync_instance
            from .components import global_graph
            global_graph(self.document,lambda graph:sync_instance(graph,graph.object(item.component_source_id),item))
            self.document.touch()
