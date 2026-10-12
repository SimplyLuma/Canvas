# SPDX-License-Identifier: MPL-2.0
"""Boolean operations on curved shapes: union, subtract, intersect, exclude.

Any number of operands, each a set of subpaths (holes and separate parts
included) with its own fill rule, combine in one pass:

1. Curves are flattened adaptively (to a tolerance scaled to the shapes), and
   every straight piece remembers the curve and parameter range it came from.
2. Points are snapped to a fine grid, then every piece is split wherever it
   meets another: crossings, T-junctions and overlapping collinear runs. Pieces
   that coincide (two shapes sharing an edge) merge into one.
3. Each piece is classified exactly, without sampling near it: the winding of
   each operand just to either side comes from one ray cast from the piece's
   midpoint, plus the piece's own contribution. The operation says whether
   the result is inside on each side; a piece whose sides differ is part of
   the result's boundary, oriented with the inside on its left.
4. Boundary pieces link into closed loops, and runs of pieces from one
   original curve are joined back into that curve's exact sub-bézier (split by
   de Casteljau at the recorded parameters), so a union of two circles is
   still made of arcs, not polygons.

The result uses the nonzero rule: outer loops and holes have opposite
orientation. Operation semantics are Figma's: union of all, the bottom shape
minus all the others, the area common to all, and exclude as odd coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Iterable, Sequence

from .geometry import Subpath

OPERATIONS = ("union", "subtract", "intersect", "exclude")
Point = tuple[float, float]


@dataclass
class Shape:
    """One operand: subpaths in world coordinates and a fill rule."""
    subpaths: list[Subpath]
    rule: str = "nonzero"


@dataclass
class _Source:
    curve: tuple | None      # (p0, p1, p2, p3) for a cubic, None for a line segment
    key: int                 # identifies the original segment
    t0: float
    t1: float


@dataclass
class _Edge:
    a: Point
    b: Point
    operand: int
    source: _Source
    splits: list[tuple[float, Point]] = field(default_factory=list)


# ── flattening ───────────────────────────────────────────────────────────

def _cubic_point(p0, p1, p2, p3, t: float) -> Point:
    u = 1 - t
    return (u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1])


def _flat_enough(p0, p1, p2, p3, tolerance: float) -> bool:
    ux = 3 * p1[0] - 2 * p0[0] - p3[0]
    uy = 3 * p1[1] - 2 * p0[1] - p3[1]
    vx = 3 * p2[0] - p0[0] - 2 * p3[0]
    vy = 3 * p2[1] - p0[1] - 2 * p3[1]
    return max(ux * ux, vx * vx) + max(uy * uy, vy * vy) <= 16 * tolerance * tolerance


def split_cubic(p0, p1, p2, p3, t: float):
    """de Casteljau: the two halves of a cubic at t."""
    def lerp(a, b):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    q0, q1, q2 = lerp(p0, p1), lerp(p1, p2), lerp(p2, p3)
    r0, r1 = lerp(q0, q1), lerp(q1, q2)
    s = lerp(r0, r1)
    return (p0, q0, r0, s), (s, r1, q2, p3)


def sub_cubic(curve, t0: float, t1: float):
    """The piece of a cubic between two parameters (t0 may exceed t1: reversed)."""
    reverse = t0 > t1
    if reverse:
        t0, t1 = t1, t0
    p0, p1, p2, p3 = curve
    if t1 < 1.0:
        (p0, p1, p2, p3), _ = split_cubic(p0, p1, p2, p3, t1)
    if t0 > 0.0 and t1 > 0:
        _, (p0, p1, p2, p3) = split_cubic(p0, p1, p2, p3, t0 / t1)
    return (p3, p2, p1, p0) if reverse else (p0, p1, p2, p3)


def _flatten_cubic(curve, tolerance: float, t0: float = 0.0, t1: float = 1.0, depth: int = 0,
                   out: list | None = None) -> list[tuple[float, Point]]:
    """(t, point) samples after the start, adaptively."""
    if out is None:
        out = []
    p0, p1, p2, p3 = curve
    if depth >= 12 or _flat_enough(p0, p1, p2, p3, tolerance):
        out.append((t1, p3))
        return out
    left, right = split_cubic(p0, p1, p2, p3, 0.5)
    middle = (t0 + t1) / 2
    _flatten_cubic(left, tolerance, t0, middle, depth + 1, out)
    _flatten_cubic(right, tolerance, middle, t1, depth + 1, out)
    return out


# ── the operation ────────────────────────────────────────────────────────

def _inside(winding: int, rule: str) -> bool:
    return winding % 2 != 0 if rule == "evenodd" else winding != 0


def _combine(operation: str, insides: Sequence[bool]) -> bool:
    if operation == "union":
        return any(insides)
    if operation == "intersect":
        return all(insides)
    if operation == "subtract":
        return bool(insides) and insides[0] and not any(insides[1:])
    if operation == "exclude":
        return sum(1 for value in insides if value) % 2 == 1
    raise ValueError(f"unknown boolean operation: {operation}")


def boolean(operation: str, shapes: Sequence[Shape], *, tolerance: float | None = None) -> list[Subpath]:
    """The result of a boolean operation as closed subpaths (nonzero rule)."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown boolean operation: {operation}")
    shapes = [shape for shape in shapes if shape.subpaths]
    if not shapes:
        return []
    extent = _extent(shapes)
    if extent is None:
        return []
    size = max(extent[2] - extent[0], extent[3] - extent[1], 1e-6)
    tolerance = tolerance if tolerance is not None else max(size * 2e-4, 1e-3)
    # A power-of-two grid keeps whole and fractional-pixel coordinates exact.
    grid = 2.0 ** math.floor(math.log2(tolerance / 16))

    def snap(point: Point) -> Point:
        return (round(point[0] / grid) * grid, round(point[1] / grid) * grid)

    edges: list[_Edge] = []
    key = 0
    for index, shape in enumerate(shapes):
        for subpath in shape.subpaths:
            segments = list(subpath.segments)
            start = subpath.start
            if not segments:
                continue
            # Every operand is treated as closed, as a fill is.
            last = segments[-1]
            end = (last[1], last[2]) if last[0] == "L" else (last[5], last[6])
            if math.dist(end, start) > 1e-12:
                segments.append(("L", start[0], start[1]))
            cursor = start
            for segment in segments:
                key += 1
                if segment[0] == "L":
                    target = (segment[1], segment[2])
                    a, b = snap(cursor), snap(target)
                    if a != b:
                        edges.append(_Edge(a, b, index, _Source(None, key, 0.0, 1.0)))
                    cursor = target
                    continue
                curve = (cursor, (segment[1], segment[2]), (segment[3], segment[4]), (segment[5], segment[6]))
                previous_t, previous = 0.0, snap(cursor)
                for t, sample in _flatten_cubic(curve, tolerance):
                    point = snap(sample)
                    if point != previous:
                        edges.append(_Edge(previous, point, index, _Source(curve, key, previous_t, t)))
                        previous, previous_t = point, t
                cursor = curve[3]
    pieces = _split_all(edges, snap)
    boundary = _classify(pieces, shapes, operation)
    loops = _link(boundary)
    minimum_area = tolerance * tolerance * 4
    result = []
    for loop in loops:
        if abs(_loop_area(loop)) <= minimum_area:
            continue
        result.append(_rebuild(loop))
    return result


