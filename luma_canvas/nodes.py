# SPDX-License-Identifier: MPL-2.0
"""Editing a vector's nodes: the model behind vector edit mode.

A path is a list of nodes in the layer's box. A node has a point, optional
handles (`in_x`, `in_y`, `out_x`, `out_y`, absolute in the box), `move` when
it starts another subpath, and `closed` on the last node of a closed subpath.
How a node's two handles follow each other is its `mirror`, Figma's three
choices: "none" (independent), "angle" (opposite directions, own lengths)
and "length" (angle and length). Older paths say `smooth`, read as "length".

Every function returns a new list and never changes its input, so a gesture
can always start again from the path it began with.
"""

from __future__ import annotations

import copy
import math
from typing import Iterable

Point = tuple[float, float]
MIRRORS = ("none", "angle", "length")


# ── structure ────────────────────────────────────────────────────────────

def subpaths(path: list[dict]) -> list[tuple[int, int]]:
    """Index ranges [start, end) of each subpath."""
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(path) + 1):
        if index == len(path) or path[index].get("move"):
            if index > start:
                ranges.append((start, index))
            start = index
    return ranges


def subpath_of(path: list[dict], index: int) -> tuple[int, int]:
    for start, end in subpaths(path):
        if start <= index < end:
            return start, end
    raise IndexError(index)


def is_closed(path: list[dict], start: int, end: int) -> bool:
    last = path[end - 1]
    return bool(last.get("closed") or last.get("close"))


def point(node: dict) -> Point:
    return float(node["x"]), float(node["y"])


def handle(node: dict, which: str) -> Point | None:
    if f"{which}_x" not in node:
        return None
    return float(node[f"{which}_x"]), float(node[f"{which}_y"])


def mirror_of(node: dict) -> str:
    value = node.get("mirror")
    if value in MIRRORS:
        return value
    return "length" if node.get("smooth") else "none"


def segments(path: list[dict]) -> list[tuple[int, int]]:
    """Every segment as (from node, to node), closing segments included."""
    result: list[tuple[int, int]] = []
    for start, end in subpaths(path):
        for index in range(start, end - 1):
            result.append((index, index + 1))
        if is_closed(path, start, end) and end - start > 1:
            result.append((end - 1, start))
    return result


def segment_curve(path: list[dict], segment: tuple[int, int]) -> tuple[Point, Point, Point, Point]:
    a, b = path[segment[0]], path[segment[1]]
    p0, p3 = point(a), point(b)
    return p0, handle(a, "out") or p0, handle(b, "in") or p3, p3


def is_straight(curve) -> bool:
    return curve[1] == curve[0] and curve[2] == curve[3]


def _cubic(curve, t: float) -> Point:
    p0, p1, p2, p3 = curve
    u = 1 - t
    return (u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1])


def nearest_on_segment(path: list[dict], segment: tuple[int, int], target: Point,
                       steps: int = 48) -> tuple[float, float]:
    """(t, distance) of the point on a segment nearest to `target`."""
    curve = segment_curve(path, segment)
    best_t, best = 0.0, math.inf
    for step in range(steps + 1):
        t = step / steps
        distance = math.dist(_cubic(curve, t), target)
        if distance < best:
            best_t, best = t, distance
    # Refine by bisection around the best sample.
    low, high = max(0.0, best_t - 1 / steps), min(1.0, best_t + 1 / steps)
    for _ in range(24):
        a, b = low + (high - low) / 3, high - (high - low) / 3
        if math.dist(_cubic(curve, a), target) < math.dist(_cubic(curve, b), target):
            high = b
        else:
            low = a
    t = (low + high) / 2
    return t, math.dist(_cubic(curve, t), target)


def nearest_segment(path: list[dict], target: Point, reach: float) -> tuple[tuple[int, int], float] | None:
    """The segment within `reach` of a point, and where on it."""
    found = None
    best = reach
    for segment in segments(path):
        t, distance = nearest_on_segment(path, segment, target)
        if distance <= best:
            found, best = (segment, t), distance
    return found


# ── edits ────────────────────────────────────────────────────────────────

def _copy(path: list[dict]) -> list[dict]:
    return [dict(node) for node in path]


