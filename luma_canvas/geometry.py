# SPDX-License-Identifier: MPL-2.0
"""Shape outlines, hit testing, selection rules and transforms.

One outline per object feeds both the renderer and hit testing, so what is
clicked is exactly what is drawn: a rotated bar is only hit on the bar, an
ellipse only inside the ellipse, a curve along the curve.

Outlines are in the object's local box (0..width, 0..height, before rotation)
as subpaths of line and cubic segments. `to_world` and `to_local` apply the
object's rotation about its center and its flips.

Selection follows Figma (spec 5.1 and 5.2): top-level frames are transparent
to a click that hits a child, a click selects the outermost layer the person
has not entered, Ctrl selects the deepest layer, and a marquee touches by
default, encloses with Alt and goes deep with Ctrl.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

KAPPA = 0.5522847498
NON_SCENE_KINDS = {"guide", "comment"}
FRAME_LIKE = {"frame", "page", "component", "section"}
CONTAINERS = {"page", "spread", "frame", "section", "group", "component", "instance",
              "layout", "mask", "boolean"}
TRANSPARENT_CONTAINERS = {"group", "spread", "layout", "mask"}
Point = tuple[float, float]


# ── outlines ─────────────────────────────────────────────────────────────

@dataclass
class Subpath:
    start: Point
    segments: list[tuple] = field(default_factory=list)   # ("L", x, y) | ("C", x1, y1, x2, y2, x, y)
    closed: bool = False

    def line(self, x: float, y: float) -> "Subpath":
        self.segments.append(("L", x, y))
        return self

    def cubic(self, x1: float, y1: float, x2: float, y2: float, x: float, y: float) -> "Subpath":
        self.segments.append(("C", x1, y1, x2, y2, x, y))
        return self


def corner_radii_of(item: Any) -> tuple[float, float, float, float]:
    radii = getattr(item, "corner_radii", None)
    if radii and len(radii) == 4:
        values = [float(value) for value in radii]
    else:
        values = [float(getattr(item, "radius", 0.0) or 0.0)] * 4
    limit = max(0.0, min(item.width, item.height) / 2)
    return tuple(max(0.0, min(value, limit)) for value in values)  # type: ignore[return-value]


def corner_smoothing_of(item: Any) -> float:
    """How much a box's corners are smoothed (Figma's corner smoothing, 0…1)."""
    value = (getattr(item, "metadata", None) or {}).get("corner_smoothing")
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _squircle_corner(path: Subpath, vertex: Point, ex: Point, ey: Point, radius: float,
                     smoothing: float) -> None:
    """One smoothed corner (Figma's squircle), added to `path`.

    The corner is built in its own frame: its point at `vertex`, the edge it
    arrives along in direction `ex`, the edge it leaves along in `ey`. It is a
    cubic easing away from the edge, a circular arc of 90° × (1 − smoothing),
    and the mirrored cubic back onto the next edge, which is how Figma draws
    it. With no smoothing it is exactly a quarter circle.
    """
    arc_measure = 90.0 * (1.0 - smoothing)
    length = math.sin(math.radians(arc_measure / 2)) * radius * math.sqrt(2)
    alpha = (90.0 - arc_measure) / 2
    beta = 45.0 * smoothing
    c = radius * math.tan(math.radians(alpha / 2)) * math.cos(math.radians(beta))
    d = c * math.tan(math.radians(beta))
    b = ((1.0 + smoothing) * radius - length - c - d) / 3
    a = 2 * b
    reach = (1.0 + smoothing) * radius

    def at(u: float, v: float) -> Point:
        return (vertex[0] + ex[0] * u + ey[0] * v, vertex[1] + ex[1] * u + ey[1] * v)

    path.line(*at(-reach, 0.0))
    third = (-reach + a + b + c, d)
    path.cubic(*at(-reach + a, 0.0), *at(-reach + a + b, 0.0), *at(*third))
    fourth = (third[0] + length, third[1] + length)
    _arc_cubics(path, at(-radius, radius), at(*third), at(*fourth), radius)
    path.cubic(*at(fourth[0] + d, fourth[1] + c), *at(fourth[0] + d, fourth[1] + b + c),
               *at(fourth[0] + d, fourth[1] + a + b + c))


def _arc_cubics(path: Subpath, center: Point, start: Point, end: Point, radius: float) -> None:
    """A circular arc from `start` to `end` around `center`, as cubics."""
    start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
    end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = end_angle - start_angle
    while sweep <= -math.pi:
        sweep += 2 * math.pi
    while sweep > math.pi:
        sweep -= 2 * math.pi
    if abs(sweep) < 1e-9:
        path.line(*end)
        return
    steps = max(1, math.ceil(abs(sweep) / (math.pi / 2)))
    step = sweep / steps
    kappa = 4.0 / 3.0 * math.tan(step / 4)
    angle = start_angle
    for _ in range(steps):
        next_angle = angle + step
        x0, y0 = center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)
        x1, y1 = center[0] + radius * math.cos(next_angle), center[1] + radius * math.sin(next_angle)
        path.cubic(x0 - kappa * radius * math.sin(angle), y0 + kappa * radius * math.cos(angle),
                   x1 + kappa * radius * math.sin(next_angle), y1 - kappa * radius * math.cos(next_angle),
                   x1, y1)
        angle = next_angle


def _smoothing_for(radius: float, smoothing: float, budget: float) -> float:
    """Smoothing a corner has room for: it reaches (1 + smoothing) × radius
    along each edge, and cannot reach past the middle of the box."""
    if radius <= 0:
        return 0.0
    return max(0.0, min(smoothing, budget / radius - 1.0))


def smoothed_rect(width: float, height: float, radii: Iterable[float], smoothing: float) -> Subpath:
    """A box with smoothed (squircle) corners, clockwise from the top left."""
    tl, tr, br, bl = radii
    budget = max(0.0, min(width, height) / 2)
    start = (1.0 + _smoothing_for(tl, smoothing, budget)) * tl
    path = Subpath((min(start, width), 0.0), closed=True)
    for vertex, ex, ey, radius in (((width, 0.0), (1.0, 0.0), (0.0, 1.0), tr),
                                   ((width, height), (0.0, 1.0), (-1.0, 0.0), br),
                                   ((0.0, height), (-1.0, 0.0), (0.0, -1.0), bl),
                                   ((0.0, 0.0), (0.0, -1.0), (1.0, 0.0), tl)):
        if radius <= 0:
            path.line(*vertex)
            continue
        _squircle_corner(path, vertex, ex, ey, radius, _smoothing_for(radius, smoothing, budget))
    return path


def rounded_rect(width: float, height: float, radii: Iterable[float]) -> Subpath:
    tl, tr, br, bl = radii
    path = Subpath((tl, 0.0), closed=True)
    path.line(width - tr, 0.0)
    if tr:
        path.cubic(width - tr + tr * KAPPA, 0, width, tr - tr * KAPPA, width, tr)
    path.line(width, height - br)
    if br:
        path.cubic(width, height - br + br * KAPPA, width - br + br * KAPPA, height, width - br, height)
    path.line(bl, height)
    if bl:
        path.cubic(bl - bl * KAPPA, height, 0, height - bl + bl * KAPPA, 0, height - bl)
    path.line(0.0, tl)
    if tl:
        path.cubic(0, tl - tl * KAPPA, tl - tl * KAPPA, 0, tl, 0)
    return path


def ellipse_outline(width: float, height: float) -> Subpath:
    rx, ry = width / 2, height / 2
    cx, cy = rx, ry
    ox, oy = rx * KAPPA, ry * KAPPA
    path = Subpath((cx + rx, cy), closed=True)
    path.cubic(cx + rx, cy + oy, cx + ox, cy + ry, cx, cy + ry)
    path.cubic(cx - ox, cy + ry, cx - rx, cy + oy, cx - rx, cy)
    path.cubic(cx - rx, cy - oy, cx - ox, cy - ry, cx, cy - ry)
    path.cubic(cx + ox, cy - ry, cx + rx, cy - oy, cx + rx, cy)
    return path


def polygon_vertices(width: float, height: float, count: int,
                     inner_ratio: float | None = None) -> list[Point]:
    """A regular polygon (or star) inscribed in the box, first vertex on top."""
    count = max(3, min(60, int(count or 3)))
    cx, cy, rx, ry = width / 2, height / 2, width / 2, height / 2
    points: list[Point] = []
    steps = count * 2 if inner_ratio is not None else count
    for index in range(steps):
        angle = -math.pi / 2 + index * math.tau / steps
        scale = inner_ratio if inner_ratio is not None and index % 2 else 1.0
        points.append((cx + rx * scale * math.cos(angle), cy + ry * scale * math.sin(angle)))
    return points


def is_closed_path(path: list[dict]) -> bool:
    return bool(path) and any(bool(path[-1].get(key)) for key in ("closed", "close"))


def nodes_outline(path: list[dict]) -> Subpath | None:
    """A Canvas path (nodes with optional absolute-in-box handles) as béziers."""
    if not path:
        return None

    def point(node: dict) -> Point:
        return float(node["x"]), float(node["y"])

    def segment(start: dict, end: dict) -> tuple:
        x0, y0 = point(start)
        x3, y3 = point(end)
        x1, y1 = float(start.get("out_x", x0)), float(start.get("out_y", y0))
        x2, y2 = float(end.get("in_x", x3)), float(end.get("in_y", y3))
        if (x1, y1) == (x0, y0) and (x2, y2) == (x3, y3):
            return ("L", x3, y3)
        return ("C", x1, y1, x2, y2, x3, y3)

    outline = Subpath(point(path[0]), closed=is_closed_path(path))
    for start, end in zip(path, path[1:]):
        outline.segments.append(segment(start, end))
    if outline.closed and len(path) > 1:
        closing = segment(path[-1], path[0])
        if closing[0] == "C" or point(path[-1]) != point(path[0]):
            outline.segments.append(closing)
    return outline


def nodes_outlines(path: list[dict]) -> list[Subpath]:
    """Every subpath of a path: a node with `move` starts a new one (imported
    vectors with holes and several parts); `closed` on a subpath's last node
    closes it."""
    if not any(node.get("move") for node in path[1:]):
        shape = nodes_outline(path)
        return [shape] if shape else []
    shapes: list[Subpath] = []
    start = 0
    for index in range(1, len(path) + 1):
        if index == len(path) or path[index].get("move"):
            shape = nodes_outline(path[start:index])
            if shape is not None and shape.segments:
                shapes.append(shape)
            start = index
    return shapes


def fill_rule(item: Any) -> str:
    """`nonzero` (the default) or `evenodd`, from `metadata["fill_rule"]`."""
    metadata = getattr(item, "metadata", None) or {}
    return "evenodd" if metadata.get("fill_rule") == "evenodd" else "nonzero"


def outline(item: Any) -> list[Subpath]:
    """The geometry of an object in its local box."""
    width, height = float(item.width), float(item.height)
    kind = item.kind
    path = getattr(item, "path", None) or []
    if kind in {"path", "line"} and path:
        return nodes_outlines(path)
    if kind == "boolean":
        # A live boolean's computed result; nothing while it has none.
        return nodes_outlines(path) if path else []
    if kind == "line":
        return [Subpath((0.0, 0.0)).line(width, height)]
    if kind == "ellipse":
        return [ellipse_outline(width, height)]
    if kind in {"polygon", "star"}:
        if path:
            return nodes_outlines(path)
        count = int(getattr(item, "point_count", 0) or (5 if kind == "star" else 3))
        ratio = float(getattr(item, "inner_ratio", 0.382)) if kind == "star" else None
        vertices = polygon_vertices(width, height, count, ratio)
        shape = Subpath(vertices[0], closed=True)
        for vertex in vertices[1:]:
            shape.line(*vertex)
        return [shape]
    radii = corner_radii_of(item)
    smoothing = corner_smoothing_of(item)
    if smoothing > 1e-4 and any(radii):
        return [smoothed_rect(width, height, radii, smoothing)]
    return [rounded_rect(width, height, radii)]


def is_open_kind(item: Any) -> bool:
    shapes = outline(item)
    return bool(shapes) and not all(shape.closed for shape in shapes)


def flatten_subpath(shape: Subpath, steps: int = 16) -> list[Point]:
    points = [shape.start]
    x0, y0 = shape.start
    for segment in shape.segments:
        if segment[0] == "L":
            points.append((segment[1], segment[2]))
            x0, y0 = segment[1], segment[2]
            continue
        _, x1, y1, x2, y2, x3, y3 = segment
        for step in range(1, steps + 1):
            t = step / steps
            u = 1 - t
            points.append((u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                           u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3))
        x0, y0 = x3, y3
    if shape.closed and points[0] != points[-1]:
        points.append(points[0])
    return points


def line_end_shapes(item: Any, shapes: list[Subpath], width: float) -> list[tuple[str, Any]]:
    """Decorated line ends in the layer's box: ("stroke", points), ("fill", points)
    or ("circle", (center, radius)). Round and square caps are the stroke's own."""
    if not shapes:
        return []
    points = flatten_subpath(shapes[0], 8)
    if len(points) < 2:
        return []
    result: list[tuple[str, Any]] = []
    for cap, tip, before in ((getattr(item, "start_cap", "none"), points[0], points[1]),
                             (getattr(item, "end_cap", "none"), points[-1], points[-2])):
        if cap in {None, "none", "round", "square"}:
            continue
        angle = math.atan2(tip[1] - before[1], tip[0] - before[0])
        size = max(6.0, width * 3.0)

        def back(distance: float, spread: float) -> Point:
            return (tip[0] - math.cos(angle) * distance - math.sin(angle) * spread,
                    tip[1] - math.sin(angle) * distance + math.cos(angle) * spread)
        if cap == "line_arrow":
            result.append(("stroke", [back(size, size * 0.6), tip, back(size, -size * 0.6)]))
        elif cap == "triangle_arrow":
            result.append(("fill", [tip, back(size * 1.2, size * 0.6), back(size * 1.2, -size * 0.6)]))
        elif cap == "reverse_triangle":
            result.append(("fill", [back(size * 1.2, 0), back(0, size * 0.6), back(0, -size * 0.6)]))
        elif cap == "circle":
            result.append(("circle", (tip, size * 0.45)))
        elif cap == "diamond":
            half = size * 0.5
            result.append(("fill", [(tip[0], tip[1] - half), (tip[0] + half, tip[1]),
                                    (tip[0], tip[1] + half), (tip[0] - half, tip[1])]))
    return result


def subpath_bounds(shapes: list[Subpath]) -> tuple[float, float, float, float] | None:
    points = [point for shape in shapes for point in flatten_subpath(shape, 24)]
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


# ── transforms ───────────────────────────────────────────────────────────

def center_of(item: Any) -> Point:
    return item.x + item.width / 2, item.y + item.height / 2


def skew_of(item: Any) -> float:
    """How far the box leans: x grows by this much of y, in the layer's own
    axes, before it is rotated (`metadata["skew"]`, as Figma's transform
    decomposes). Zero for an upright box."""
    value = (getattr(item, "metadata", None) or {}).get("skew")
    try:
        skew = float(value)
    except (TypeError, ValueError):
        return 0.0
    return skew if math.isfinite(skew) and abs(skew) < 1e4 else 0.0


def to_world(item: Any, lx: float, ly: float) -> Point:
    cx, cy = center_of(item)
    px, py = lx - item.width / 2, ly - item.height / 2
    if getattr(item, "flip_h", False):
        px = -px
    if getattr(item, "flip_v", False):
        py = -py
    skew = skew_of(item)
    if skew:
        px = px + skew * py
    angle = math.radians(getattr(item, "rotation", 0.0) or 0.0)
    cos, sin = math.cos(angle), math.sin(angle)
    return cx + px * cos - py * sin, cy + px * sin + py * cos


def to_local(item: Any, wx: float, wy: float) -> Point:
    cx, cy = center_of(item)
    angle = -math.radians(getattr(item, "rotation", 0.0) or 0.0)
    cos, sin = math.cos(angle), math.sin(angle)
    px, py = wx - cx, wy - cy
    lx, ly = px * cos - py * sin, px * sin + py * cos
    skew = skew_of(item)
    if skew:
        lx = lx - skew * ly
    if getattr(item, "flip_h", False):
        lx = -lx
    if getattr(item, "flip_v", False):
        ly = -ly
    return lx + item.width / 2, ly + item.height / 2


def world_corners(item: Any) -> list[Point]:
    return [to_world(item, 0, 0), to_world(item, item.width, 0),
            to_world(item, item.width, item.height), to_world(item, 0, item.height)]


def world_bounds(item: Any) -> tuple[float, float, float, float]:
    """Axis-aligned bounds of the rotated box: left, top, right, bottom."""
    if not getattr(item, "rotation", 0.0) and not skew_of(item):
        return item.x, item.y, item.x + item.width, item.y + item.height
    corners = world_corners(item)
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return min(xs), min(ys), max(xs), max(ys)


def union_bounds(items: Iterable[Any]) -> tuple[float, float, float, float] | None:
    boxes = [world_bounds(item) for item in items]
    if not boxes:
        return None
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


# ── containment ──────────────────────────────────────────────────────────

def _winding(point: Point, polygon: list[Point]) -> int:
    x, y = point
    winding = 0
    for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1]):
        if y0 <= y:
            if y1 > y and (x1 - x0) * (y - y0) - (x - x0) * (y1 - y0) > 0:
                winding += 1
        elif y1 <= y and (x1 - x0) * (y - y0) - (x - x0) * (y1 - y0) < 0:
            winding -= 1
    return winding