def _extent(shapes: Iterable[Shape]):
    xs: list[float] = []
    ys: list[float] = []
    for shape in shapes:
        for subpath in shape.subpaths:
            xs.append(subpath.start[0])
            ys.append(subpath.start[1])
            for segment in subpath.segments:
                xs.extend(segment[1::2])
                ys.extend(segment[2::2])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ── splitting ────────────────────────────────────────────────────────────

def _split_all(edges: list[_Edge], snap) -> list[tuple[Point, Point, list[tuple[int, _Source, bool]]]]:
    """Split every edge where another meets it; merge coinciding pieces.

    Returns unique pieces (a, b, provenance) where provenance lists
    (operand, source, same_direction_as_a_to_b).
    """
    order = sorted(range(len(edges)), key=lambda i: min(edges[i].a[0], edges[i].b[0]))
    for position, i in enumerate(order):
        first = edges[i]
        ax0, ax1 = sorted((first.a[0], first.b[0]))
        ay0, ay1 = sorted((first.a[1], first.b[1]))
        for j in order[position + 1:]:
            second = edges[j]
            bx0 = min(second.a[0], second.b[0])
            if bx0 > ax1:
                break
            by0, by1 = sorted((second.a[1], second.b[1]))
            if by0 > ay1 or by1 < ay0:
                continue
            _intersect(first, second, snap)
    merged: dict[tuple[Point, Point], tuple[Point, Point, list]] = {}
    for edge in edges:
        splits = sorted({(round(t, 12), point) for t, point in edge.splits if 0 < t < 1}, key=lambda value: value[0])
        points = [(0.0, edge.a)] + [(t, point) for t, point in splits] + [(1.0, edge.b)]
        span = edge.source.t1 - edge.source.t0
        for (ta, pa), (tb, pb) in zip(points, points[1:]):
            if pa == pb:
                continue
            source = _Source(edge.source.curve, edge.source.key,
                             edge.source.t0 + span * ta, edge.source.t0 + span * tb)
            forward = pa < pb
            unique = (pa, pb) if forward else (pb, pa)
            entry = merged.get(unique)
            if entry is None:
                entry = merged[unique] = (unique[0], unique[1], [])
            entry[2].append((edge.operand, source, forward))
    return list(merged.values())