def move_nodes(path: list[dict], indices: Iterable[int], dx: float, dy: float) -> list[dict]:
    """Move nodes with their handles."""
    result = _copy(path)
    for index in set(indices):
        node = result[index]
        for key in ("x", "in_x", "out_x"):
            if key in node:
                node[key] = float(node[key]) + dx
        for key in ("y", "in_y", "out_y"):
            if key in node:
                node[key] = float(node[key]) + dy
    return result


def move_handle(path: list[dict], index: int, which: str, x: float, y: float, *,
                break_mirror: bool = False) -> list[dict]:
    """Drag one handle. The other follows the node's mirror; Alt breaks it."""
    result = _copy(path)
    node = result[index]
    node[f"{which}_x"], node[f"{which}_y"] = float(x), float(y)
    if break_mirror:
        node["mirror"] = "none"
        node["smooth"] = False
        return result
    mode = mirror_of(node)
    other = "in" if which == "out" else "out"
    px, py = point(node)
    vx, vy = float(x) - px, float(y) - py
    if mode == "length":
        node[f"{other}_x"], node[f"{other}_y"] = px - vx, py - vy
    elif mode == "angle":
        existing = handle(node, other)
        length = math.dist(existing, (px, py)) if existing else math.hypot(vx, vy)
        norm = math.hypot(vx, vy)
        if norm > 1e-9:
            node[f"{other}_x"], node[f"{other}_y"] = px - vx / norm * length, py - vy / norm * length
    return result


def split_segment(path: list[dict], segment: tuple[int, int], t: float) -> tuple[list[dict], int]:
    """Add a node on a segment without changing the shape; returns the new node's index."""
    result = _copy(path)
    a_index, b_index = segment
    curve = segment_curve(path, segment)
    t = min(max(t, 1e-6), 1 - 1e-6)
    if is_straight(curve):
        x, y = _cubic(curve, t)
        new = {"x": x, "y": y}
    else:
        p0, p1, p2, p3 = curve

        def lerp(p, q):
            return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
        q0, q1, q2 = lerp(p0, p1), lerp(p1, p2), lerp(p2, p3)
        r0, r1 = lerp(q0, q1), lerp(q1, q2)
        s = lerp(r0, r1)
        result[a_index]["out_x"], result[a_index]["out_y"] = q0
        result[b_index]["in_x"], result[b_index]["in_y"] = q2
        new = {"x": s[0], "y": s[1], "in_x": r0[0], "in_y": r0[1], "out_x": r1[0], "out_y": r1[1],
               "mirror": "angle"}
    if b_index > a_index:
        position = b_index
    else:
        # The closing segment: the new node becomes the subpath's last node.
        position = a_index + 1
        if result[a_index].get("closed"):
            result[a_index] = {key: value for key, value in result[a_index].items() if key not in {"closed", "close"}}
            new["closed"] = True
    result.insert(position, new)
    return result, position


def delete_nodes(path: list[dict], indices: Iterable[int]) -> list[dict]:
    """Remove nodes; neighbors join. A subpath left with one node goes too."""
    doomed = set(indices)
    result: list[dict] = []
    for start, end in subpaths(path):
        closed = is_closed(path, start, end)
        kept = [dict(path[index]) for index in range(start, end) if index not in doomed]
        if len(kept) < 2:
            continue
        for node in kept:
            node.pop("closed", None)
            node.pop("close", None)
            node.pop("move", None)
        if result:
            kept[0]["move"] = True
        if closed:
            kept[-1]["closed"] = True
        result.extend(kept)
    return result


def set_closed(path: list[dict], index: int, closed: bool) -> list[dict]:
    """Close or open the subpath a node belongs to."""
    result = _copy(path)
    start, end = subpath_of(result, index)
    last = {key: value for key, value in result[end - 1].items() if key not in {"closed", "close"}}
    if closed:
        last["closed"] = True
    result[end - 1] = last
    return result