def _distance_to_segment(point: Point, a: Point, b: Point) -> float:
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length <= 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def contains(item: Any, wx: float, wy: float, *, tolerance: float = 4.0) -> bool:
    """Whether a world point hits the object's real shape.

    `tolerance` is in document units (callers pass screen px / zoom). Closed
    shapes hit inside even with a fully transparent fill; open paths, lines and
    strokes hit within `max(stroke/2, tolerance)` of the outline.
    """
    left, top, right, bottom = world_bounds(item)
    stroke = float(getattr(item, "stroke_width", 0.0) or 0.0) if getattr(item, "stroke", None) else 0.0
    reach = max(stroke / 2, tolerance)
    if wx < left - reach or wx > right + reach or wy < top - reach or wy > bottom + reach:
        return False
    lx, ly = to_local(item, wx, wy)
    kind = item.kind
    if kind in {"text", "image"} or kind in CONTAINERS and kind not in FRAME_LIKE and kind != "boolean":
        return 0 <= lx <= item.width and 0 <= ly <= item.height
    shapes = outline(item)
    for shape in shapes:
        points = flatten_subpath(shape)
        filled = shape.closed and (getattr(item, "fill", None) is not None
                                   or getattr(item, "fill_gradient", None) is not None
                                   or kind in FRAME_LIKE or kind in {"rectangle", "ellipse",
                                                                     "polygon", "star", "boolean"})
        if filled and len(points) > 2 and _winding((lx, ly), points) != 0:
            return True
        edges = zip(points, points[1:])
        if any(_distance_to_segment((lx, ly), a, b) <= reach for a, b in edges):
            if shape.closed or kind in {"line", "path"} or stroke:
                return True
    return False