def _intersect(first: _Edge, second: _Edge, snap) -> None:
    (x1, y1), (x2, y2) = first.a, first.b
    (x3, y3), (x4, y4) = second.a, second.b
    dx1, dy1 = x2 - x1, y2 - y1
    dx2, dy2 = x4 - x3, y4 - y3
    denominator = dx1 * dy2 - dy1 * dx2
    length1 = dx1 * dx1 + dy1 * dy1
    length2 = dx2 * dx2 + dy2 * dy2
    if length1 <= 0 or length2 <= 0:
        return
    if abs(denominator) <= 1e-12 * math.sqrt(length1 * length2):
        # Parallel: split where collinear pieces overlap.
        cross = (x3 - x1) * dy1 - (y3 - y1) * dx1
        if abs(cross) > 1e-9 * math.sqrt(length1) * max(1.0, math.sqrt(length1)):
            return
        for point in (second.a, second.b):
            t = ((point[0] - x1) * dx1 + (point[1] - y1) * dy1) / length1
            if 0 < t < 1 and point not in (first.a, first.b):
                first.splits.append((t, point))
        for point in (first.a, first.b):
            t = ((point[0] - x3) * dx2 + (point[1] - y3) * dy2) / length2
            if 0 < t < 1 and point not in (second.a, second.b):
                second.splits.append((t, point))
        return
    t = ((x3 - x1) * dy2 - (y3 - y1) * dx2) / denominator
    u = ((x3 - x1) * dy1 - (y3 - y1) * dx1) / denominator
    eps = 1e-9
    if t < -eps or t > 1 + eps or u < -eps or u > 1 + eps:
        return
    point = snap((x1 + t * dx1, y1 + t * dy1))
    if point not in (first.a, first.b) and 0 < t < 1:
        first.splits.append((t, point))
    elif point not in (first.a, first.b):
        # A crossing that snapped to an interior position of an end parameter.
        first.splits.append((min(max(t, 1e-9), 1 - 1e-9), point))
    if point not in (second.a, second.b):
        second.splits.append((min(max(u, 1e-9), 1 - 1e-9), point))


# ── classification ───────────────────────────────────────────────────────