def set_mirror(path: list[dict], indices: Iterable[int], mode: str) -> list[dict]:
    """Corner ("none" with no handles), or smooth with mirrored handles."""
    if mode not in MIRRORS and mode != "corner":
        raise ValueError(f"unknown node type: {mode}")
    result = _copy(path)
    for index in set(indices):
        node = result[index]
        if mode == "corner":
            for key in ("in_x", "in_y", "out_x", "out_y", "smooth"):
                node.pop(key, None)
            node["mirror"] = "none"
            continue
        node["mirror"] = mode
        node["smooth"] = mode != "none"
        if mode == "none":
            continue
        previous, following = _neighbors(result, index)
        px, py = point(node)
        if handle(node, "in") is None and handle(node, "out") is None:
            # Handles along the line through the neighbors, a third of the way to each.
            if previous is None or following is None:
                continue
            dx, dy = following[0] - previous[0], following[1] - previous[1]
            norm = math.hypot(dx, dy) or 1.0
            ux, uy = dx / norm, dy / norm
            back = math.dist((px, py), previous) / 3
            ahead = math.dist((px, py), following) / 3
            if mode == "length":
                back = ahead = (back + ahead) / 2
            node["in_x"], node["in_y"] = px - ux * back, py - uy * back
            node["out_x"], node["out_y"] = px + ux * ahead, py + uy * ahead
            continue
        out = handle(node, "out") or (px, py)
        incoming = handle(node, "in") or (px, py)
        ox, oy = out[0] - px, out[1] - py
        ix, iy = px - incoming[0], py - incoming[1]
        ux, uy = ox + ix, oy + iy
        norm = math.hypot(ux, uy)
        if norm < 1e-9:
            continue
        ux, uy = ux / norm, uy / norm
        out_length, in_length = math.hypot(ox, oy), math.hypot(ix, iy)
        if mode == "length":
            out_length = in_length = (out_length + in_length) / 2
        node["out_x"], node["out_y"] = px + ux * out_length, py + uy * out_length
        node["in_x"], node["in_y"] = px - ux * in_length, py - uy * in_length
    return result


def _neighbors(path: list[dict], index: int) -> tuple[Point | None, Point | None]:
    start, end = subpath_of(path, index)
    closed = is_closed(path, start, end)
    previous = index - 1 if index > start else (end - 1 if closed else None)
    following = index + 1 if index < end - 1 else (start if closed else None)
    return (point(path[previous]) if previous is not None and previous != index else None,
            point(path[following]) if following is not None and following != index else None)


def detect_mirrors(path: list[dict]) -> list[dict]:
    """Name each node's mirror from its handles (after converting a shape)."""
    result = _copy(path)
    for node in result:
        if "mirror" in node:
            continue
        incoming, out = handle(node, "in"), handle(node, "out")
        if incoming is None or out is None:
            node["mirror"] = "length" if node.get("smooth") else "none"
            continue
        px, py = point(node)
        ix, iy = px - incoming[0], py - incoming[1]
        ox, oy = out[0] - px, out[1] - py
        cross = ix * oy - iy * ox
        dot = ix * ox + iy * oy
        li, lo = math.hypot(ix, iy), math.hypot(ox, oy)
        if li < 1e-9 or lo < 1e-9 or abs(cross) > 1e-3 * li * lo or dot <= 0:
            node["mirror"] = "none"
        else:
            node["mirror"] = "length" if abs(li - lo) < 1e-3 * max(li, lo) else "angle"
        node["smooth"] = node["mirror"] != "none"
    return result


def nodes_in_rect(path: list[dict], to_view, rect: tuple[float, float, float, float]) -> list[int]:
    left, right = sorted((rect[0], rect[2]))
    top, bottom = sorted((rect[1], rect[3]))
    found = []
    for index, node in enumerate(path):
        x, y = to_view(*point(node))
        if left <= x <= right and top <= y <= bottom:
            found.append(index)
    return found


def rebased(path: list[dict]) -> tuple[list[dict], tuple[float, float, float, float]] | None:
    """The path moved so its bounds start at 0, 0, and those bounds (left, top, width, height)."""
    from .geometry import nodes_outlines, subpath_bounds
    shapes = nodes_outlines(path)
    bounds = subpath_bounds(shapes) if shapes else None
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    moved = move_nodes(path, range(len(path)), -left, -top)
    return moved, (left, top, right - left, bottom - top)


def deepcopy(path: list[dict]) -> list[dict]:
    return copy.deepcopy(path)
