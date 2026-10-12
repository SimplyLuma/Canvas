# SPDX-License-Identifier: MPL-2.0
"""Figma geometry: affine matrices, path command blobs and vector networks.

Everything here is pure Python and bounded. Coordinates come out in a node's
local box, as Canvas path nodes (`x`, `y`, absolute `in_x`/`in_y` and
`out_x`/`out_y` handles, `closed` on a subpath's last node and `move` on the
first node of every subpath after the first).

Formats (see docs/fig-import.md for sources):

- A commands blob is a sequence of records: one opcode byte, then little
  endian float32 coordinates: 0 close, 1 move (x y), 2 line (x y),
  3 quadratic (x1 y1 x y), 4 cubic (x1 y1 x2 y2 x y).
- A vector network blob is three uint32 counts (vertices, segments, regions),
  then vertices (uint32 style, float32 x, y), segments (uint32 style,
  uint32 start, float32 tangent x, y, uint32 end, float32 tangent x, y) with
  tangents relative to their vertex, then regions (uint32 flags whose low bit
  is 1 for nonzero winding and whose other bits are the style id, uint32 loop
  count, then per loop a uint32 count and that many segment indexes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import struct
from typing import Any, Iterable

MAX_COMMANDS = 2_000_000
MAX_NETWORK_ITEMS = 2_000_000


class GeometryError(ValueError):
    pass


# ── matrices ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Affine:
    """x' = a x + c y + e, y' = b x + d y + f (Figma m00 m01 m02 / m10 m11 m12)."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    @classmethod
    def from_figma(cls, value: Any) -> "Affine":
        if not isinstance(value, dict):
            return IDENTITY
        values = [_finite(value.get(key), default) for key, default in
                  (("m00", 1.0), ("m10", 0.0), ("m01", 0.0), ("m11", 1.0), ("m02", 0.0), ("m12", 0.0))]
        return cls(*values)

    def __matmul__(self, other: "Affine") -> "Affine":
        """self ∘ other: apply `other` first."""
        return Affine(
            self.a * other.a + self.c * other.b,
            self.b * other.a + self.d * other.b,
            self.a * other.c + self.c * other.d,
            self.b * other.c + self.d * other.d,
            self.a * other.e + self.c * other.f + self.e,
            self.b * other.e + self.d * other.f + self.f,
        )

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def apply_vector(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y, self.b * x + self.d * y

    @property
    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    def inverse(self) -> "Affine | None":
        det = self.determinant
        if abs(det) < 1e-12 or not math.isfinite(det):
            return None
        return Affine(self.d / det, -self.b / det, -self.c / det, self.a / det,
                      (self.c * self.f - self.d * self.e) / det,
                      (self.b * self.e - self.a * self.f) / det)

    def is_rigid(self, tolerance: float = 1e-3) -> bool:
        """Rotation, translation and at most a mirror: no scale or skew."""
        return (abs(math.hypot(self.a, self.b) - 1) < tolerance
                and abs(math.hypot(self.c, self.d) - 1) < tolerance
                and abs(self.a * self.c + self.b * self.d) < tolerance)

    def rotation_degrees(self) -> float:
        return math.degrees(math.atan2(self.b, self.a))


IDENTITY = Affine()


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def scale(sx: float, sy: float) -> Affine:
    return Affine(sx, 0.0, 0.0, sy, 0.0, 0.0)


# ── subpaths ─────────────────────────────────────────────────────────────

@dataclass
class Subpath:
    start: tuple[float, float]
    segments: list[tuple] = field(default_factory=list)   # ("L", x, y) | ("C", x1, y1, x2, y2, x, y)
    closed: bool = False
    # Vector network vertex of the start and of each segment's end, when known.
    vertices: list[int] = field(default_factory=list)

    def transformed(self, matrix: Affine) -> "Subpath":
        start = matrix.apply(*self.start)
        segments = []
        for segment in self.segments:
            if segment[0] == "L":
                segments.append(("L", *matrix.apply(segment[1], segment[2])))
            else:
                segments.append(("C", *matrix.apply(segment[1], segment[2]),
                                 *matrix.apply(segment[3], segment[4]),
                                 *matrix.apply(segment[5], segment[6])))
        return Subpath(start, segments, self.closed)

    def end(self) -> tuple[float, float]:
        if not self.segments:
            return self.start
        last = self.segments[-1]
        return (last[1], last[2]) if last[0] == "L" else (last[5], last[6])


def decode_commands(blob: bytes) -> list[Subpath]:
    """A Figma path commands blob as subpaths (raises GeometryError when damaged)."""
    counts = {0: 0, 1: 2, 2: 2, 3: 4, 4: 6}
    subpaths: list[Subpath] = []
    current: Subpath | None = None
    offset = 0
    length = len(blob)
    records = 0
    while offset < length:
        opcode = blob[offset]
        offset += 1
        count = counts.get(opcode)
        if count is None:
            raise GeometryError(f"unknown path command {opcode}")
        if offset + count * 4 > length:
            raise GeometryError("a path command is truncated")
        values = struct.unpack_from(f"<{count}f", blob, offset) if count else ()
        offset += count * 4
        records += 1
        if records > MAX_COMMANDS:
            raise GeometryError("the path has too many commands")
        if any(not math.isfinite(value) for value in values):
            raise GeometryError("a path command has an invalid number")
        if opcode == 1:
            current = Subpath((values[0], values[1]))
            subpaths.append(current)
            continue
        if current is None:
            if opcode == 0:
                continue
            current = Subpath((0.0, 0.0))
            subpaths.append(current)
        if opcode == 0:
            current.closed = True
            start = current.start
            current = Subpath(start)
            # A new subpath only exists once something is drawn in it.
            subpaths.append(current)
            continue
        if opcode == 2:
            current.segments.append(("L", values[0], values[1]))
        elif opcode == 3:
            x0, y0 = current.end()
            qx, qy, x, y = values
            current.segments.append(("C", x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0),
                                     x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y), x, y))
        else:
            current.segments.append(("C", *values))
    return [subpath for subpath in subpaths if subpath.segments]