def _classify(pieces, shapes: Sequence[Shape], operation: str):
    """Boundary pieces of the result, oriented with the inside on the left."""
    count = len(shapes)
    rules = [shape.rule for shape in shapes]
    # Horizontal bands of pieces for the +x rays, vertical bands for +y rays.
    ys = [piece[0][1] for piece in pieces] + [piece[1][1] for piece in pieces]
    xs = [piece[0][0] for piece in pieces] + [piece[1][0] for piece in pieces]
    if not pieces:
        return []
    bands = max(1, int(math.sqrt(len(pieces))))
    y_index = _BandIndex(pieces, bands, min(ys), max(ys), axis=1)
    x_index = _BandIndex(pieces, bands, min(xs), max(xs), axis=0)
    boundary = []
    for index, (a, b, provenance) in enumerate(pieces):
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        horizontal = a[1] == b[1]
        windings = [0] * count
        own = [0] * count
        if not horizontal:
            for other in y_index.candidates(my):
                if other == index:
                    continue
                (px, py), (qx, qy), sources = pieces[other]
                if (py <= my < qy) or (qy <= my < py):
                    x = px + (my - py) * (qx - px) / (qy - py)
                    if x > mx:
                        for operand, _source, forward in sources:
                            # (p -> q) with p < q lexicographically; direction flips when not forward.
                            upward = (qy > py) == forward
                            windings[operand] += 1 if upward else -1
            for operand, _source, forward in provenance:
                upward = (b[1] > a[1]) == forward
                own[operand] += 1 if upward else -1
            # The +x side of the piece; the -x side adds the piece's own crossing.
            plus = windings
            minus = [windings[k] + own[k] for k in range(count)]
            # Left of a->b (y down): for a downward piece (b.y > a.y) the left is +x.
            left_is_plus = b[1] > a[1]
        else:
            for other in x_index.candidates(mx):
                if other == index:
                    continue
                (px, py), (qx, qy), sources = pieces[other]
                if (px <= mx < qx) or (qx <= mx < px):
                    y = py + (mx - px) * (qy - py) / (qx - px)
                    if y > my:
                        for operand, _source, forward in sources:
                            rightward = (qx > px) == forward
                            windings[operand] += 1 if rightward else -1
            for operand, _source, forward in provenance:
                rightward = (b[0] > a[0]) == forward
                own[operand] += 1 if rightward else -1
            plus = windings
            minus = [windings[k] + own[k] for k in range(count)]
            # Left of a->b (y down): for a rightward piece the left is -y.
            left_is_plus = b[0] < a[0]
        inside_plus = _combine(operation, [_inside(plus[k], rules[k]) for k in range(count)])
        inside_minus = _combine(operation, [_inside(minus[k], rules[k]) for k in range(count)])
        if inside_plus == inside_minus:
            continue
        inside_left = inside_plus if left_is_plus else inside_minus
        operand, source, forward = provenance[0]
        if inside_left:
            boundary.append((a, b, source, forward))
        else:
            boundary.append((b, a, source, not forward))
    return boundary


class _BandIndex:
    def __init__(self, pieces, bands: int, low: float, high: float, axis: int) -> None:
        self.low, self.bands = low, bands
        self.step = (high - low) / bands if high > low else 1.0
        self.buckets: list[list[int]] = [[] for _ in range(bands)]
        for index, piece in enumerate(pieces):
            first = self._band(min(piece[0][axis], piece[1][axis]))
            last = self._band(max(piece[0][axis], piece[1][axis]))
            for band in range(first, last + 1):
                self.buckets[band].append(index)

    def _band(self, value: float) -> int:
        return max(0, min(self.bands - 1, int((value - self.low) / self.step)))

    def candidates(self, value: float) -> list[int]:
        return self.buckets[self._band(value)]


# ── loops and curves ─────────────────────────────────────────────────────

def _link(boundary):
    outgoing: dict[Point, list[int]] = {}
    for index, (a, _b, _source, _forward) in enumerate(boundary):
        outgoing.setdefault(a, []).append(index)
    used = [False] * len(boundary)
    loops = []
    for start in range(len(boundary)):
        if used[start]:
            continue
        loop = []
        current = start
        while current is not None and not used[current]:
            used[current] = True
            loop.append(boundary[current])
            a, b = boundary[current][0], boundary[current][1]
            options = [index for index in outgoing.get(b, []) if not used[index]]
            if not options:
                current = None
                break
            if len(options) == 1:
                current = options[0]
                continue
            # Several ways on: take the sharpest left turn, keeping loops simple.
            incoming = math.atan2(b[1] - a[1], b[0] - a[0])

            def turn(index: int) -> float:
                c = boundary[index][1]
                angle = math.atan2(c[1] - b[1], c[0] - b[0]) - incoming
                return (angle + math.pi) % (2 * math.pi)
            current = min(options, key=turn)
        if len(loop) >= 2 and loop[-1][1] == loop[0][0]:
            loops.append(loop)
    return loops


