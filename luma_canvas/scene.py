# SPDX-License-Identifier: MPL-2.0
"""The scene renderer: document objects as GSK render nodes.

Each object's own drawing (fills, strokes, effects, text) is built once in its
local box and cached by a fingerprint of the fields that affect it. A frame
is composed from its children's cached nodes plus transforms, so moving an
object, panning and zooming rebuild nothing but transforms.

What is drawn: true béziers from `geometry.outline`, per-corner radii and
smoothed (squircle) corners, stroke alignment (inside, center, outside),
dashes, caps and joins, line end caps, every paint of a layer's fill and
stroke stacks with its own opacity and blend mode, linear, radial, angular and
diamond gradients, image paints, text (fills, stroke and per-run styles), drop
shadow (with or without showing behind a see-through layer), inner shadow,
layer blur, background blur, blur that grows along a line (progressive),
a group's shadow and blur cast by its layers together, layer blend modes
against what is beneath them (groups pass through, as in Figma), layer
opacity, flips, rotation, shear, and frame clipping. Paint is sRGB, as
exported.

Imported layers (see docs/fig-import.md) may also carry, in `metadata`:
`fill_rule` ("evenodd"), `blend` (the layer's blend mode), `corner_smoothing`,
`skew`, `stroke_sides` (per-side stroke weights), `extra_fills` (paints above
the fill, from imports made before paint stacks), and on frames `clip_path`
(path nodes the frame clips its children to, for masks).
"""

from __future__ import annotations

import json
import time
import math
from typing import Any

import gi

gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import geometry  # noqa: E402
from .model import ChangeTracker, asset_bytes  # noqa: E402
from .paint import (Color, color_of, group_effects_of, has_paint, normalized_effect, paint_color,  # noqa: E402
                    sorted_stops, visible_paints)
from .typography import build_layout, type_settings  # noqa: E402


def rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def point(x: float, y: float) -> Graphene.Point:
    return Graphene.Point().init(x, y)


def rgba(color: Color, opacity: float = 1.0) -> Gdk.RGBA:
    value = Gdk.RGBA()
    value.red, value.green, value.blue = color.r, color.g, color.b
    value.alpha = max(0.0, min(1.0, color.a * opacity))
    return value


def gsk_path(shapes: list[geometry.Subpath], transform=None) -> Gsk.Path | None:
    """Build a Gsk.Path, optionally mapping every point through `transform`."""
    builder = Gsk.PathBuilder.new()
    empty = True
    for shape in shapes:
        def map_point(x: float, y: float) -> tuple[float, float]:
            return transform(x, y) if transform else (x, y)
        builder.move_to(*map_point(*shape.start))
        for segment in shape.segments:
            empty = False
            if segment[0] == "L":
                builder.line_to(*map_point(segment[1], segment[2]))
            else:
                x1, y1 = map_point(segment[1], segment[2])
                x2, y2 = map_point(segment[3], segment[4])
                x3, y3 = map_point(segment[5], segment[6])
                builder.cubic_to(x1, y1, x2, y2, x3, y3)
        if shape.closed:
            builder.close()
    if empty:
        return None
    return builder.to_path()


def cairo_path_to_gsk(context) -> "Gsk.Path | None":
    """A cairo path (what PangoCairo draws glyph outlines into) as a Gsk.Path."""
    import cairo
    builder = Gsk.PathBuilder.new()
    empty = True
    for kind, points in context.copy_path():
        if kind == cairo.PATH_MOVE_TO:
            builder.move_to(points[0], points[1])
        elif kind == cairo.PATH_LINE_TO:
            builder.line_to(points[0], points[1])
            empty = False
        elif kind == cairo.PATH_CURVE_TO:
            builder.cubic_to(*points)
            empty = False
        elif kind == cairo.PATH_CLOSE_PATH:
            builder.close()
    return None if empty else builder.to_path()


def affine_transform(a: float, b: float, c: float, d: float, e: float, f: float) -> Gsk.Transform:
    """x' = a x + c y + e, y' = b x + d y + f as a GSK transform.

    Built from translate, rotate, skew and scale rather than a raw matrix: a
    matrix transform is a 3D transform to GSK, which the Cairo renderer
    cannot draw, while these keep the transform in the 2D category.
    """
    sx = math.hypot(a, b)
    if sx < 1e-12:
        return Gsk.Transform.new().matrix(Graphene.Matrix().init_from_2d(a, b, c, d, e, f))
    sy = (a * d - b * c) / sx
    shear = (a * c + b * d) / sx
    transform = Gsk.Transform.new().translate(point(e, f)).rotate(math.degrees(math.atan2(b, a)))
    if abs(sy) > 1e-12 and abs(shear) > 1e-9:
        transform = transform.skew(math.degrees(math.atan(shear / sy)), 0.0)
    return transform.scale(sx, sy if abs(sy) > 1e-12 else 1e-12)


def invert_affine(a: float, b: float, c: float, d: float, e: float, f: float):
    det = a * d - b * c
    if abs(det) < 1e-12 or not math.isfinite(det):
        return None
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def mapped_bounds(matrix, left: float, top: float, right: float, bottom: float) -> Graphene.Rect:
    a, b, c, d, e, f = matrix
    xs, ys = [], []
    for x, y in ((left, top), (right, top), (right, bottom), (left, bottom)):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return rect(min(xs), min(ys), max(max(xs) - min(xs), 1e-3), max(max(ys) - min(ys), 1e-3))


def fill_rule_of(item: Any) -> Gsk.FillRule:
    return Gsk.FillRule.EVEN_ODD if geometry.fill_rule(item) == "evenodd" else Gsk.FillRule.WINDING


def color_stops(paint: dict[str, Any], opacity: float) -> list[Gsk.ColorStop]:
    stops = []
    last = -1.0
    for stop in sorted_stops(paint):
        value = Gsk.ColorStop()
        # GSK requires non-decreasing offsets in 0..1.
        offset = max(last, max(0.0, min(1.0, float(stop["position"]))))
        value.offset = offset
        value.color = rgba(Color.from_dict(stop["color"]), opacity)
        stops.append(value)
        last = offset
    if len(stops) == 1:
        extra = Gsk.ColorStop()
        extra.offset = 1.0
        extra.color = stops[0].color
        stops.append(extra)
    return stops


BOX_KINDS = {"rectangle", "frame", "page", "component", "section", "instance", "layout"}

BLEND_NODE_MODES = {
    "darken": "DARKEN", "multiply": "MULTIPLY", "color-burn": "COLOR_BURN", "lighten": "LIGHTEN",
    "screen": "SCREEN", "color-dodge": "COLOR_DODGE", "overlay": "OVERLAY", "soft-light": "SOFT_LIGHT",
    "hard-light": "HARD_LIGHT", "difference": "DIFFERENCE", "exclusion": "EXCLUSION", "hue": "HUE",
    "saturation": "SATURATION", "color": "COLOR", "luminosity": "LUMINOSITY",
}


def container(first: Gsk.RenderNode | None, second: Gsk.RenderNode | None) -> Gsk.RenderNode | None:
    if first is None:
        return second
    if second is None:
        return first
    return Gsk.ContainerNode.new([first, second])


PROGRESSIVE_STEPS = 12


def blurred_node(node: Gsk.RenderNode, radius: float) -> Gsk.RenderNode:
    return Gsk.BlurNode.new(node, radius) if radius > 0.05 else node