# ── the tree ─────────────────────────────────────────────────────────────

class Tree:
    """Parent to children in paint order, built once per query."""

    def __init__(self, objects: list[Any]) -> None:
        self.objects = objects
        self.by_id = {item.id: item for item in objects}
        self.children: dict[str | None, list[Any]] = {}
        for item in objects:
            parent = item.parent_id if item.parent_id in self.by_id else None
            self.children.setdefault(parent, []).append(item)

    def parent(self, item: Any) -> Any | None:
        return self.by_id.get(item.parent_id)

    def ancestors(self, item: Any) -> list[Any]:
        chain = []
        current = self.parent(item)
        while current is not None and current not in chain:
            chain.append(current)
            current = self.parent(current)
        return chain

    def is_top_level_frame(self, item: Any) -> bool:
        parent = self.parent(item)
        return item.kind in FRAME_LIKE and (parent is None or parent.kind == "spread")

    def paint_order(self) -> list[Any]:
        """Every object, parents before children, siblings in list order."""
        result: list[Any] = []
        pending = list(reversed(self.children.get(None, [])))
        while pending:
            item = pending.pop()
            result.append(item)
            pending.extend(reversed(self.children.get(item.id, [])))
        return result


def clips(item: Any) -> bool:
    """Whether a container hides the parts of its children outside it."""
    return (item.kind in FRAME_LIKE and bool(getattr(item, "clip_content", True))
            and (getattr(item, "metadata", None) or {}).get("clip_content", True) is not False)