def _loop_area(loop) -> float:
    area = 0.0
    for a, b, _source, _forward in loop:
        area += a[0] * b[1] - b[0] * a[1]
    return area / 2


def _rebuild(loop) -> Subpath:
    """Join runs of pieces from one original segment back into exact curves."""
    # Rotate so the loop starts where a run begins.
    def same_run(previous, following) -> bool:
        return (previous[2].key == following[2].key and previous[3] == following[3]
                and abs(previous[2].t1 - following[2].t0) < 1e-9 if previous[3]
                else previous[2].key == following[2].key and previous[3] == following[3]
                and abs(previous[2].t0 - following[2].t1) < 1e-9)
    start = 0
    for index in range(len(loop)):
        if not same_run(loop[index - 1], loop[index]):
            start = index
            break
    ordered = loop[start:] + loop[:start]
    runs: list[list] = []
    for piece in ordered:
        if runs and same_run(runs[-1][-1], piece):
            runs[-1].append(piece)
        else:
            runs.append([piece])
    if len(runs) > 1 and same_run(runs[-1][-1], runs[0][0]):
        runs[0] = runs.pop() + runs[0]
    path = Subpath(runs[0][0][0], closed=True)
    for run in runs:
        begin, end = run[0][0], run[-1][1]
        source_first, source_last = run[0][2], run[-1][2]
        forward = run[0][3]
        curve = source_first.curve
        if curve is None:
            path.line(*end)
            continue
        t0 = source_first.t0 if forward else source_first.t1
        t1 = source_last.t1 if forward else source_last.t0
        p0, p1, p2, p3 = sub_cubic(curve, t0, t1)
        # The run's ends are snapped (and may be crossings): move the handles with them.
        d0 = (begin[0] - p0[0], begin[1] - p0[1])
        d3 = (end[0] - p3[0], end[1] - p3[1])
        path.cubic(p1[0] + d0[0], p1[1] + d0[1], p2[0] + d3[0], p2[1] + d3[1], end[0], end[1])
    return _merge_collinear(path)


def _merge_collinear(path: Subpath) -> Subpath:
    """Drop corners that are not corners: a vertex between two straight pieces on one line."""
    count = len(path.segments)
    if count < 3:
        return path
    # Vertex i starts segment i; the last segment ends back at vertex 0.
    vertices = [path.start] + [(segment[-2], segment[-1]) for segment in path.segments[:-1]]
    segments = list(path.segments)
    changed = True
    while changed and len(segments) > 2:
        changed = False
        size = len(segments)
        for index in range(size):
            previous = segments[index - 1]
            if previous[0] != "L" or segments[index][0] != "L":
                continue
            before, vertex, after = vertices[index - 1], vertices[index], vertices[(index + 1) % size]
            length = math.dist(before, after)
            if length <= 0:
                continue
            cross = (vertex[0] - before[0]) * (after[1] - before[1]) - (vertex[1] - before[1]) * (after[0] - before[0])
            dot = (vertex[0] - before[0]) * (after[0] - vertex[0]) + (vertex[1] - before[1]) * (after[1] - vertex[1])
            if abs(cross) / length < 1e-6 and dot >= 0:
                # Segment index-1 now runs straight to `after`.
                segments[index - 1] = ("L", after[0], after[1])
                del segments[index]
                del vertices[index]
                changed = True
                break
    result = Subpath(vertices[0], closed=True)
    for index, segment in enumerate(segments):
        target = vertices[(index + 1) % len(vertices)]
        if segment[0] == "L":
            result.line(*target)
        else:
            result.segments.append(segment)
    return result