def progressive_blur_node(node: Gsk.RenderNode, effect: dict[str, Any], item: Any,
                          to_world: Any = None) -> Gsk.RenderNode:
    """`node` blurred more and more along the effect's line (Figma's
    progressive blur).

    The line is cut into strips across its direction, and each strip is drawn
    from a copy of the node blurred by the amount that belongs there. Strips
    overlap by a pixel, so no seam between them loses coverage, and there are
    enough of them that the step from one to the next is a blur radius or two.
    """
    light, heavy = effect["blur"], max(effect["end_blur"], effect["blur"])
    if heavy - light < 0.5 or not effect["progressive"] or effect["from"] is None or effect["to"] is None:
        return blurred_node(node, heavy)
    width, height = max(float(item.width), 1e-6), max(float(item.height), 1e-6)

    def at(fraction: float) -> tuple[float, float]:
        x = (effect["from"][0] + (effect["to"][0] - effect["from"][0]) * fraction) * width
        y = (effect["from"][1] + (effect["to"][1] - effect["from"][1]) * fraction) * height
        return to_world(x, y) if to_world is not None else (x, y)

    bounds = node.get_bounds()
    first, last = at(0.0), at(1.0)
    span = math.hypot(last[0] - first[0], last[1] - first[1])
    if bounds is None or span < 1e-6:
        return blurred_node(node, heavy)
    # Along the line, and across it far enough to cover everything drawn.
    along = ((last[0] - first[0]) / span, (last[1] - first[1]) / span)
    across = (-along[1], along[0])
    reach = math.hypot(bounds.get_width(), bounds.get_height()) + heavy * 3 + 4
    snapshot = Gtk.Snapshot()
    steps = PROGRESSIVE_STEPS
    for index in range(steps):
        start_at = span * index / steps - (1.0 if index else reach)
        end_at = span * (index + 1) / steps + (0.0 if index < steps - 1 else reach)
        builder = Gsk.PathBuilder.new()
        for corner, (distance, side) in enumerate(((start_at, -1), (end_at, -1), (end_at, 1), (start_at, 1))):
            x = first[0] + along[0] * distance + across[0] * side * reach
            y = first[1] + along[1] * distance + across[1] * side * reach
            builder.move_to(x, y) if corner == 0 else builder.line_to(x, y)
        builder.close()
        middle = (index + 0.5) / steps
        snapshot.push_fill(builder.to_path(), Gsk.FillRule.WINDING)
        snapshot.append_node(blurred_node(node, light + (heavy - light) * middle))
        snapshot.pop()
    return snapshot.to_node() or node


def background_blur_effect(item: Any) -> dict[str, Any] | None:
    """The layer's strongest background blur, or None."""
    best = None
    for effect in getattr(item, "effects", None) or []:
        if isinstance(effect, dict):
            value = normalized_effect(effect)
            if value and value["kind"] == "background_blur":
                if best is None or max(value["blur"], value["end_blur"]) > max(best["blur"], best["end_blur"]):
                    best = value
    return best


def needs_backdrop(item: Any) -> bool:
    return ((getattr(item, "metadata", None) or {}).get("blend") in BLEND_NODE_MODES
            or background_blur_effect(item) is not None)


def passes_through(tree: Any, item: Any) -> bool:
    """A group without opacity, blend, mask or effects of its own lets its
    layers blend with what is below the group (Figma's Pass through)."""
    return (item.kind in {"group", "layout"} and float(item.opacity) >= 1.0
            and not (item.metadata or {}).get("blend") and not item.mask_id
            and not group_effects_of(item)
            and bool(tree.children.get(item.id)))


def backdrop_parents(tree: Any, objects: Any = None) -> set[str | None]:
    """The layer lists that need to be composed one layer at a time (among
    `objects`, every layer by default)."""
    keys: set[str | None] = set()
    for item in (tree.objects if objects is None else objects):
        if not needs_backdrop(item):
            continue
        parent = tree.parent(item)
        while True:
            keys.add(parent.id if parent is not None else None)
            if parent is None or not passes_through(tree, parent):
                break
            parent = tree.parent(parent)
    return keys


def opaque_fill(item: Any) -> bool:
    paints = visible_paints(item, "fill")
    if len(paints) != 1 or paints[0].get("type") != "solid" or float(item.opacity) < 1.0:
        return False
    color = color_of(paints[0].get("color"))
    return color is not None and color.a * float(paints[0].get("opacity", 1.0)) >= 0.999


def compose_blend(below: Gsk.RenderNode | None, top: Gsk.RenderNode, mode: str | None) -> Gsk.RenderNode:
    """`top` over `below` with a CSS blend mode (Gsk.BlendNode), or plainly."""
    name = BLEND_NODE_MODES.get(str(mode or "normal"))
    if below is None:
        return top
    if name is None:
        return Gsk.ContainerNode.new([below, top])
    return Gsk.BlendNode.new(below, top, getattr(Gsk.BlendMode, name))

def append_clipped(snapshot: Gtk.Snapshot, content: Gtk.Snapshot, bounds: Graphene.Rect | None = None,
                   rounded: Gsk.RoundedRect | None = None) -> None:
    """Append `content` clipped to a rectangle or rounded rectangle.

    GTK 4.22's GPU renderers try a clip node as an occlusion candidate and, when
    its content then fails that pass, restore the scissor but not the clip
    (gskgpunodeprocessor.c, add_first_node_clipped). Later layers are drawn
    under the stale clip: they vanish on partial redraws and GSK logs
    "Clipping is broken" (seen on Intel GPUs). An opacity node of 1 has no
    occlusion handler, so the clip inside it is never a candidate; it costs no
    offscreen, since opacity is handed down to the content.
    """
    node = content.to_node()
    if node is None:
        return
    clip = Gsk.RoundedClipNode.new(node, rounded) if rounded is not None else Gsk.ClipNode.new(node, bounds)
    snapshot.append_node(Gsk.OpacityNode.new(clip, 1.0))


CAPS = {"butt": Gsk.LineCap.BUTT, "none": Gsk.LineCap.BUTT, "round": Gsk.LineCap.ROUND,
        "square": Gsk.LineCap.SQUARE}
JOINS = {"miter": Gsk.LineJoin.MITER, "round": Gsk.LineJoin.ROUND, "bevel": Gsk.LineJoin.BEVEL}


def effect_margin(item: Any) -> float:
    margin = 0.0
    for effect in getattr(item, "effects", []) or []:
        value = normalized_effect(effect) if isinstance(effect, dict) else None
        if value:
            margin = max(margin, value["blur"] * 2 + abs(value["x"]) + abs(value["y"])
                         + abs(value["spread"]))
    stroke = float(getattr(item, "stroke_width", 0) or 0)
    return margin + stroke * 2 + 2


def snap_box(item: Any, grid: float | None) -> tuple[float, float, float, float] | None:
    """An unrotated box's edges on whole device pixels, or None.

    `grid` is device pixels per document unit. Both edges round the same way,
    so abutting boxes share their edge pixel exactly.
    """
    if not grid or getattr(item, "rotation", 0.0) or geometry.skew_of(item):
        return None
    left = round(item.x * grid) / grid
    top = round(item.y * grid) / grid
    right = round((item.x + item.width) * grid) / grid
    bottom = round((item.y + item.height) * grid) / grid
    return left, top, right, bottom


class _RootEntry:
    """A top-level layer composed once: its node, the world area it draws
    into, the ids inside it, and the snapping grid it was built for."""

    __slots__ = ("grid", "node", "bounds", "members", "changed")

    def __init__(self, grid, node, bounds, members) -> None:
        self.grid, self.node, self.bounds, self.members = grid, node, bounds, members
        self.changed = False