# ── vector networks ──────────────────────────────────────────────────────

@dataclass
class Network:
    vertices: list[tuple[float, float, int]]
    segments: list[tuple[int, float, float, int, float, float, int]]   # start, tsx, tsy, end, tex, tey, style
    regions: list[tuple[str, int, list[list[int]]]]                    # winding, style, loops of segment indexes


def decode_network(blob: bytes) -> Network:
    try:
        vertex_count, segment_count, region_count = struct.unpack_from("<III", blob, 0)
        if (vertex_count > MAX_NETWORK_ITEMS or segment_count > MAX_NETWORK_ITEMS
                or region_count > MAX_NETWORK_ITEMS
                or 12 + vertex_count * 12 + segment_count * 28 + region_count * 8 > len(blob)):
            raise GeometryError("the vector network is larger than its data")
        offset = 12
        vertices = []
        for _ in range(vertex_count):
            style, x, y = struct.unpack_from("<Iff", blob, offset)
            offset += 12
            vertices.append((_finite(x), _finite(y), style))
        segments = []
        for _ in range(segment_count):
            style, start, tsx, tsy, end, tex, tey = struct.unpack_from("<IIffIff", blob, offset)
            offset += 28
            if start >= vertex_count or end >= vertex_count:
                raise GeometryError("a vector segment names a missing vertex")
            segments.append((start, _finite(tsx), _finite(tsy), end, _finite(tex), _finite(tey), style))
        regions = []
        for _ in range(region_count):
            flags, loop_count = struct.unpack_from("<II", blob, offset)
            offset += 8
            if loop_count > len(blob) - offset:
                raise GeometryError("a vector region is larger than its data")
            loops = []
            for _ in range(loop_count):
                (count,) = struct.unpack_from("<I", blob, offset)
                offset += 4
                if count * 4 > len(blob) - offset:
                    raise GeometryError("a vector loop is larger than its data")
                indexes = list(struct.unpack_from(f"<{count}I", blob, offset))
                offset += count * 4
                if any(index >= segment_count for index in indexes):
                    raise GeometryError("a vector loop names a missing segment")
                loops.append(indexes)
            regions.append(("NONZERO" if flags & 1 else "ODD", flags >> 1, loops))
    except struct.error as error:
        raise GeometryError(f"the vector network is truncated ({error})") from error
    return Network(vertices, segments, regions)