def to_nodes(subpaths: Iterable[Subpath], offset: Point = (0.0, 0.0)) -> list[dict]:
    """Subpaths as Canvas path nodes in a box at `offset` (`move` starts a subpath)."""
    ox, oy = offset
    nodes: list[dict] = []

    def r(value: float) -> float:
        return round(float(value), 4)
    for subpath in subpaths:
        if not subpath.segments:
            continue
        first_index = len(nodes)
        first = {"x": r(subpath.start[0] - ox), "y": r(subpath.start[1] - oy)}
        if first_index:
            first["move"] = True
        nodes.append(first)
        previous = first
        for segment in subpath.segments:
            if segment[0] == "L":
                node = {"x": r(segment[1] - ox), "y": r(segment[2] - oy)}
            else:
                _, x1, y1, x2, y2, x, y = segment
                node = {"x": r(x - ox), "y": r(y - oy), "in_x": r(x2 - ox), "in_y": r(y2 - oy)}
                previous["out_x"], previous["out_y"] = r(x1 - ox), r(y1 - oy)
            nodes.append(node)
            previous = node
        if subpath.closed:
            last = nodes[-1]
            if len(nodes) - first_index > 1 and abs(last["x"] - first["x"]) < 1e-3 and abs(last["y"] - first["y"]) < 1e-3:
                if "in_x" in last:
                    first["in_x"], first["in_y"] = last["in_x"], last["in_y"]
                nodes.pop()
            nodes[-1]["closed"] = True
    return nodes


def area(subpaths: Iterable[Subpath], rule: str = "nonzero", samples: int = 200) -> float:
    """Filled area (Monte Carlo-free: exact polygon area of the flattened loops, signed sum)."""
    total = 0.0
    for subpath in subpaths:
        from .geometry import flatten_subpath
        points = flatten_subpath(subpath, 32)
        loop = 0.0
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
            loop += x0 * y1 - x1 * y0
        total += loop / 2
    return abs(total)


# ── live boolean layers ──────────────────────────────────────────────────
#
# A `boolean` layer is a container whose children are its operands. It draws
# the computed result with its own paints; its children are not drawn. The
# result is kept in the layer's `path` (in its box) with the operands'
# fingerprint in `metadata["boolean_key"]`, so an unchanged boolean is never
# recomputed (and an imported one keeps the geometry its editor computed).

BOOLEAN_KIND = "boolean"
NAMES = {"union": "Union", "subtract": "Subtract", "intersect": "Intersect", "exclude": "Exclude"}
GEOMETRY_FIELDS = ("kind", "x", "y", "width", "height", "rotation", "flip_h", "flip_v", "path", "radius",
                   "corner_radii", "point_count", "inner_ratio", "visible")


def operation_of(item) -> str:
    value = (getattr(item, "metadata", None) or {}).get("boolean_operation")
    return value if value in OPERATIONS else "union"


_TEXT_CONTEXT = None


def text_subpaths(item) -> list[Subpath] | None:
    """A text layer's glyphs as outlines in its box (None without Pango)."""
    global _TEXT_CONTEXT
    try:
        import cairo
        import gi
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        from .typography import build_layout
    except (ImportError, ValueError):
        return None
    if _TEXT_CONTEXT is None:
        _TEXT_CONTEXT = PangoCairo.FontMap.get_default().create_context()
        _TEXT_CONTEXT.set_round_glyph_positions(False)
    text = build_layout(_TEXT_CONTEXT, item)
    context = cairo.Context(cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None))
    context.translate(0, text.offset_y)
    PangoCairo.layout_path(context, text.layout)
    shapes: list[Subpath] = []
    current: Subpath | None = None
    for kind, points in context.copy_path():
        if kind == cairo.PATH_MOVE_TO:
            current = Subpath((points[0], points[1]), closed=True)
            shapes.append(current)
        elif kind == cairo.PATH_LINE_TO and current is not None:
            current.line(points[0], points[1])
        elif kind == cairo.PATH_CURVE_TO and current is not None:
            current.cubic(*points)
    return [shape for shape in shapes if shape.segments]