class SceneRenderer:
    """Builds and caches render nodes for a document."""

    # Milliseconds of deferred work (roots never built, or built at another
    # zoom) a frame may take; the rest follows on later frames.
    FRAME_BUDGET_MS = 8.0

    def __init__(self, widget: Gtk.Widget | None) -> None:
        # Without a widget (export, thumbnails) text is laid out with Pango's
        # cairo font map, which needs no display.
        self.widget = widget
        self._content: dict[str, tuple[str, Gsk.RenderNode | None]] = {}
        self._textures: dict[str, Gdk.Texture | None] = {}
        self._pango: Pango.Context | None = None
        # The canvas keeps one composed node per top-level layer and rebuilds
        # only the ones whose layers changed (a huge document has thousands).
        self._roots: dict[str, _RootEntry] = {}
        self._root_of: dict[str, str] = {}
        self._tree_seen: Any = None
        self._tracker = ChangeTracker().open() if widget is not None else None
        # True when the last scene still shows roots waiting to be rebuilt.
        self.stale = False

    def invalidate(self) -> None:
        self._content.clear()
        self._roots.clear()
        self._root_of = {}
        self._tree_seen = None
        if self._tracker is not None:
            self._tracker.take()

    def close(self) -> None:
        """Stop hearing about layer changes (the canvas is not shown)."""
        if self._tracker is not None:
            self._tracker.close()
            self._tracker = None

    def resume(self) -> None:
        """Hear about layer changes again and start the per-layer cache over."""
        if self.widget is None:
            return
        if self._tracker is None:
            self._tracker = ChangeTracker()
        self._tracker.open()
        self.invalidate()

    # ── fingerprints ─────────────────────────────────────────────────────

    FIELDS = ("kind", "width", "height", "fill", "fill_gradient", "fills", "stroke", "stroke_gradient", "strokes",
              "stroke_width", "stroke_align", "stroke_cap", "stroke_join", "stroke_dash",
              "radius", "corner_radii", "path", "effects", "text", "metadata", "point_count",
              "inner_ratio", "start_cap", "end_cap", "asset_id")

    def fingerprint(self, item: Any) -> str:
        return json.dumps([getattr(item, name, None) for name in self.FIELDS],
                          default=str, separators=(",", ":"))

    # ── the scene ────────────────────────────────────────────────────────

    def scene(self, document: Any, *, visible: tuple[float, float, float, float] | None = None,
              outline_mode: bool = False, zoom: float = 1.0,
              device_scale: float | None = None, tree: geometry.Tree | None = None) -> Gsk.RenderNode | None:
        """The document in world coordinates, culled to `visible` if given.

        With `device_scale` (zoom times the display scale), unrotated boxes
        are snapped to whole device pixels, so a frame's edge and a child's
        edge on it land on the same pixel and nothing between them lets the
        canvas ground through. The caller must also place the scene at a
        whole device pixel.

        The canvas's renderer composes each top-level layer once and reuses it
        until a layer inside it changes; `tree` is the caller's layer tree,
        which must be a new object whenever layers are added, removed,
        reordered or reparented.
        """
        tree = tree if tree is not None else geometry.Tree(document.objects)
        self._document = document
        self._grid = device_scale
        roots = tree.children.get(None, [])
        if self._tracker is not None and not outline_mode and not self._page_level_backdrop(tree, roots):
            return self._scene_from_roots(tree, roots, visible, zoom)
        self.stale = False
        snapshot = Gtk.Snapshot()
        seen: set[str] = set()
        self._backdrop_keys = backdrop_parents(tree) if not outline_mode else set()
        self._compose_children(snapshot, tree, roots, visible, outline_mode, zoom, seen)
        for stale in [key for key in self._content if key.split("#", 1)[0] not in seen]:
            del self._content[stale]
        return snapshot.to_node()

    # ── one node per top-level layer ─────────────────────────────────────

    @staticmethod
    def _page_level_backdrop(tree: geometry.Tree, roots: list[Any]) -> bool:
        """Whether top-level layers interact (a mask among them, or a blend or
        background blur that sees the page), so the page composes as a whole."""
        pending = list(roots)
        top = True
        while pending:
            next_level = []
            for item in pending:
                if top and geometry.mask_mode(item):
                    return True
                if needs_backdrop(item):
                    return True
                if passes_through(tree, item):
                    next_level.extend(tree.children.get(item.id, []))
            pending, top = next_level, False
        return False

    def _members(self, tree: geometry.Tree, root: Any) -> list[Any]:
        members = [root]
        pending = [root.id]
        while pending:
            children = tree.children.get(pending.pop(), [])
            members.extend(children)
            pending.extend(child.id for child in children)
        return members

    def _scene_from_roots(self, tree: geometry.Tree, roots: list[Any], visible, zoom: float) -> Gsk.RenderNode | None:
        assert self._tracker is not None
        dirty = self._tracker.take()
        entries = self._roots
        if tree is not self._tree_seen:
            # Layers were added, removed, reordered or reparented: find each
            # layer's top-level layer again and drop what no longer matches.
            self._tree_seen = tree
            root_of: dict[str, str] = {}
            memberships: dict[str, tuple[str, ...]] = {}
            for root in roots:
                members = tuple(member.id for member in self._members(tree, root))
                memberships[root.id] = members
                for member in members:
                    root_of[member] = root.id
            self._root_of = root_of
            for root_id in list(entries):
                if root_id not in memberships or entries[root_id].members != memberships[root_id]:
                    del entries[root_id]
            for key in [key for key in self._content if key.split("#", 1)[0] not in tree.by_id]:
                del self._content[key]
        for object_id in dirty:
            item = tree.by_id.get(object_id)
            if item is not None and item.kind in {"mask", "component"}:
                # Other layers may draw through it (a mask) or copy it.
                entries.clear()
                break
            root_id = self._root_of.get(object_id)
            if root_id is not None:
                entry = entries.get(root_id)
                if entry is not None:
                    entry.changed = True

        grid = self._grid
        start = time.monotonic()

        def in_view(bounds) -> bool:
            return (visible is None or bounds is None
                    or not (bounds[2] < visible[0] or bounds[0] > visible[2]
                            or bounds[3] < visible[1] or bounds[1] > visible[3]))

        # A root whose layers changed is rebuilt now. A root never built, or
        # built at another zoom, waits for the frame budget, visible ones first.
        waiting: list[Any] = []
        for root in roots:
            entry = entries.get(root.id)
            if entry is not None and entry.changed:
                entries[root.id] = self._build_root(tree, root, zoom)
            elif entry is None or entry.grid != grid:
                if in_view(entry.bounds if entry is not None else geometry.world_bounds(root)):
                    waiting.append(root)
        waiting.sort(key=lambda root: 0 if in_view(entries[root.id].bounds if root.id in entries
                                                   else geometry.world_bounds(root)) else 1)
        stale = False
        built = 0
        for root in waiting:
            # Every frame makes progress: the first waiting root is always
            # built, the rest only while the budget lasts. Opening a document
            # whose every frame is in view fills the page over a few frames
            # instead of freezing on the first one.
            if built > 0 and (time.monotonic() - start) * 1000 > self.FRAME_BUDGET_MS:
                stale = True
                continue
            entries[root.id] = self._build_root(tree, root, zoom)
            built += 1
        self.stale = stale
        nodes = [entries[root.id].node for root in roots
                 if root.id in entries and entries[root.id].node is not None and in_view(entries[root.id].bounds)]
        if not nodes:
            return None
        return nodes[0] if len(nodes) == 1 else Gsk.ContainerNode.new(nodes)

    def _build_root(self, tree: geometry.Tree, root: Any, zoom: float) -> "_RootEntry":
        from .export import render_bounds
        members = self._members(tree, root)
        self._backdrop_keys = backdrop_parents(tree, members)
        snapshot = Gtk.Snapshot()
        self._compose(snapshot, tree, root, None, False, zoom, set())
        return _RootEntry(self._grid, snapshot.to_node(), render_bounds(tree, root),
                          tuple(member.id for member in members))

    def _compose_children(self, snapshot: Gtk.Snapshot, tree: geometry.Tree, children: list[Any], visible,
                          outline_mode: bool, zoom: float, seen: set[str],
                          seed: Gsk.RenderNode | None = None) -> None:
        """Siblings in paint order. A mask layer is not drawn: it masks every
        sibling above it (Figma), with its alpha, luminance or outline."""
        if children and not outline_mode and (tree.parent(children[0]).id if tree.parent(children[0]) else None) \
                in getattr(self, "_backdrop_keys", set()):
            node = self._accumulate(tree, children, visible, zoom, seen, None, seed)
            if node is not None:
                snapshot.append_node(node)
            return
        for index, child in enumerate(children):
            mode = geometry.mask_mode(child)
            if mode is None or not child.visible:
                self._compose(snapshot, tree, child, visible, outline_mode, zoom, seen)
                continue
            seen.add(child.id)
            rest = children[index + 1:]
            if outline_mode:
                self._compose(snapshot, tree, child, visible, outline_mode, zoom, seen)
                self._compose_children(snapshot, tree, rest, visible, outline_mode, zoom, seen)
                return
            if not rest:
                return
            if mode == "outline" or not self._push_mask_of(snapshot, tree, child, mode, visible, zoom, seen):
                clip = gsk_path(geometry.outline(child), lambda x, y, item=child: geometry.to_world(item, x, y))
                if clip is None:
                    # A mask with no area hides everything it masks.
                    for hidden in rest:
                        seen.add(hidden.id)
                    return
                snapshot.push_fill(clip, fill_rule_of(child))
            self._compose_children(snapshot, tree, rest, visible, outline_mode, zoom, seen)
            snapshot.pop()
            return

    def _accumulate(self, tree: geometry.Tree, children: list[Any], visible, zoom: float, seen: set[str],
                    below: Gsk.RenderNode | None, seed: Gsk.RenderNode | None) -> Gsk.RenderNode | None:
        """Siblings composed one by one, so a layer can blend with what is below
        it or blur it (background blur). Groups pass through: their layers blend
        with the layers under the group, as in Figma."""
        for index, child in enumerate(children):
            if not child.visible or child.kind in {"guide", "comment", "slice"}:
                continue
            mode = geometry.mask_mode(child)
            if mode is not None:
                seen.add(child.id)
                rest = children[index + 1:]
                if not rest:
                    break
                inner = self._accumulate(tree, rest, visible, zoom, seen, None, container(seed, below))
                if inner is None:
                    break
                masked = Gtk.Snapshot()
                if mode == "outline" or not self._push_mask_of(masked, tree, child, mode, visible, zoom, seen):
                    clip = gsk_path(geometry.outline(child), lambda x, y, item=child: geometry.to_world(item, x, y))
                    if clip is None:
                        break
                    masked.push_fill(clip, fill_rule_of(child))
                masked.append_node(inner)
                masked.pop()
                below = container(below, masked.to_node())
                break
            if passes_through(tree, child):
                seen.add(child.id)
                below = self._accumulate(tree, tree.children.get(child.id, []), visible, zoom, seen, below, seed)
                continue
            layer = Gtk.Snapshot()
            self._compose(layer, tree, child, visible, False, zoom, seen)
            node = layer.to_node()
            if node is None:
                continue
            effect = background_blur_effect(child)
            backdrop = container(seed, below)
            if effect is not None and max(effect["blur"], effect["end_blur"]) > 0 and backdrop is not None:
                clip = gsk_path(geometry.outline(child), lambda x, y, item=child: geometry.to_world(item, x, y))
                if clip is not None:
                    blurred = Gtk.Snapshot()
                    blurred.push_fill(clip, fill_rule_of(child))
                    blurred.append_node(progressive_blur_node(
                        backdrop, effect, child,
                        lambda x, y, item=child: geometry.to_world(item, x, y)))
                    blurred.pop()
                    below = container(below, blurred.to_node())
            blend = (child.metadata or {}).get("blend", "normal")
            if blend in BLEND_NODE_MODES and seed is not None and below is None:
                # Blend with the frame's own (opaque) fill beneath its first child.
                below = compose_blend(seed, node, blend)
            else:
                below = compose_blend(below, node, blend)
        return below

    @staticmethod
    def _append_group_effects(outer: Gtk.Snapshot, content: Gtk.Snapshot,
                              effects: list[dict[str, Any]]) -> None:
        node = content.to_node()
        if node is None:
            return
        shadows = []
        for effect in effects:
            if effect["kind"] != "drop_shadow":
                continue
            shadow = Gsk.Shadow()
            shadow.color = rgba(effect["color"])
            shadow.dx, shadow.dy, shadow.radius = effect["x"], effect["y"], effect["blur"]
            shadows.append(shadow)
        blur = max((effect["blur"] for effect in effects if effect["kind"] == "layer_blur"), default=0.0)
        if blur > 0:
            outer.push_blur(blur)
        if shadows:
            outer.push_shadow(shadows)
        outer.append_node(node)
        if shadows:
            outer.pop()
        if blur > 0:
            outer.pop()

    def _push_mask_of(self, snapshot: Gtk.Snapshot, tree: geometry.Tree, mask: Any, mode: str, visible,
                      zoom: float, seen: set[str]) -> bool:
        """Push a GSK mask recorded from the mask layer's own drawing; the
        caller draws the masked layers and pops."""
        recorder = Gtk.Snapshot()
        self._compose(recorder, tree, mask, None, False, zoom, seen)
        node = recorder.to_node()
        if node is None:
            return False
        luminance = getattr(Gsk.MaskMode, "LUMINANCE", None)
        snapshot.push_mask(luminance if mode == "luminance" and luminance is not None else Gsk.MaskMode.ALPHA)
        snapshot.append_node(node)
        snapshot.pop()
        return True

    def _compose(self, snapshot: Gtk.Snapshot, tree: geometry.Tree, item: Any, visible,
                 outline_mode: bool, zoom: float, seen: set[str], *, own_mask: bool = True) -> None:
        if not item.visible or item.kind in {"guide", "comment", "slice"}:
            return
        if own_mask and item.mask_id:
            mask = tree.by_id.get(item.mask_id)
            if mask is not None and mask.kind == "mask":
                # Older documents: a `mask` layer referenced by `mask_id` clips by its box.
                clip = gsk_path(geometry.outline(mask), lambda x, y: geometry.to_world(mask, x, y))
                if clip is not None:
                    snapshot.push_fill(clip, Gsk.FillRule.WINDING)
                    self._compose(snapshot, tree, item, visible, outline_mode, zoom, seen, own_mask=False)
                    snapshot.pop()
                    return
        seen.add(item.id)
        children = tree.children.get(item.id, [])
        if visible is not None and not children:
            left, top, right, bottom = geometry.world_bounds(item)
            margin = effect_margin(item)
            if (right + margin < visible[0] or left - margin > visible[2]
                    or bottom + margin < visible[1] or top - margin > visible[3]):
                return
        opacity = max(0.0, min(1.0, float(item.opacity)))
        if opacity < 1.0:
            snapshot.push_opacity(opacity)
        # A group has no shape of its own, so its shadow and blur are drawn on
        # everything inside it at once, cast by their shape together (Figma).
        group_effects = group_effects_of(item) if children and not outline_mode else []
        outer_group = snapshot
        if group_effects:
            snapshot = Gtk.Snapshot()
        # A frame's stroke is drawn over its children, and outside its clip, so
        # an outside stroke still shows (Figma).
        stroke_on_top = (bool(children) and item.kind in geometry.FRAME_LIKE and not outline_mode
                         and item.stroke_width > 0 and has_paint(item, "stroke"))
        if item.kind not in geometry.TRANSPARENT_CONTAINERS or item.kind == "layout":
            node = (self._outline_node(item, zoom) if outline_mode
                    else self.content_node(item, "body" if stroke_on_top else "all"))
            if node is not None:
                snapshot.save()
                self._apply_transform(snapshot, item, self._grid)
                snapshot.append_node(node)
                snapshot.restore()
        if children and item.kind != "boolean":
            clipped = geometry.clips(item)
            custom_clip = (item.metadata or {}).get("clip_path") if clipped else None
            # Box clips record the children apart and clip them as a node.
            clip_bounds: Graphene.Rect | None = None
            clip_rounded: Gsk.RoundedRect | None = None
            outer = snapshot
            if clipped and custom_clip:
                clip = gsk_path(geometry.nodes_outlines(custom_clip), lambda x, y: geometry.to_world(item, x, y))
                if clip is not None:
                    snapshot.push_fill(clip, Gsk.FillRule.WINDING)
                else:
                    clipped = False
            elif clipped:
                box = snap_box(item, self._grid)
                radii = geometry.corner_radii_of(item) if item.kind != "section" else (0, 0, 0, 0)
                if any(radii) and geometry.corner_smoothing_of(item) > 1e-4:
                    box = None   # smoothed corners clip by their own path
                if box is not None and not any(radii):
                    # A rectangle clip on whole pixels: no antialiased fringe.
                    clip_bounds = rect(box[0], box[1], box[2] - box[0], box[3] - box[1])
                elif box is not None:
                    clip_rounded = Gsk.RoundedRect()
                    size = lambda radius: Graphene.Size().init(radius, radius)  # noqa: E731
                    clip_rounded.init(rect(box[0], box[1], box[2] - box[0], box[3] - box[1]),
                                      size(radii[0]), size(radii[1]), size(radii[2]), size(radii[3]))
                else:
                    clip = gsk_path(geometry.outline(item), lambda x, y: geometry.to_world(item, x, y))
                    if clip is not None:
                        snapshot.push_fill(clip, Gsk.FillRule.WINDING)
                    else:
                        clipped = False
                if clip_bounds is not None or clip_rounded is not None:
                    snapshot = Gtk.Snapshot()
            seed = None
            if item.id in getattr(self, "_backdrop_keys", set()) and clipped and opaque_fill(item):
                body = Gtk.Snapshot()
                body.save()
                self._apply_transform(body, item, self._grid)
                fill_node = self.content_node(item, "body")
                if fill_node is not None:
                    body.append_node(fill_node)
                body.restore()
                seed = body.to_node()
            self._compose_children(snapshot, tree, children, visible, outline_mode, zoom, seen, seed)
            if snapshot is not outer:
                append_clipped(outer, snapshot, clip_bounds, clip_rounded)
                snapshot = outer
            elif clipped:
                snapshot.pop()
            if stroke_on_top:
                node = self.content_node(item, "stroke")
                if node is not None:
                    snapshot.save()
                    self._apply_transform(snapshot, item, self._grid)
                    snapshot.append_node(node)
                    snapshot.restore()
        if group_effects:
            self._append_group_effects(outer_group, snapshot, group_effects)
            snapshot = outer_group
        if opacity < 1.0:
            snapshot.pop()

    @staticmethod
    def _apply_transform(snapshot: Gtk.Snapshot, item: Any, grid: float | None = None) -> None:
        width, height = item.width, item.height
        box = snap_box(item, grid)
        if box is not None:
            left, top, right, bottom = box
            snapshot.translate(point((left + right) / 2, (top + bottom) / 2))
            if getattr(item, "flip_h", False) or getattr(item, "flip_v", False):
                snapshot.scale(-1.0 if item.flip_h else 1.0, -1.0 if item.flip_v else 1.0)
            if item.kind != "text":
                scale_x = (right - left) / width if width > 1e-9 else 1.0
                scale_y = (bottom - top) / height if height > 1e-9 else 1.0
                snapshot.scale(scale_x, scale_y)
            snapshot.translate(point(-width / 2, -height / 2))
            return
        snapshot.translate(point(item.x + width / 2, item.y + height / 2))
        if item.rotation:
            snapshot.rotate(float(item.rotation))
        skew = geometry.skew_of(item)
        if skew:
            snapshot.transform(Gsk.Transform.new().skew(math.degrees(math.atan(skew)), 0.0))
        if getattr(item, "flip_h", False) or getattr(item, "flip_v", False):
            snapshot.scale(-1.0 if item.flip_h else 1.0, -1.0 if item.flip_v else 1.0)
        snapshot.translate(point(-width / 2, -height / 2))

    def _outline_node(self, item: Any, zoom: float) -> Gsk.RenderNode | None:
        snapshot = Gtk.Snapshot()
        path = gsk_path(geometry.outline(item))
        if path is None:
            return None
        stroke = Gsk.Stroke.new(1.0 / max(zoom, 1e-6))
        ink = self.widget.get_color() if self.widget is not None else Gdk.RGBA(red=0, green=0, blue=0, alpha=1)
        snapshot.append_stroke(path, stroke, ink)
        return snapshot.to_node()

    # ── one object ───────────────────────────────────────────────────────

    def content_node(self, item: Any, part: str = "all") -> Gsk.RenderNode | None:
        """One object's drawing: "all", its "body" without the stroke, or the "stroke"."""
        key = self.fingerprint(item) + part
        cache_id = item.id if part == "all" else f"{item.id}#{part}"
        cached = self._content.get(cache_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            node = self._build(item, part)
        except (GLib.Error, ValueError, TypeError) as error:  # never lose the frame
            print(f"Canvas could not draw {item.name}: {error}")
            node = None
        self._content[cache_id] = (key, node)
        return node

    def _build(self, item: Any, part: str = "all") -> Gsk.RenderNode | None:
        snapshot = Gtk.Snapshot()
        shapes = geometry.outline(item)
        path = gsk_path(shapes)
        closed = bool(shapes) and all(shape.closed for shape in shapes)
        if part == "stroke":
            if path is not None:
                self._stroke(snapshot, item, path, shapes, closed)
            return snapshot.to_node()
        effects = [value for value in (normalized_effect(effect) for effect in item.effects
                                       if isinstance(effect, dict)) if value]
        layer_blurs = [effect for effect in effects if effect["kind"] == "layer_blur"]
        blur = max((effect["blur"] for effect in layer_blurs), default=0.0)
        ramp = next((effect for effect in layer_blurs if effect["progressive"]), None)
        outer_blur = None
        if ramp is not None:
            outer_blur, snapshot = snapshot, Gtk.Snapshot()
        elif blur > 0:
            snapshot.push_blur(blur)

        # A rounded rect stands in for the shape only while its corners are
        # plain circles; a squircle's shadow follows its own path.
        rounded = (item.kind not in {"ellipse", "polygon", "star", "path", "line", "text"} and closed
                   and geometry.corner_smoothing_of(item) <= 1e-4)
        drops = [effect for effect in effects if effect["kind"] == "drop_shadow"]
        # An outset shadow is exact (it carries spread), and nothing shows
        # through the layer anyway when the layer is opaque or asks for no
        # shadow behind it.
        outset = [effect for effect in drops
                  if rounded and closed and (not effect["behind"] or opaque_fill(item))]
        rest = [effect for effect in drops if effect not in outset]
        for effect in outset:
            snapshot.append_outset_shadow(self._rounded(item), rgba(effect["color"]),
                                          effect["x"], effect["y"], effect["spread"],
                                          effect["blur"])
        if path is not None and closed:
            # A shadow with "show behind" off, on a shape GSK has no outset
            # shadow for: cast it and cut the layer's own shape out of it.
            knockout = [effect for effect in rest if not effect["behind"]]
            if knockout:
                rest = [effect for effect in rest if effect["behind"]]
                self._knockout_shadows(snapshot, item, path, knockout)
        shape_shadows = rest if closed or not rounded else []
        if shape_shadows:
            shadows = []
            for effect in shape_shadows:
                shadow = Gsk.Shadow()
                shadow.color = rgba(effect["color"])
                shadow.dx, shadow.dy, shadow.radius = effect["x"], effect["y"], effect["blur"]
                shadows.append(shadow)
            snapshot.push_shadow(shadows)

        if item.kind == "image":
            self._draw_image(snapshot, item)
        elif path is not None and closed and item.kind != "text":
            self._fill(snapshot, item, path)
            for effect in effects:
                if effect["kind"] == "inner_shadow":
                    self._inner_shadow(snapshot, item, path, effect, rounded)
        if item.kind == "text":
            self._draw_text(snapshot, item)
            if part != "body":
                self._stroke_text(snapshot, item)
        elif path is not None and part != "body":
            self._stroke(snapshot, item, path, shapes, closed)

        if shape_shadows:
            snapshot.pop()
        if ramp is not None:
            inner = snapshot.to_node()
            if inner is not None:
                outer_blur.append_node(progressive_blur_node(inner, ramp, item))
            snapshot = outer_blur
        elif blur > 0:
            snapshot.pop()
        return snapshot.to_node()

    def _rounded(self, item: Any) -> Gsk.RoundedRect:
        tl, tr, br, bl = geometry.corner_radii_of(item)
        value = Gsk.RoundedRect()
        size = lambda radius: Graphene.Size().init(radius, radius)  # noqa: E731
        value.init(rect(0, 0, item.width, item.height), size(tl), size(tr), size(br), size(bl))
        return value

    def _paint_into(self, snapshot: Gtk.Snapshot, item: Any, paint_gradient: dict[str, Any] | None,
                    solid: str | None, bounds: tuple[float, float, float, float]) -> bool:
        """Append the paint over `bounds` (inside a clip the caller pushed)."""
        if paint_gradient and paint_gradient.get("visible", True) is not False:
            opacity = float(paint_gradient.get("opacity", 1.0))
            handles = paint_gradient.get("handles") or []
            width, height = max(item.width, 1e-6), max(item.height, 1e-6)
            x, y, w, h = bounds
            area = rect(x, y, max(w, 1e-3), max(h, 1e-3))
            if paint_gradient.get("type") == "radial":
                cx, cy = (handles[0] if handles else (0.5, 0.5))
                ex, ey = (handles[1] if len(handles) > 1 else (0.5, 1.0))
                wx, wy = (handles[2] if len(handles) > 2 else (1.0, 0.5))
                # The unit circle mapped onto the handles: the first axis to
                # handle 2, the second to handle 1. Rotated and skewed
                # ellipses (Figma's gradient transforms) draw exactly.
                matrix = ((wx - cx) * width, (wy - cy) * height, (ex - cx) * width, (ey - cy) * height,
                          cx * width, cy * height)
                inverse = invert_affine(*matrix)
                if inverse is None:
                    return True
                snapshot.save()
                snapshot.transform(affine_transform(*matrix))
                snapshot.append_radial_gradient(mapped_bounds(inverse, x, y, x + max(w, 1e-3), y + max(h, 1e-3)),
                                                point(0, 0), 1.0, 1.0, 0.0, 1.0,
                                                color_stops(paint_gradient, opacity))
                snapshot.restore()
            else:
                sx, sy = (handles[0] if handles else (0.5, 0.0))
                ex, ey = (handles[1] if len(handles) > 1 else (0.5, 1.0))
                start = point(sx * width, sy * height)
                end = point(ex * width, ey * height)
                if abs(start.x - end.x) < 1e-6 and abs(start.y - end.y) < 1e-6:
                    end = point(end.x, end.y + 1e-3)
                snapshot.append_linear_gradient(area, start, end,
                                                color_stops(paint_gradient, opacity))
            return True
        color = color_of(solid)
        if color is None:
            return False
        x, y, w, h = bounds
        snapshot.append_color(rgba(color), rect(x, y, max(w, 1e-3), max(h, 1e-3)))
        return True

    def _fill(self, snapshot: Gtk.Snapshot, item: Any, path: Gsk.Path) -> None:
        """Every visible fill, bottom to top, each with its own opacity and blend."""
        paints = visible_paints(item, "fill")
        if not paints:
            return
        rule = fill_rule_of(item)
        if len(paints) == 1 and paints[0].get("type") == "solid" and paints[0].get("blend", "normal") == "normal":
            color = color_of(paints[0].get("color"))
            if color is None:
                return
            opacity = float(paints[0].get("opacity", 1.0))
            if item.kind in BOX_KINDS and not any(geometry.corner_radii_of(item)):
                # A plain box is a color node: GPU renderers antialias path
                # fills even on whole pixels, which left seams where boxes meet.
                snapshot.append_color(rgba(color, opacity), rect(0, 0, max(item.width, 0.0), max(item.height, 0.0)))
            else:
                snapshot.append_fill(path, rule, rgba(color, opacity))
            return
        snapshot.push_fill(path, rule)
        self._append_paints(snapshot, item, paints, self._paint_bounds(item, path))
        snapshot.pop()

    def _append_paints(self, snapshot: Gtk.Snapshot, item: Any, paints: list[dict[str, Any]],
                       bounds: tuple[float, float, float, float]) -> None:
        """A stack of paints over `bounds` (inside a clip the caller pushed).

        A paint with a blend mode blends with the paints below it in the stack.
        """
        below: Gsk.RenderNode | None = None
        for paint in paints:
            layer = Gtk.Snapshot()
            self._paint_layer(layer, item, paint, bounds)
            node = layer.to_node()
            if node is None:
                continue
            below = compose_blend(below, node, paint.get("blend", "normal"))
        if below is not None:
            snapshot.append_node(below)

    @staticmethod
    def _paint_bounds(item: Any, path: Gsk.Path | None) -> tuple[float, float, float, float]:
        """The area a paint must cover: the box, grown to the path."""
        left, top, right, bottom = 0.0, 0.0, max(item.width, 0.0), max(item.height, 0.0)
        if path is not None:
            found, bounds = path.get_bounds()
            if found:
                left = min(left, bounds.get_x())
                top = min(top, bounds.get_y())
                right = max(right, bounds.get_x() + bounds.get_width())
                bottom = max(bottom, bounds.get_y() + bounds.get_height())
        return left, top, right - left, bottom - top

    def _paint_layer(self, snapshot: Gtk.Snapshot, item: Any, paint: dict[str, Any],
                     bounds: tuple[float, float, float, float]) -> None:
        """One paint of a stack, over `bounds`, inside a clip the caller pushed."""
        kind = paint.get("type", "solid")
        opacity = max(0.0, min(1.0, float(paint.get("opacity", 1.0) or 0.0)))
        x, y, w, h = bounds
        if kind == "solid":
            color = color_of(paint.get("color"))
            if color is not None:
                snapshot.append_color(rgba(color, opacity), rect(x, y, max(w, 1e-3), max(h, 1e-3)))
            return
        if kind in {"linear", "radial"}:
            self._paint_into(snapshot, item, paint, None, bounds)
            return
        width, height = float(item.width), float(item.height)
        if width <= 1e-6 or height <= 1e-6:
            return
        if kind in {"angular", "diamond"}:
            values = paint.get("transform") or [1, 0, 0, 1, 0, 0]
            if len(values) != 6:
                return
            # Gradient space (Figma: center 0.5, 0.5) to layer pixels.
            to_unit = invert_affine(*[float(value) for value in values])
            if to_unit is None:
                return
            a, b, c, d, e, f = to_unit
            matrix = (a * width, b * height, c * width, d * height, e * width, f * height)
            back = invert_affine(*matrix)
            if back is None:
                return
            area = mapped_bounds(back, x, y, x + w, y + h)
            stops = color_stops(paint, opacity)
            snapshot.save()
            snapshot.transform(affine_transform(*matrix))
            if kind == "angular":
                snapshot.append_conic_gradient(area, point(0.5, 0.5), 90.0, stops)
            else:
                gx, gy = area.get_x(), area.get_y()
                gr, gb = gx + area.get_width(), gy + area.get_height()
                for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
                    left, right = (0.5, max(gr, 0.5 + 1e-3)) if sx > 0 else (min(gx, 0.5 - 1e-3), 0.5)
                    top, bottom = (0.5, max(gb, 0.5 + 1e-3)) if sy > 0 else (min(gy, 0.5 - 1e-3), 0.5)
                    quadrant = rect(left, top, right - left, bottom - top)
                    ramp = Gtk.Snapshot()
                    ramp.append_linear_gradient(quadrant, point(0.5, 0.5),
                                                point(0.5 + sx / 4, 0.5 + sy / 4), stops)
                    append_clipped(snapshot, ramp, quadrant)
            snapshot.restore()
            return
        if kind == "image":
            self._image_paint(snapshot, item, paint, bounds, opacity)

    def _image_paint(self, snapshot: Gtk.Snapshot, item: Any, paint: dict[str, Any],
                     bounds: tuple[float, float, float, float], opacity: float) -> None:
        texture = self._texture_for(paint.get("asset_id"))
        if texture is None:
            # A missing image shows a checkerboard, as Figma does, so the
            # hole in the design is visible rather than silently blank.
            x, y, w, h = bounds
            cell = 12.0
            snapshot.push_repeat(rect(x, y, max(w, 1e-3), max(h, 1e-3)), rect(0, 0, cell * 2, cell * 2))
            snapshot.append_color(rgba(Color(1, 1, 1, 1), opacity), rect(0, 0, cell * 2, cell * 2))
            snapshot.append_color(rgba(Color(0.85, 0.85, 0.85, 1), opacity), rect(0, 0, cell, cell))
            snapshot.append_color(rgba(Color(0.85, 0.85, 0.85, 1), opacity), rect(cell, cell, cell, cell))
            snapshot.pop()
            return
        image_width, image_height = float(texture.get_width()), float(texture.get_height())
        if image_width <= 0 or image_height <= 0:
            return
        width, height = float(item.width), float(item.height)
        mode = paint.get("scale_mode", "fill")
        if opacity < 1.0:
            snapshot.push_opacity(opacity)
        snapshot.save()
        if mode == "crop":
            values = paint.get("transform") or [1, 0, 0, 1, 0, 0]
            to_unit = invert_affine(*[float(value) for value in values]) if len(values) == 6 else None
            if to_unit is not None:
                a, b, c, d, e, f = to_unit
                # Image pixels -> image unit square -> layer unit box -> layer pixels.
                snapshot.transform(affine_transform(a * width / image_width, b * height / image_width,
                                                    c * width / image_height, d * height / image_height,
                                                    e * width, f * height))
                self._append_texture(snapshot, texture, rect(0, 0, image_width, image_height))
        elif mode == "tile":
            scale = float(paint.get("scale", 1.0) or 1.0)
            tile = rect(0, 0, max(image_width * scale, 1e-3), max(image_height * scale, 1e-3))
            x, y, w, h = bounds
            snapshot.push_repeat(rect(x, y, max(w, 1e-3), max(h, 1e-3)), tile)
            self._append_texture(snapshot, texture, tile)
            snapshot.pop()
        else:
            quarter_turns = int(round(float(paint.get("rotation", 0.0) or 0.0) / 90.0)) % 4
            turned_w, turned_h = (image_height, image_width) if quarter_turns % 2 else (image_width, image_height)
            ratio = (max(width / turned_w, height / turned_h) if mode != "fit"
                     else min(width / turned_w, height / turned_h))
            snapshot.translate(point(width / 2, height / 2))
            if quarter_turns:
                snapshot.rotate(90.0 * quarter_turns)
            drawn_w, drawn_h = image_width * ratio, image_height * ratio
            self._append_texture(snapshot, texture, rect(-drawn_w / 2, -drawn_h / 2, drawn_w, drawn_h))
        snapshot.restore()
        if opacity < 1.0:
            snapshot.pop()

    @staticmethod
    def _append_texture(snapshot: Gtk.Snapshot, texture: Gdk.Texture, area: Graphene.Rect) -> None:
        if hasattr(snapshot, "append_scaled_texture"):
            snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.TRILINEAR, area)
        else:
            snapshot.append_texture(texture, area)

    def _stroke_spec(self, item: Any, width: float) -> Gsk.Stroke:
        stroke = Gsk.Stroke.new(max(width, 0.0))
        stroke.set_line_cap(CAPS.get(item.stroke_cap, Gsk.LineCap.BUTT))
        stroke.set_line_join(JOINS.get(item.stroke_join, Gsk.LineJoin.MITER))
        dash = [float(value) for value in (item.stroke_dash or []) if float(value) >= 0]
        if dash and sum(dash) > 0:
            stroke.set_dash(dash)
        return stroke

    def _stroke(self, snapshot: Gtk.Snapshot, item: Any, path: Gsk.Path,
                shapes: list[geometry.Subpath], closed: bool) -> None:
        width = float(item.stroke_width or 0.0)
        paints = visible_paints(item, "stroke")
        if not paints or width <= 0:
            return
        align = item.stroke_align if closed else "center"
        sides = (item.metadata or {}).get("stroke_sides")
        if isinstance(sides, dict) and closed and item.kind not in {"ellipse", "polygon", "star", "path", "line"}:
            self._stroke_sides(snapshot, item, path, sides, align, paints)
            return
        rule = fill_rule_of(item)
        drawn_width = width * 2 if align in {"inside", "outside"} else width
        stroke = self._stroke_spec(item, drawn_width)
        if align == "inside":
            snapshot.push_fill(path, rule)
        elif align == "outside":
            snapshot.push_mask(Gsk.MaskMode.INVERTED_ALPHA)
            snapshot.append_fill(path, rule, Gdk.RGBA(red=0, green=0, blue=0, alpha=1))
            snapshot.pop()
        if len(paints) == 1 and paints[0].get("type") == "solid":
            color = color_of(paints[0].get("color"))
            if color is not None:
                snapshot.append_stroke(path, stroke, rgba(color, float(paints[0].get("opacity", 1.0))))
        else:
            snapshot.push_stroke(path, stroke)
            pad = drawn_width + 2
            self._append_paints(snapshot, item, paints, (-pad, -pad, item.width + pad * 2, item.height + pad * 2))
            snapshot.pop()
        if align in {"inside", "outside"}:
            snapshot.pop()
        if not closed:
            top = paints[-1]
            self._line_ends(snapshot, item, shapes, width,
                            (paint_color(top) or Color(0, 0, 0, 1)).with_alpha(
                                (paint_color(top) or Color(0, 0, 0, 1)).a * float(top.get("opacity", 1.0))))

    def _stroke_sides(self, snapshot: Gtk.Snapshot, item: Any, path: Gsk.Path, sides: dict[str, Any],
                      align: str, paints: list[dict[str, Any]]) -> None:
        """A box stroke with its own weight per side, as bands along the edges."""
        def weight(name: str) -> float:
            try:
                return max(0.0, float(sides.get(name, 0.0) or 0.0))
            except (TypeError, ValueError):
                return 0.0
        top, right, bottom, left = weight("top"), weight("right"), weight("bottom"), weight("left")
        outward = {"inside": 0.0, "center": 0.5, "outside": 1.0}.get(align, 0.0)
        width, height = float(item.width), float(item.height)
        x0, x1 = -left * outward, width + right * outward
        y0, y1 = -top * outward, height + bottom * outward
        builder = Gsk.PathBuilder.new()
        bands = ((x0, y0, x1 - x0, top), (x0, y1 - bottom, x1 - x0, bottom),
                 (x0, y0, left, y1 - y0), (x1 - right, y0, right, y1 - y0))
        empty = True
        for bx, by, bw, bh in bands:
            if bw > 0 and bh > 0:
                builder.add_rect(rect(bx, by, bw, bh))
                empty = False
        if empty:
            return
        bands_path = builder.to_path()
        clipped = align == "inside" and any(geometry.corner_radii_of(item))
        if clipped:
            snapshot.push_fill(path, Gsk.FillRule.WINDING)
        snapshot.push_fill(bands_path, Gsk.FillRule.WINDING)
        self._append_paints(snapshot, item, paints, (x0, y0, x1 - x0, y1 - y0))
        snapshot.pop()
        if clipped:
            snapshot.pop()

    def _line_ends(self, snapshot: Gtk.Snapshot, item: Any, shapes: list[geometry.Subpath],
                   width: float, color: Color | None = None) -> None:
        color = color or color_of(item.stroke)
        if color is None:
            return
        for kind, value in geometry.line_end_shapes(item, shapes, width):
            builder = Gsk.PathBuilder.new()
            if kind == "circle":
                (cx, cy), radius = value
                builder.add_circle(point(cx, cy), radius)
            else:
                builder.move_to(*value[0])
                for vertex in value[1:]:
                    builder.line_to(*vertex)
                if kind == "fill":
                    builder.close()
            if kind == "stroke":
                stroke = self._stroke_spec(item, width)
                stroke.set_dash(None)
                stroke.set_line_join(Gsk.LineJoin.ROUND)
                stroke.set_line_cap(Gsk.LineCap.ROUND)
                snapshot.append_stroke(builder.to_path(), stroke, rgba(color))
            else:
                snapshot.append_fill(builder.to_path(), Gsk.FillRule.WINDING, rgba(color))

    def _knockout_shadows(self, snapshot: Gtk.Snapshot, item: Any, path: Gsk.Path,
                          effects: list[dict[str, Any]]) -> None:
        """Drop shadows the layer's own shape is cut out of (Figma's "Show
        behind transparent areas", switched off)."""
        shadows = []
        for effect in effects:
            shadow = Gsk.Shadow()
            shadow.color = rgba(effect["color"])
            shadow.dx, shadow.dy, shadow.radius = effect["x"], effect["y"], effect["blur"]
            shadows.append(shadow)
        rule = fill_rule_of(item)
        ink = Gdk.RGBA(red=0, green=0, blue=0, alpha=1)
        snapshot.push_mask(Gsk.MaskMode.INVERTED_ALPHA)
        snapshot.append_fill(path, rule, ink)
        snapshot.pop()
        snapshot.push_shadow(shadows)
        # The silhouette casts the shadows; the mask removes it again.
        snapshot.append_fill(path, rule, ink)
        snapshot.pop()
        snapshot.pop()

    def _inner_shadow(self, snapshot: Gtk.Snapshot, item: Any, path: Gsk.Path,
                      effect: dict[str, Any], rounded: bool) -> None:
        if rounded:
            snapshot.append_inset_shadow(self._rounded(item), rgba(effect["color"]),
                                         effect["x"], effect["y"], effect["spread"], effect["blur"])
            return
        # Any other shape: shadow cast inward by a ring around the shape.
        margin = effect["blur"] * 3 + abs(effect["x"]) + abs(effect["y"]) + 10
        builder = Gsk.PathBuilder.new()
        builder.add_rect(rect(-margin, -margin, item.width + margin * 2, item.height + margin * 2))
        builder.add_path(path)
        ring = builder.to_path()
        snapshot.push_fill(path, Gsk.FillRule.WINDING)
        shadow = Gsk.Shadow()
        shadow.color = rgba(effect["color"])
        shadow.dx, shadow.dy, shadow.radius = effect["x"], effect["y"], effect["blur"]
        snapshot.push_shadow([shadow])
        snapshot.append_fill(ring, Gsk.FillRule.EVEN_ODD, Gdk.RGBA(red=0, green=0, blue=0, alpha=1))
        snapshot.pop()
        # The ring lies outside the shape, so the clip leaves only its shadow.
        snapshot.pop()

    def _pango_context(self) -> Pango.Context:
        if self._pango is None:
            if self.widget is not None:
                self._pango = self.widget.create_pango_context()
            else:
                gi.require_version("PangoCairo", "1.0")
                from gi.repository import PangoCairo
                self._pango = PangoCairo.FontMap.get_default().create_context()
            self._pango.set_round_glyph_positions(False)
        return self._pango

    def text_layout(self, item: Any, text: str | None = None):
        """The laid-out text of a text layer (shared with on-canvas editing)."""
        return build_layout(self._pango_context(), item, text)

    def _text_glyph_path(self, item: Any) -> Gsk.Path | None:
        """The outlines of a text layer's glyphs, in the layer's own box."""
        import cairo
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        text = self.text_layout(item)
        context = cairo.Context(cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None))
        context.translate(0, text.offset_y)
        PangoCairo.layout_path(context, text.layout)
        return cairo_path_to_gsk(context)

    def _stroke_text(self, snapshot: Gtk.Snapshot, item: Any) -> None:
        """A text layer's stroke, drawn on its glyph outlines as Figma does."""
        if float(item.stroke_width or 0.0) <= 0 or not visible_paints(item, "stroke"):
            return
        path = self._text_glyph_path(item)
        if path is not None:
            self._stroke(snapshot, item, path, [], True)

    @staticmethod
    def _runs_paint(item: Any) -> bool:
        """Whether the text's own runs carry colors (then the layer's stack cannot)."""
        return any(isinstance(run, dict) and ("fill" in run or "fill_gradient" in run)
                   for run in (item.metadata or {}).get("ranges") or [])

    def _draw_text(self, snapshot: Gtk.Snapshot, item: Any) -> None:
        paints = visible_paints(item, "fill")
        text = self.text_layout(item)
        clip = type_settings(item.metadata)["sizing"] == "fixed"
        outer = snapshot
        if clip:
            snapshot = Gtk.Snapshot()
        snapshot.save()
        snapshot.translate(point(0, text.offset_y))
        if (len(paints) > 1 or paints and paints[0].get("type") != "solid") and self._runs_paint(item):
            # Runs with their own color paint themselves (Pango attributes), so
            # only the layer's bottom paint can apply to the rest.
            paints = paints[:1]
        if len(paints) == 1 and paints[0].get("type") == "solid" and paints[0].get("blend", "normal") == "normal":
            color = color_of(paints[0].get("color"), Color(0, 0, 0, 1))
            snapshot.append_layout(text.layout, rgba(color, float(paints[0].get("opacity", 1.0))))
        elif paints:
            bounds = (0.0, -text.offset_y, max(item.width, text.width, 1), max(item.height, text.height, 1))
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
            snapshot.append_layout(text.layout, rgba(Color(0, 0, 0, 1)))
            snapshot.pop()
            self._append_paints(snapshot, item, paints, bounds)
            snapshot.pop()
        snapshot.restore()
        if clip:
            append_clipped(outer, snapshot, rect(0, 0, max(item.width, 1), max(item.height, 1)))

    def _draw_image(self, snapshot: Gtk.Snapshot, item: Any) -> None:
        width, height = max(item.width, 1.0), max(item.height, 1.0)
        texture = self._texture(item)
        if texture is not None:
            snapshot.append_texture(texture, rect(0, 0, width, height))
            return
        if item.metadata.get("preview") == "meadow":
            self._meadow(snapshot, width, height)
            return
        snapshot.append_color(rgba(Color(0.5, 0.55, 0.6, 0.25)), rect(0, 0, width, height))

    def _texture(self, item: Any) -> Gdk.Texture | None:
        return self._texture_for(getattr(item, "asset_id", None))

    def _texture_for(self, asset_id: str | None) -> Gdk.Texture | None:
        if not asset_id:
            return None
        if asset_id in self._textures:
            return self._textures[asset_id]
        texture = None
        document = getattr(self, "_document", None)
        if document is not None:
            asset = next((value for value in document.assets if value.id == asset_id), None)
            data = None
            if asset is not None:
                # Packages store embedded bytes by asset id, pasted images by
                # embedded name; read either.
                data = asset_bytes(document.embedded_assets, asset)
            if data:
                try:
                    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                except GLib.Error:
                    texture = None
        self._textures[asset_id] = texture
        return texture

    @staticmethod
    def _meadow(snapshot: Gtk.Snapshot, width: float, height: float) -> None:
        """The bundled demo's abstract meadow, drawn as vectors so it stays sharp."""
        area = rect(0, 0, width, height)
        outer, snapshot = snapshot, Gtk.Snapshot()

        def stops(*values):
            result = []
            for offset, (r, g, b, a) in values:
                stop = Gsk.ColorStop()
                stop.offset = offset
                stop.color = rgba(Color(r, g, b, a))
                result.append(stop)
            return result

        snapshot.append_linear_gradient(area, point(0, 0), point(width, height), stops(
            (0.0, (0.76, 0.83, 0.40, 1)), (0.48, (0.08, 0.60, 0.48, 1)), (1.0, (0.00, 0.27, 0.24, 1))))
        snapshot.append_radial_gradient(area, point(width * .06, height * .04), width * .72, width * .72,
                                        0.0, 1.0, stops((0.0, (0.94, 0.94, 0.42, .86)),
                                                        (1.0, (0.18, 0.62, 0.39, 0))))
        ribbons = (
            (((-.05, .96), (.22, .56), (.14, .30), (-.04, .26), (-.08, .36), (.08, .43), (.18, .71), (-.05, 1.08)),
             ((.02, .27, .16, 1), (.42, .72, .10, 1))),
            (((.54, 1.08), (.64, .62), (.67, .33), (1.06, .20), (1.08, .30), (.84, .44), (.78, .78), (.69, 1.09)),
             ((.10, .37, .24, 1), (.69, .86, .20, 1))),
            (((.80, 1.08), (.86, .74), (.94, .63), (1.08, .58), (1.10, .76), (.96, .77), (.94, .95), (.92, 1.08)),
             ((.01, .18, .14, 1), (.06, .50, .32, 1))),
        )
        for points, colors in ribbons:
            p = [(x * width, y * height) for x, y in points]
            builder = Gsk.PathBuilder.new()
            builder.move_to(*p[0])
            builder.cubic_to(*p[1], *p[2], *p[3])
            builder.line_to(*p[4])
            builder.cubic_to(*p[5], *p[6], *p[7])
            builder.close()
            ribbon = builder.to_path()
            snapshot.push_fill(ribbon, Gsk.FillRule.WINDING)
            snapshot.append_linear_gradient(area, point(0, 0), point(width, height),
                                            stops((0.0, colors[0]), (1.0, colors[1])))
            snapshot.pop()
            snapshot.append_stroke(ribbon, Gsk.Stroke.new(1.0), rgba(Color(0.84, 0.94, 0.32, .72)))
        append_clipped(outer, snapshot, area)