def _segment_subpath_piece(network: Network, index: int, reverse: bool, sx: float, sy: float):
    start, tsx, tsy, end, tex, tey, _style = network.segments[index]
    if reverse:
        start, end = end, start
        tsx, tsy, tex, tey = tex, tey, tsx, tsy
    x0, y0 = network.vertices[start][0] * sx, network.vertices[start][1] * sy
    x3, y3 = network.vertices[end][0] * sx, network.vertices[end][1] * sy
    if tsx == 0 and tsy == 0 and tex == 0 and tey == 0:
        return start, end, (x0, y0), ("L", x3, y3)
    return start, end, (x0, y0), ("C", x0 + tsx * sx, y0 + tsy * sy, x3 + tex * sx, y3 + tey * sy, x3, y3)


def _chain(network: Network, indexes: list[int], sx: float, sy: float, closed_hint: bool) -> Subpath | None:
    """Segments that follow one another (in either direction) as one subpath."""
    if not indexes:
        return None
    segments = network.segments
    first = segments[indexes[0]]
    reverse_first = False
    if len(indexes) > 1:
        following = segments[indexes[1]]
        if first[3] not in (following[0], following[3]) and first[0] in (following[0], following[3]):
            reverse_first = True
    start_vertex, cursor, start_point, piece = _segment_subpath_piece(network, indexes[0], reverse_first, sx, sy)
    subpath = Subpath(start_point, [piece], vertices=[start_vertex, cursor])
    for index in indexes[1:]:
        segment = segments[index]
        reverse = segment[0] != cursor and segment[3] == cursor
        _s, cursor, _p, piece = _segment_subpath_piece(network, index, reverse, sx, sy)
        subpath.segments.append(piece)
        subpath.vertices.append(cursor)
    subpath.closed = closed_hint or cursor == start_vertex
    if subpath.closed:
        end = subpath.end()
        if subpath.segments[-1][0] == "L" and math.isclose(end[0], subpath.start[0], abs_tol=1e-6) \
                and math.isclose(end[1], subpath.start[1], abs_tol=1e-6):
            # The closing line is implied by `closed`.
            subpath.segments.pop()
            subpath.vertices.pop()
            if not subpath.segments:
                return None
    return subpath


def round_corners(subpath: Subpath, radius_of) -> Subpath:
    """Round corners between straight segments, as Figma's vertex corner radius does.

    `radius_of(vertex)` is the radius at a network vertex. The rounding is a
    circular arc tangent to both segments, limited to half of each segment.
    Corners next to a curve keep their point.
    """
    if not subpath.segments or len(subpath.vertices) != len(subpath.segments) + 1:
        return subpath
    points = [subpath.start] + [subpath_end(segment) for segment in subpath.segments]
    segments = list(subpath.segments)
    vertices = list(subpath.vertices)
    closed = subpath.closed
    if closed:
        if not (math.isclose(points[-1][0], points[0][0], abs_tol=1e-6)
                and math.isclose(points[-1][1], points[0][1], abs_tol=1e-6)):
            segments.append(("L", points[0][0], points[0][1]))
            points.append(points[0])
            vertices.append(vertices[0])
    count = len(segments)
    corners: dict[int, tuple] = {}
    for k in (range(count) if closed else range(1, count)):
        incoming = segments[k - 1] if k > 0 else segments[-1]
        outgoing = segments[k]
        if incoming[0] != "L" or outgoing[0] != "L":
            continue
        radius = radius_of(vertices[k])
        if not radius or radius <= 0:
            continue
        px, py = points[k]
        ax, ay = points[k - 1] if k > 0 else points[count - 1]
        bx, by = points[k + 1]
        la, lb = math.hypot(ax - px, ay - py), math.hypot(bx - px, by - py)
        if la < 1e-9 or lb < 1e-9:
            continue
        ux, uy = (ax - px) / la, (ay - py) / la
        vx, vy = (bx - px) / lb, (by - py) / lb
        angle = math.acos(max(-1.0, min(1.0, ux * vx + uy * vy)))
        if angle < 1e-4 or angle > math.pi - 1e-4:
            continue
        tangent = radius / math.tan(angle / 2)
        tangent = min(tangent, la / 2, lb / 2)
        effective = tangent * math.tan(angle / 2)
        handle = 4 / 3 * math.tan((math.pi - angle) / 4) * effective
        t1 = (px + ux * tangent, py + uy * tangent)
        t2 = (px + vx * tangent, py + vy * tangent)
        corners[k] = (t1, t2, (t1[0] - ux * handle, t1[1] - uy * handle), (t2[0] - vx * handle, t2[1] - vy * handle))
    if not corners:
        return subpath
    start = corners[0][1] if closed and 0 in corners else points[0]
    result = Subpath(start, [], closed)
    for index, segment in enumerate(segments):
        end_position = index + 1
        corner = corners.get(0 if closed and end_position == count else end_position)
        if corner is None:
            result.segments.append(segment)
            continue
        t1, t2, c1, c2 = corner
        result.segments.append(("L", t1[0], t1[1]))
        result.segments.append(("C", c1[0], c1[1], c2[0], c2[1], t2[0], t2[1]))
    return result


