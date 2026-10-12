# SPDX-License-Identifier: Apache-2.0

"""Paths: béziers, and the boolean operations between them.

A path is a list of nodes. Each node has a point, and optionally the two
handles that shape the curve into and out of it:

    {"x": .., "y": .., "in_x": .., "in_y": .., "out_x": .., "out_y": ..,
     "smooth": bool, "closed": bool}

Nodes and handles are in the object's box: relative to the object's `x` and
`y`, the same frame the renderer, the SVG exporter and the importer use, so
moving an object moves its path. `closed` on the last node closes the path;
the older `close` key is still read.

Booleans live in `booleans.py`: exact on curves, and live as `boolean`
layers whose children stay editable.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .model import LayoutObject, new_id


FLATTEN_STEPS = 24


@dataclass(frozen=True)
class Point:
    x: float
    y: float


def node(x: float, y: float, *, smooth: bool = False,
         in_point: tuple[float, float] | None = None,
         out_point: tuple[float, float] | None = None,
         closed: bool = False, close: bool = False) -> dict:
    entry: dict = {"x": float(x), "y": float(y), "smooth": bool(smooth)}
    if in_point:
        entry["in_x"], entry["in_y"] = float(in_point[0]), float(in_point[1])
    if out_point:
        entry["out_x"], entry["out_y"] = float(out_point[0]), float(out_point[1])
    if closed or close:
        entry["closed"] = True
    return entry


def is_closed(path: list[dict]) -> bool:
    """One key closes a path. `close` was written by earlier builds."""
    return bool(path) and (bool(path[-1].get("closed")) or bool(path[-1].get("close")))


def close_path(path: list[dict]) -> list[dict]:
    if not path:
        return path
    last = {key: value for key, value in path[-1].items() if key != "close"}
    last["closed"] = True
    path[-1] = last
    return path


def set_handle(path: list[dict], index: int, which: str,
               x: float, y: float, *, mirror: bool = True) -> list[dict]:
    """Move one handle, mirroring the other while the node is smooth.

    Alt-dragging breaks a handle, which is the same as clearing `smooth`; that
    decision belongs to the caller, so this only honours it.
    """
    if not 0 <= index < len(path):
        return path
    entry = dict(path[index])
    entry[f"{which}_x"], entry[f"{which}_y"] = float(x), float(y)
    if mirror and entry.get("smooth"):
        other = "out" if which == "in" else "in"
        entry[f"{other}_x"] = entry["x"] - (float(x) - entry["x"])
        entry[f"{other}_y"] = entry["y"] - (float(y) - entry["y"])
    path[index] = entry
    return path


def break_handle(path: list[dict], index: int) -> list[dict]:
    """Alt-drag: the two handles stop being a straight line through the node."""
    if 0 <= index < len(path):
        path[index] = {**path[index], "smooth": False}
    return path


def insert_node(path: list[dict], index: int, x: float, y: float) -> list[dict]:
    path.insert(index, node(x, y))
    return path


def remove_node(path: list[dict], index: int) -> list[dict]:
    if 0 <= index < len(path) and len(path) > 2:
        closing = is_closed(path) and index == len(path) - 1
        path.pop(index)
        if closing and path:
            close_path(path)
    return path


# ── flattening ───────────────────────────────────────────────────────────

def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, steps: int) -> list[Point]:
    points = []
    for step in range(1, steps + 1):
        t = step / steps
        u = 1 - t
        x = (u * u * u * p0.x + 3 * u * u * t * p1.x
             + 3 * u * t * t * p2.x + t * t * t * p3.x)
        y = (u * u * u * p0.y + 3 * u * u * t * p1.y
             + 3 * u * t * t * p2.y + t * t * t * p3.y)
        points.append(Point(x, y))
    return points


def flatten(path: list[dict], steps: int = FLATTEN_STEPS) -> list[Point]:
    """Turn a path into a polygon, following its curves."""
    if not path:
        return []
    points = [Point(float(path[0]["x"]), float(path[0]["y"]))]
    for index in range(len(path) - 1):
        start, end = path[index], path[index + 1]
        p0 = Point(float(start["x"]), float(start["y"]))
        p3 = Point(float(end["x"]), float(end["y"]))
        p1 = Point(float(start.get("out_x", p0.x)), float(start.get("out_y", p0.y)))
        p2 = Point(float(end.get("in_x", p3.x)), float(end.get("in_y", p3.y)))
        if (p1.x, p1.y) == (p0.x, p0.y) and (p2.x, p2.y) == (p3.x, p3.y):
            points.append(p3)
        else:
            points.extend(_cubic(p0, p1, p2, p3, steps))
    if is_closed(path):
        start, end = path[-1], path[0]
        p0 = Point(float(start["x"]), float(start["y"]))
        p3 = Point(float(end["x"]), float(end["y"]))
        p1 = Point(float(start.get("out_x", p0.x)), float(start.get("out_y", p0.y)))
        p2 = Point(float(end.get("in_x", p3.x)), float(end.get("in_y", p3.y)))
        if (p1.x, p1.y) == (p0.x, p0.y) and (p2.x, p2.y) == (p3.x, p3.y):
            points.append(p3)
        else:
            points.extend(_cubic(p0, p1, p2, p3, steps))
    return points


def rectangle_points(item: LayoutObject) -> list[Point]:
    return [Point(item.x, item.y), Point(item.x + item.width, item.y),
            Point(item.x + item.width, item.y + item.height),
            Point(item.x, item.y + item.height)]


def ellipse_points(item: LayoutObject, steps: int = 48) -> list[Point]:
    cx, cy = item.x + item.width / 2, item.y + item.height / 2
    rx, ry = item.width / 2, item.height / 2
    return [Point(cx + rx * math.cos(2 * math.pi * i / steps),
                  cy + ry * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def outline_of(item: LayoutObject) -> list[Point]:
    """A polygon for any object a boolean can be taken of."""
    if item.kind in {"path", "polygon", "star", "line"} and item.path:
        return flatten(item.path)
    if item.kind == "ellipse":
        return ellipse_points(item)
    return rectangle_points(item)


# ── booleans ─────────────────────────────────────────────────────────────

def boolean(operation: str, first: list[Point], second: list[Point]) -> list[Point]:
    """Union, subtract, intersect or exclude on two polygons: the largest result loop.

    Kept for callers of the polygon API; `booleans.boolean` works on curves,
    holes and any number of shapes, and is what boolean layers use.
    """
    from . import booleans
    from .geometry import Subpath

    def subpath(points: list[Point]) -> Subpath:
        shape = Subpath((points[0].x, points[0].y), closed=True)
        for point in points[1:]:
            shape.line(point.x, point.y)
        return shape
    if operation not in booleans.OPERATIONS:
        raise ValueError(f"unknown boolean operation: {operation}")
    if len(first) < 3 or len(second) < 3:
        return []
    result = booleans.boolean(operation, [booleans.Shape([subpath(first)]), booleans.Shape([subpath(second)])])
    if not result:
        return []
    from .geometry import flatten_subpath
    loops = [flatten_subpath(shape, 24)[:-1] for shape in result]
    largest = max(loops, key=lambda loop: abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1)
                                                   in zip(loop, loop[1:] + loop[:1]))))
    return [Point(x, y) for x, y in largest]


def path_from_points(points: list[Point]) -> list[dict]:
    path = [node(point.x, point.y) for point in points]
    return close_path(path)


def combine(operation: str, items: list[LayoutObject]) -> LayoutObject:
    """Make one path from several, keeping the operands as its children.

    The result records what it was made from and how, so the operation can be
    changed or undone later rather than being baked in.
    """
    if len(items) < 2:
        raise ValueError("a boolean needs at least two objects")
    result = outline_of(items[0])
    for item in items[1:]:
        result = boolean(operation, result, outline_of(item))
    if not result:
        raise ValueError("that operation leaves nothing behind")

    left = min(point.x for point in result)
    top = min(point.y for point in result)
    right = max(point.x for point in result)
    bottom = max(point.y for point in result)
    combined = LayoutObject(
        new_id("path"), "path", f"{operation.title()} of {len(items)}",
        parent_id=items[0].parent_id,
        x=left, y=top, width=max(1.0, right - left), height=max(1.0, bottom - top),
        fill=items[0].fill, stroke=items[0].stroke, stroke_width=items[0].stroke_width,
        path=path_from_points(result),
    )
    combined.metadata = {"boolean": operation,
                         "operands": [item.id for item in items]}
    return combined


# ── stroke and corners ───────────────────────────────────────────────────

def outline_stroke(item: LayoutObject) -> list[dict]:
    """Turn a stroke into a filled shape, at the alignment it is drawn with."""
    if not item.stroke_width:
        return list(item.path)
    half = item.stroke_width / 2
    offsets = {"center": (half, half), "inside": (0.0, item.stroke_width),
               "outside": (item.stroke_width, 0.0)}
    outward, inward = offsets.get(item.stroke_align, (half, half))
    points = outline_of(item)
    cx = sum(point.x for point in points) / len(points)
    cy = sum(point.y for point in points) / len(points)

    def offset(point: Point, amount: float) -> Point:
        dx, dy = point.x - cx, point.y - cy
        length = math.hypot(dx, dy) or 1.0
        return Point(point.x + dx / length * amount, point.y + dy / length * amount)

    outer = [offset(point, outward) for point in points]
    inner = [offset(point, -inward) for point in reversed(points)]
    return path_from_points(outer + inner)


def corner_radii(item: LayoutObject) -> tuple[float, float, float, float]:
    """The four corners, clockwise from the top left."""
    if item.corner_radii and len(item.corner_radii) == 4:
        return tuple(float(value) for value in item.corner_radii)
    return (item.radius, item.radius, item.radius, item.radius)


def set_corner_radius(item: LayoutObject, corner: str | None, value: float) -> None:
    """Set one corner, or all four when no corner is named."""
    order = ("top-left", "top-right", "bottom-right", "bottom-left")
    if corner is None:
        item.radius = max(0.0, value)
        item.corner_radii = []
        return
    if corner not in order:
        raise ValueError(f"unknown corner: {corner}")
    current = list(corner_radii(item))
    current[order.index(corner)] = max(0.0, value)
    item.corner_radii = current


def simplify(path: list[dict], tolerance: float = 1.0) -> list[dict]:
    """Drop nodes that sit on the line between their neighbours."""
    if len(path) < 3:
        return path
    kept = [path[0]]
    for index in range(1, len(path) - 1):
        previous, current, following = path[index - 1], path[index], path[index + 1]
        area = abs((float(following["x"]) - float(previous["x"]))
                   * (float(current["y"]) - float(previous["y"]))
                   - (float(current["x"]) - float(previous["x"]))
                   * (float(following["y"]) - float(previous["y"]))) / 2
        if area > tolerance:
            kept.append(current)
    kept.append(path[-1])
    return kept