# ── masks (Figma: a mask layer masks the layers above it in its parent) ──

MASK_MODES = ("alpha", "outline", "luminance")


def mask_mode(item: Any) -> str | None:
    """"alpha", "outline" or "luminance" for a layer used as a mask, else None."""
    value = (getattr(item, "metadata", None) or {}).get("mask")
    if value is True:
        return "alpha"
    return value if value in MASK_MODES else None


def is_active_mask(item: Any) -> bool:
    return mask_mode(item) is not None and bool(getattr(item, "visible", True))


def masks_below(siblings: list[Any]) -> list[list[Any]]:
    """For each sibling (paint order), the masks under it that clip it."""
    active: list[Any] = []
    result: list[list[Any]] = []
    for item in siblings:
        result.append(list(active))
        if is_active_mask(item):
            active.append(item)
    return result


def mask_contains(mask: Any, wx: float, wy: float) -> bool:
    """Whether a world point is inside a mask's shape (its outline, filled)."""
    left, top, right, bottom = world_bounds(mask)
    if wx < left or wx > right or wy < top or wy > bottom:
        return False
    lx, ly = to_local(mask, wx, wy)
    if mask.kind in {"text", "image"} or mask.kind in CONTAINERS and mask.kind not in FRAME_LIKE and mask.kind != "boolean":
        return 0 <= lx <= mask.width and 0 <= ly <= mask.height
    shapes = [flatten_subpath(shape) for shape in outline(mask)]
    winding = sum(_winding((lx, ly), flat) for flat in shapes if len(flat) > 2)
    return winding % 2 != 0 if fill_rule(mask) == "evenodd" else winding != 0