def subpath_end(segment: tuple) -> tuple[float, float]:
    return (segment[1], segment[2]) if segment[0] == "L" else (segment[5], segment[6])


@dataclass
class NetworkShapes:
    regions: list[tuple[str, int, list[Subpath]]]   # winding, style id, closed loops
    open_paths: list[Subpath]                       # segments that belong to no region
    loose_closed: list[Subpath]                     # closed chains outside regions (unfilled)
    vertex_styles: list[int]


def network_shapes(network: Network, size: tuple[float, float], normalized: tuple[float, float]) -> NetworkShapes:
    sx = size[0] / normalized[0] if normalized[0] else 1.0
    sy = size[1] / normalized[1] if normalized[1] else 1.0
    used: set[int] = set()
    regions = []
    for winding, style, loops in network.regions:
        subpaths = []
        for loop in loops:
            used.update(loop)
            shape = _chain(network, loop, sx, sy, True)
            if shape is not None:
                subpaths.append(shape)
        if subpaths:
            regions.append((winding, style, subpaths))
    remaining = [index for index in range(len(network.segments)) if index not in used]
    open_paths: list[Subpath] = []
    loose_closed: list[Subpath] = []
    for chain in _walk_chains(network, remaining):
        shape = _chain(network, chain, sx, sy, False)
        if shape is None:
            continue
        (loose_closed if shape.closed else open_paths).append(shape)
    return NetworkShapes(regions, open_paths, loose_closed, [vertex[2] for vertex in network.vertices])


def _walk_chains(network: Network, indexes: Iterable[int]) -> list[list[int]]:
    """Group loose segments into maximal chains through vertices of degree two."""
    pending = list(indexes)
    by_vertex: dict[int, list[int]] = {}
    for index in pending:
        start, _, _, end, _, _, _ = network.segments[index]
        by_vertex.setdefault(start, []).append(index)
        by_vertex.setdefault(end, []).append(index)
    unused = set(pending)
    chains: list[list[int]] = []

    def other(index: int, vertex: int) -> int:
        start, _, _, end, _, _, _ = network.segments[index]
        return end if start == vertex else start

    # Start at open ends (degree one) or branch points first, so chains are long.
    order = sorted(pending, key=lambda index: min(len(by_vertex[network.segments[index][0]]),
                                                  len(by_vertex[network.segments[index][3]])) != 1)
    for seed in order:
        if seed not in unused:
            continue
        start, _, _, end, _, _, _ = network.segments[seed]
        # Walk from the end with degree != 2 when there is one.
        if len(by_vertex[end]) != 2 and len(by_vertex[start]) == 2:
            start, end = end, start
        chain = [seed]
        unused.discard(seed)
        cursor = end if network.segments[seed][0] == start else network.segments[seed][0]
        # Extend forward while the path runs through a degree-two vertex.
        while len(by_vertex.get(cursor, [])) == 2:
            following = next((index for index in by_vertex[cursor] if index in unused), None)
            if following is None:
                break
            unused.discard(following)
            chain.append(following)
            cursor = other(following, cursor)
        chains.append(chain)
    return chains


# ── Canvas path nodes ────────────────────────────────────────────────────

def to_nodes(subpaths: Iterable[Subpath]) -> list[dict[str, Any]]:
    """Subpaths as Canvas path nodes (multi-subpath: `move` starts one)."""
    nodes: list[dict[str, Any]] = []
    for subpath in subpaths:
        if not subpath.segments:
            continue
        first_index = len(nodes)
        first = {"x": _r(subpath.start[0]), "y": _r(subpath.start[1])}
        if first_index:
            first["move"] = True
        nodes.append(first)
        previous = first
        for segment in subpath.segments:
            if segment[0] == "L":
                node = {"x": _r(segment[1]), "y": _r(segment[2])}
            else:
                _, x1, y1, x2, y2, x, y = segment
                node = {"x": _r(x), "y": _r(y), "in_x": _r(x2), "in_y": _r(y2)}
                previous["out_x"], previous["out_y"] = _r(x1), _r(y1)
            nodes.append(node)
            previous = node
        if subpath.closed:
            last = nodes[-1]
            if len(nodes) - first_index > 1 and last["x"] == first["x"] and last["y"] == first["y"]:
                # The closing segment is the stored last segment: keep its
                # curve on the first node and drop the duplicate point.
                if "in_x" in last:
                    first["in_x"], first["in_y"] = last["in_x"], last["in_y"]
                nodes.pop()
            nodes[-1]["closed"] = True
    return nodes