def _world_subpaths(item) -> list[Subpath]:
    from . import geometry
    if item.kind == "text":
        shapes = text_subpaths(item)
        if shapes is None:
            shapes = [geometry.rounded_rect(item.width, item.height, (0, 0, 0, 0))]
    elif item.kind == "image":
        shapes = [geometry.rounded_rect(item.width, item.height, geometry.corner_radii_of(item))]
    else:
        shapes = geometry.outline(item)
    result = []
    for shape in shapes:
        start = geometry.to_world(item, *shape.start)
        mapped = Subpath(start, closed=True)
        for segment in shape.segments:
            if segment[0] == "L":
                mapped.line(*geometry.to_world(item, segment[1], segment[2]))
            else:
                mapped.cubic(*geometry.to_world(item, segment[1], segment[2]),
                             *geometry.to_world(item, segment[3], segment[4]),
                             *geometry.to_world(item, segment[5], segment[6]))
        result.append(mapped)
    return result


def operand_shape(tree, item) -> Shape | None:
    """What one child contributes to a boolean, in world coordinates."""
    from . import geometry
    if not item.visible or item.kind in {"guide", "comment", "slice"}:
        return None
    children = tree.children.get(item.id, [])
    if item.kind == BOOLEAN_KIND:
        return Shape(_world_subpaths(item)) if item.path else None
    if children and item.kind not in geometry.FRAME_LIKE:
        # A group stands for the union of what is inside it.
        parts = [shape for shape in (operand_shape(tree, child) for child in children) if shape is not None]
        if not parts:
            return None
        return Shape(boolean("union", parts)) if len(parts) > 1 else parts[0]
    subpaths = _world_subpaths(item)
    return Shape(subpaths, geometry.fill_rule(item)) if subpaths else None


def fingerprint(tree, item) -> str:
    import json
    values = [operation_of(item)]
    pending = list(tree.children.get(item.id, []))
    while pending:
        child = pending.pop(0)
        values.append([getattr(child, name, None) for name in GEOMETRY_FIELDS]
                      + [(child.metadata or {}).get("fill_rule")])
        pending[0:0] = tree.children.get(child.id, [])
    return json.dumps(values, default=str, separators=(",", ":"))


def compute(tree, item) -> tuple[list[dict], tuple[float, float, float, float] | None]:
    """The result of a boolean layer as path nodes in its new box, and the box."""
    from . import geometry
    shapes = [shape for shape in (operand_shape(tree, child) for child in tree.children.get(item.id, []))
              if shape is not None]
    result = boolean(operation_of(item), shapes) if shapes else []
    bounds = geometry.subpath_bounds(result) if result else None
    if bounds is None:
        return [], None
    left, top, right, bottom = bounds
    return to_nodes(result, (left, top)), (left, top, max(right - left, 0.01), max(bottom - top, 0.01))


def update_booleans(objects: list) -> bool:
    """Recompute every boolean whose operands changed, innermost first."""
    from . import geometry
    if not any(item.kind == BOOLEAN_KIND for item in objects):
        return False
    tree = geometry.Tree(objects)
    booleans = [item for item in tree.paint_order() if item.kind == BOOLEAN_KIND]
    changed = False
    for item in reversed(booleans):
        key = fingerprint(tree, item)
        if (item.metadata or {}).get("boolean_key") == key:
            continue
        nodes, box = compute(tree, item)
        if box is not None:
            item.x, item.y, item.width, item.height = box
        item.rotation = 0.0
        item.flip_h = item.flip_v = False
        item.path = nodes
        item.metadata = {**(item.metadata or {}), "boolean_key": key}
        changed = True
    return changed


def flattened_nodes(item) -> tuple[list[dict], tuple[float, float, float, float]] | None:
    """A layer's outline as vector path nodes in its own box (Flatten)."""
    from . import geometry
    if item.kind in {"path", BOOLEAN_KIND} and item.path:
        return [dict(node) for node in item.path], (item.x, item.y, item.width, item.height)
    if item.kind == "text":
        shapes = text_subpaths(item) or []
    elif item.kind == "image":
        shapes = [geometry.rounded_rect(item.width, item.height, geometry.corner_radii_of(item))]
    else:
        shapes = geometry.outline(item)
    if not shapes:
        return None
    return to_nodes(shapes), (item.x, item.y, item.width, item.height)