def _hittable(item: Any) -> bool:
    return item.visible and not item.locked and item.kind not in NON_SCENE_KINDS


def hit_chain(tree: Tree, wx: float, wy: float, *, tolerance: float = 4.0,
              parent_id: str | None = None, exclude: set[str] | None = None) -> list[Any]:
    """Root-to-leaf chain of the top-most object under a point, or []."""
    exclude = exclude or set()
    siblings = tree.children.get(parent_id, [])
    masking = masks_below(siblings) if any(mask_mode(item) for item in siblings) else None
    for index in range(len(siblings) - 1, -1, -1):
        item = siblings[index]
        if item.id in exclude or not _hittable(item):
            continue
        if masking is not None and not all(mask_contains(mask, wx, wy) for mask in masking[index]):
            # Masked content is only there to click inside its mask.
            continue
        children = tree.children.get(item.id, [])
        if item.kind == "boolean":
            # A boolean is where its result is; its operands are reached through it.
            if not contains(item, wx, wy, tolerance=tolerance):
                continue
            chain = hit_chain(tree, wx, wy, tolerance=tolerance, parent_id=item.id, exclude=exclude)
            return [item, *chain] if chain else [item]
        inside_clip = True
        if clips(item):
            inside_clip = 0 <= to_local(item, wx, wy)[0] <= item.width and \
                0 <= to_local(item, wx, wy)[1] <= item.height
        if children and inside_clip:
            chain = hit_chain(tree, wx, wy, tolerance=tolerance, parent_id=item.id, exclude=exclude)
            if chain:
                return [item, *chain]
        if item.kind in TRANSPARENT_CONTAINERS:
            continue
        if contains(item, wx, wy, tolerance=tolerance):
            return [item]
    return []