def _r(value: float) -> float:
    return round(float(value), 4)


def bounds(subpaths: Iterable[Subpath]) -> tuple[float, float, float, float] | None:
    """Bounds of the control polygon (a safe superset of the curve)."""
    xs: list[float] = []
    ys: list[float] = []
    for subpath in subpaths:
        xs.append(subpath.start[0])
        ys.append(subpath.start[1])
        for segment in subpath.segments:
            xs.extend(segment[1::2])
            ys.extend(segment[2::2])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def rounded_rect(width: float, height: float, radii: tuple[float, float, float, float]) -> Subpath:
    kappa = 0.5522847498
    limit = max(0.0, min(width, height) / 2)
    tl, tr, br, bl = (max(0.0, min(limit, value)) for value in radii)
    path = Subpath((tl, 0.0), closed=True)
    path.segments.append(("L", width - tr, 0.0))
    if tr:
        path.segments.append(("C", width - tr + tr * kappa, 0, width, tr - tr * kappa, width, tr))
    path.segments.append(("L", width, height - br))
    if br:
        path.segments.append(("C", width, height - br + br * kappa, width - br + br * kappa, height,
                              width - br, height))
    path.segments.append(("L", bl, height))
    if bl:
        path.segments.append(("C", bl - bl * kappa, height, 0, height - bl + bl * kappa, 0, height - bl))
    path.segments.append(("L", 0.0, tl))
    if tl:
        path.segments.append(("C", 0, tl - tl * kappa, tl - tl * kappa, 0, tl, 0))
    return path


def ellipse(width: float, height: float) -> Subpath:
    kappa = 0.5522847498
    rx, ry = width / 2, height / 2
    ox, oy = rx * kappa, ry * kappa
    path = Subpath((width, ry), closed=True)
    path.segments.append(("C", width, ry + oy, rx + ox, height, rx, height))
    path.segments.append(("C", rx - ox, height, 0, ry + oy, 0, ry))
    path.segments.append(("C", 0, ry - oy, rx - ox, 0, rx, 0))
    path.segments.append(("C", rx + ox, 0, width, ry - oy, width, ry))
    return path


def flatten(subpaths: Iterable[Subpath], steps: int = 12) -> list[list[tuple[float, float]]]:
    result = []
    for subpath in subpaths:
        points = [subpath.start]
        x0, y0 = subpath.start
        for segment in subpath.segments:
            if segment[0] == "L":
                x0, y0 = segment[1], segment[2]
                points.append((x0, y0))
                continue
            _, x1, y1, x2, y2, x3, y3 = segment
            for step in range(1, steps + 1):
                t = step / steps
                u = 1 - t
                points.append((u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                               u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3))
            x0, y0 = x3, y3
        result.append(points)
    return result


def outlines_match(first: list[Subpath], second: list[Subpath], tolerance: float) -> bool:
    """Whether two outlines trace the same shape within `tolerance` (Hausdorff on samples)."""
    a = [point for points in flatten(first, 16) for point in points]
    b = [point for points in flatten(second, 16) for point in points]
    if not a or not b:
        return False

    def distance_to_polylines(point, shapes):
        best = math.inf
        px, py = point
        for points in shapes:
            for (x0, y0), (x1, y1) in zip(points, points[1:]):
                dx, dy = x1 - x0, y1 - y0
                length = dx * dx + dy * dy
                t = 0.0 if length <= 1e-12 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length))
                best = min(best, math.hypot(px - (x0 + t * dx), py - (y0 + t * dy)))
        return best

    shapes_a = [points + [points[0]] for points in flatten(first, 16)]
    shapes_b = [points + [points[0]] for points in flatten(second, 16)]
    stride_a = max(1, len(a) // 200)
    stride_b = max(1, len(b) // 200)
    return (all(distance_to_polylines(point, shapes_b) <= tolerance for point in a[::stride_a])
            and all(distance_to_polylines(point, shapes_a) <= tolerance for point in b[::stride_b]))