def entered_scope(tree: Tree, selected_ids: Iterable[str]) -> set[str]:
    """Containers the person is inside: ancestors of the current selection."""
    entered: set[str] = set()
    for object_id in selected_ids:
        item = tree.by_id.get(object_id)
        if item is not None:
            entered.update(parent.id for parent in tree.ancestors(item))
    return entered


def click_target(tree: Tree, chain: list[Any], selected_ids: Iterable[str], *,
                 deep: bool = False) -> Any | None:
    """Which layer of a hit chain a click selects (Figma's rules)."""
    if not chain:
        return None
    if deep:
        return chain[-1]
    entered = entered_scope(tree, selected_ids)
    for index, item in enumerate(chain):
        last = index == len(chain) - 1
        if last:
            return item
        if tree.is_top_level_frame(item) or item.kind == "spread":
            continue
        if item.id in entered:
            continue
        return item
    return chain[-1]


def child_under(tree: Tree, container: Any, chain: list[Any]) -> Any | None:
    """For a double-click: the child of `container` in the hit chain."""
    for index, item in enumerate(chain[:-1]):
        if item.id == container.id:
            return chain[index + 1]
    return None


# ── marquee ──────────────────────────────────────────────────────────────

def _polygon_rect_intersect(polygon: list[Point], rect: tuple[float, float, float, float]) -> bool:
    left, top, right, bottom = rect
    rect_points = [(left, top), (right, top), (right, bottom), (left, bottom)]
    for shape in (polygon, rect_points):
        for (x0, y0), (x1, y1) in zip(shape, shape[1:] + shape[:1]):
            nx, ny = y1 - y0, x0 - x1
            first = [nx * x + ny * y for x, y in polygon]
            second = [nx * x + ny * y for x, y in rect_points]
            if max(first) < min(second) or max(second) < min(first):
                return False
    return True


def touches(item: Any, rect: tuple[float, float, float, float]) -> bool:
    if item.width <= 0 and item.height <= 0:
        return rect[0] <= item.x <= rect[2] and rect[1] <= item.y <= rect[3]
    return _polygon_rect_intersect(world_corners(item), rect)


def encloses(rect: tuple[float, float, float, float], item: Any) -> bool:
    left, top, right, bottom = world_bounds(item)
    return rect[0] <= left and rect[1] <= top and right <= rect[2] and bottom <= rect[3]


def marquee_selection(tree: Tree, rect: tuple[float, float, float, float], *,
                      enclose: bool = False, deep: bool = False) -> list[str]:
    """Layers a marquee selects: touch by default, Alt encloses, Ctrl deep."""
    left, right = sorted((rect[0], rect[2]))
    top, bottom = sorted((rect[1], rect[3]))
    rect = (left, top, right, bottom)

    def hits(item: Any) -> bool:
        return encloses(rect, item) if enclose else touches(item, rect)

    def leaves(parent_id: str | None) -> list[str]:
        found: list[str] = []
        for item in tree.children.get(parent_id, []):
            if not _hittable(item):
                continue
            children = [child for child in tree.children.get(item.id, []) if _hittable(child)]
            if children and item.kind != "boolean":
                found.extend(leaves(item.id))
            elif item.kind not in TRANSPARENT_CONTAINERS and hits(item):
                found.append(item.id)
        return found

    if deep:
        return leaves(None)
    selected: list[str] = []
    for item in tree.children.get(None, []):
        if not _hittable(item):
            continue
        frame = tree.is_top_level_frame(item) or item.kind == "spread"
        if frame:
            if encloses(rect, item):
                selected.append(item.id)
                continue
            for child in tree.children.get(item.id, []):
                if not _hittable(child):
                    continue
                if child.kind in FRAME_LIKE and tree.children.get(child.id):
                    # A page inside a spread behaves as a top-level frame.
                    if encloses(rect, child):
                        selected.append(child.id)
                    else:
                        selected.extend(grandchild.id for grandchild in tree.children.get(child.id, [])
                                        if _hittable(grandchild) and hits(grandchild))
                    continue
                if hits(child):
                    selected.append(child.id)
            continue
        if hits(item):
            selected.append(item.id)
    return selected


# ── resize and rotate ────────────────────────────────────────────────────

HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
HANDLE_POINTS = {"nw": (0, 0), "n": (0.5, 0), "ne": (1, 0), "e": (1, 0.5),
                 "se": (1, 1), "s": (0.5, 1), "sw": (0, 1), "w": (0, 0.5)}


@dataclass
class Box:
    x: float
    y: float
    width: float
    height: float
    flip_h: bool = False
    flip_v: bool = False


def resize_local(width: float, height: float, handle: str, lx: float, ly: float, *,
                 keep_aspect: bool = False, from_center: bool = False,
                 minimum: float = 0.01) -> tuple[float, float, float, float, bool, bool]:
    """New local box (left, top, right, bottom) when a handle moves to (lx, ly).

    Coordinates are in the original local box. Returns the box normalized to
    positive size, and whether each axis flipped by crossing the opposite edge.
    """
    horizontal = "w" in handle or "e" in handle
    vertical = "n" in handle or "s" in handle
    # Signed size along each axis, measured from the fixed side (or center).
    new_w, new_h = width, height
    if "e" in handle:
        new_w = (lx - width / 2) * 2 if from_center else lx
    elif "w" in handle:
        new_w = (width / 2 - lx) * 2 if from_center else width - lx
    if "s" in handle:
        new_h = (ly - height / 2) * 2 if from_center else ly
    elif "n" in handle:
        new_h = (height / 2 - ly) * 2 if from_center else height - ly
    if keep_aspect and width > 0 and height > 0:
        if horizontal and vertical:
            scale = max(abs(new_w) / width, abs(new_h) / height)
            new_w = math.copysign(scale * width, new_w)
            new_h = math.copysign(scale * height, new_h)
        elif horizontal:
            new_h = abs(new_w) / width * height
        else:
            new_w = abs(new_h) / height * width

    def span(size: float, full: float, low_handle: bool, high_handle: bool,
             moving: bool) -> tuple[float, float]:
        if from_center or not moving:
            return full / 2 - size / 2, full / 2 + size / 2
        if low_handle:
            return full - size, full
        return 0.0, size

    left, right = span(new_w, width, "w" in handle, "e" in handle, horizontal)
    top, bottom = span(new_h, height, "n" in handle, "s" in handle, vertical)
    if not horizontal and not keep_aspect:
        left, right = 0.0, width
    if not vertical and not keep_aspect:
        top, bottom = 0.0, height
    flip_x = right < left
    flip_y = bottom < top
    if flip_x:
        left, right = right, left
    if flip_y:
        top, bottom = bottom, top
    if right - left < minimum:
        right = left + minimum
    if bottom - top < minimum:
        bottom = top + minimum
    return left, top, right, bottom, flip_x, flip_y


def resize_item_box(item: Any, handle: str, wx: float, wy: float, *,
                    keep_aspect: bool = False, from_center: bool = False,
                    grab_offset: Point = (0.0, 0.0)) -> Box:
    """Resize one (possibly rotated) object so its handle follows a world point."""
    lx, ly = to_local(item, wx, wy)
    lx -= grab_offset[0]
    ly -= grab_offset[1]
    minimum = 0.0 if item.kind == "line" else 0.01
    left, top, right, bottom, flip_x, flip_y = resize_local(
        item.width, item.height, handle, lx, ly,
        keep_aspect=keep_aspect, from_center=from_center, minimum=minimum)
    new_w, new_h = right - left, bottom - top
    center_world = to_world(item, (left + right) / 2, (top + bottom) / 2)
    return Box(center_world[0] - new_w / 2, center_world[1] - new_h / 2, new_w, new_h,
               bool(getattr(item, "flip_h", False)) ^ flip_x,
               bool(getattr(item, "flip_v", False)) ^ flip_y)


def handle_position(box: tuple[float, float, float, float], handle: str) -> Point:
    left, top, right, bottom = box
    fx, fy = HANDLE_POINTS[handle]
    return left + (right - left) * fx, top + (bottom - top) * fy


def rotation_delta(center: Point, start: Point, current: Point) -> float:
    a = math.atan2(start[1] - center[1], start[0] - center[0])
    b = math.atan2(current[1] - center[1], current[0] - center[0])
    return math.degrees(b - a)


def normalize_angle(angle: float) -> float:
    angle = (angle + 180.0) % 360.0 - 180.0
    return 180.0 if angle == -180.0 else angle


def rotate_point(point: Point, center: Point, degrees: float) -> Point:
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    px, py = point[0] - center[0], point[1] - center[1]
    return center[0] + px * cos - py * sin, center[1] + px * sin + py * cos


def constrain_angle(dx: float, dy: float, step_degrees: float = 45.0) -> Point:
    """A drag vector snapped to the nearest multiple of `step_degrees`."""
    length = math.hypot(dx, dy)
    if length == 0:
        return 0.0, 0.0
    angle = math.atan2(dy, dx)
    step = math.radians(step_degrees)
    angle = round(angle / step) * step
    projected = dx * math.cos(angle) + dy * math.sin(angle)
    return projected * math.cos(angle), projected * math.sin(angle)


def constrain_move(dx: float, dy: float, diagonal_band: float = 10.0) -> Point:
    """Shift-drag: lock to the nearer axis; diagonals only near 45 degrees."""
    if dx == 0 and dy == 0:
        return 0.0, 0.0
    angle = abs(math.degrees(math.atan2(dy, dx))) % 90
    if abs(angle - 45) <= diagonal_band:
        return constrain_angle(dx, dy, 45.0)
    return (dx, 0.0) if abs(dx) >= abs(dy) else (0.0, dy)


def radius_from_handle(item: Any, corner: int, wx: float, wy: float) -> float:
    """Corner radius when a radius handle is dragged to a world point."""
    lx, ly = to_local(item, wx, wy)
    corners = [(0.0, 0.0, 1, 1), (item.width, 0.0, -1, 1),
               (item.width, item.height, -1, -1), (0.0, item.height, 1, -1)]
    cx, cy, sx, sy = corners[corner]
    along = ((lx - cx) * sx + (ly - cy) * sy) / 2
    return max(0.0, min(along, min(item.width, item.height) / 2))


def scale_path(path: list[dict], scale_x: float, scale_y: float) -> list[dict]:
    result = []
    for node in path:
        entry = dict(node)
        for key in ("x", "in_x", "out_x"):
            if key in entry:
                entry[key] = float(entry[key]) * scale_x
        for key in ("y", "in_y", "out_y"):
            if key in entry:
                entry[key] = float(entry[key]) * scale_y
        result.append(entry)
    return result


# ── constraints (a frame's children when the frame is resized) ───────────

def constrained_box(child: tuple[float, float, float, float], old_frame: tuple[float, float, float, float],
                    new_frame: tuple[float, float, float, float], horizontal: str = "left",
                    vertical: str = "top") -> tuple[float, float, float, float]:
    """Where a child goes when its frame changes from `old_frame` to `new_frame`.

    Boxes are (x, y, width, height). Constraints are Figma's: left, right,
    left and right, center, scale (and top, bottom, top and bottom). Both
    `left-right`/`left_right` and `centre`/`center` spellings are read.
    """
    horizontal = horizontal.replace("_", "-").replace("center", "centre")
    vertical = vertical.replace("_", "-").replace("center", "centre")

    def axis(start: float, size: float, old_start: float, old_size: float, new_start: float,
             new_size: float, rule: str, near: str, far: str, both: str) -> tuple[float, float]:
        offset = start - old_start
        trailing = old_size - offset - size
        if rule == far:
            return new_start + new_size - trailing - size, size
        if rule == both:
            return new_start + offset, max(0.01, new_size - offset - trailing)
        if rule == "centre":
            center = offset + size / 2
            return new_start + new_size / 2 + (center - old_size / 2) - size / 2, size
        if rule == "scale":
            factor = new_size / old_size if old_size else 1.0
            return new_start + offset * factor, max(0.01, size * factor)
        return new_start + offset, size

    x, width = axis(child[0], child[2], old_frame[0], old_frame[2], new_frame[0], new_frame[2],
                    horizontal, "left", "right", "left-right")
    y, height = axis(child[1], child[3], old_frame[1], old_frame[3], new_frame[1], new_frame[3],
                     vertical, "top", "bottom", "top-bottom")
    return x, y, width, height


def snap_gradient_point(items: Iterable[Any], x: float, y: float, zoom: float,
                        threshold: float = 6.0) -> tuple[float, float]:
    """Snap near finite object edges and box landmarks, in screen pixels.

    Landmark points take precedence over nearby edges; distant edge extensions
    never attract the pointer. Transform in local space to respect rotation/flips.
    """
    radius = threshold / max(zoom, 1e-6)
    points, edges = [], []
    for item in items:
        if not item.visible or item.kind in NON_SCENE_KINDS:
            continue
        w, h = item.width, item.height
        lx, ly = to_local(item, x, y)
        for px in (0, w / 2, w):
            for py in (0, h / 2, h):
                wx, wy = to_world(item, px, py)
                distance = math.hypot(wx - x, wy - y)
                if distance <= radius:
                    points.append((distance, wx, wy))
        for px, py in ((0, min(h, max(0, ly))), (w, min(h, max(0, ly))),
                       (min(w, max(0, lx)), 0), (min(w, max(0, lx)), h)):
            wx, wy = to_world(item, px, py)
            distance = math.hypot(wx - x, wy - y)
            if distance <= radius:
                edges.append((distance, wx, wy))
    candidates = points or edges
    return min(candidates)[1:] if candidates else (x, y)
