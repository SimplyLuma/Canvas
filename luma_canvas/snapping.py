# SPDX-License-Identifier: Apache-2.0

"""Snapping and measurement.

Placing something is the thing this application is for, so the rules are
stated here rather than scattered through the canvas:

* an object snaps to other objects' edges and centres, and to its parent
  frame's edges, centres and padding;
* when the gap being created matches a gap that already exists nearby, that is
  detected and shown with its measurement — the single most useful behaviour
  in this category;
* grids, baselines and guides snap;
* whole pixels snap, by default, for interface work;
* rotation snaps to 15° with the constrain modifier;
* and every one of those is suppressed while the escape modifier is held,
  because a snap you cannot escape is worse than no snap at all.

Nothing here draws. It reports what it found, and the canvas draws that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from . import geometry
from .model import Document, LayoutObject
from .paint import format_number


SNAP_DISTANCE = 6.0          # document units, when the caller gives no zoom
SCREEN_THRESHOLD = 6.0       # screen px; callers divide by zoom (spec 5.6 says 5; tuned for the owner)
EQUAL_SPACING_TOLERANCE = 1.0
ROTATION_INCREMENT = 15.0


@dataclass(frozen=True)
class Guide:
    """A line the canvas should draw because a snap is holding."""

    orientation: str          # "vertical" | "horizontal"
    position: float           # in document units
    start: float
    end: float
    kind: str = "edge"        # edge | centre | frame | grid | guide | spacing
    marks: tuple[float, ...] = ()   # where along the line to draw X marks


@dataclass(frozen=True)
class Measurement:
    """A distance worth showing, with the badge's text and where it goes."""

    x1: float
    y1: float
    x2: float
    y2: float
    distance: float
    label: str


@dataclass
class SnapResult:
    dx: float
    dy: float
    guides: list[Guide] = field(default_factory=list)
    measurements: list[Measurement] = field(default_factory=list)


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def centre_x(self) -> float:
        return self.x + self.width / 2

    @property
    def centre_y(self) -> float:
        return self.y + self.height / 2

    def moved(self, dx: float, dy: float) -> "Box":
        return Box(self.x + dx, self.y + dy, self.width, self.height)


def box_of(item: LayoutObject) -> Box:
    """The object's bounds in document space, rotation included."""
    left, top, right, bottom = geometry.world_bounds(item)
    return Box(left, top, right - left, bottom - top)


def union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    left = min(box.x for box in boxes)
    top = min(box.y for box in boxes)
    right = max(box.right for box in boxes)
    bottom = max(box.bottom for box in boxes)
    return Box(left, top, right - left, bottom - top)


def _label(value: float) -> str:
    return format_number(value)


class SnapEngine:
    """Everything a drag needs to know about where it should land."""

    def __init__(self, document: Document, *, pixel_snap: bool = True,
                 grid: float = 0.0, threshold: float = SNAP_DISTANCE) -> None:
        self.document = document
        self.pixel_snap = pixel_snap
        self.grid = grid
        self.threshold = threshold
        self._parent_ids = {item.parent_id for item in document.objects if item.parent_id}

    # ── candidates ───────────────────────────────────────────────────────

    def _peers(self, moving_ids: set[str]) -> list[LayoutObject]:
        """Everything the moving objects could snap against.

        Siblings and the parent frame, not the whole document: an object in a
        frame lines up with the things beside it, not with something on
        another page. Descendants of what moves move with it, so they are
        never targets.
        """
        moving = [item for item in self.document.objects if item.id in moving_ids]
        if not moving:
            return []
        excluded = set(moving_ids)
        for item in moving:
            excluded.update(child.id for child in self.document.descendants(item.id))
        parents = {item.parent_id for item in moving}
        return [
            item for item in self.document.objects
            if item.id not in excluded
            and item.visible
            and item.kind not in {"comment", "slice", "spread", "guide"}
            and (item.parent_id in parents or item.id in parents)
        ]

    def _guides(self) -> list[LayoutObject]:
        return [item for item in self.document.objects if item.kind == "guide" and item.visible]

    def _guide_values(self, axis: str) -> list[float]:
        values = []
        for guide in self._guides():
            guide_axis = "x" if (guide.metadata or {}).get("axis") == "x" else "y"
            if guide_axis == axis:
                values.append(float(guide.x if axis == "x" else guide.y))
        return values

    def _guide_snap(self, target: "Box", axis: str) -> float | None:
        """The offset that puts an edge or the centre of `target` on a guide."""
        mine = (target.x, target.centre_x, target.right) if axis == "x" else (target.y, target.centre_y, target.bottom)
        best = None
        for value in self._guide_values(axis):
            for my_value in mine:
                distance = value - my_value
                if abs(distance) <= self.threshold and (best is None or abs(distance) < abs(best)):
                    best = distance
        return best

    # ── the drag itself ──────────────────────────────────────────────────

    def snap_move(self, moving_ids: set[str], dx: float, dy: float, *,
                  suppressed: bool = False, axes: str = "xy") -> SnapResult:
        """Adjust a drag so it lands on something, and say what it landed on."""
        moving = [item for item in self.document.objects if item.id in moving_ids]
        source = union([box_of(item) for item in moving])
        if source is None:
            return SnapResult(dx, dy)
        if suppressed:
            # Held escape modifier: no snapping at all, and nothing drawn.
            return SnapResult(dx, dy)

        target = source.moved(dx, dy)
        peers = self._peers(moving_ids)
        guides: list[Guide] = []

        best_x = self._best_axis(target, peers, "x") if "x" in axes else None
        best_y = self._best_axis(target, peers, "y") if "y" in axes else None
        spacing_x = self._spacing_snap(target, peers, "x") if "x" in axes else None
        spacing_y = self._spacing_snap(target, peers, "y") if "y" in axes else None
        if spacing_x is not None and (best_x is None or abs(spacing_x) < abs(best_x[0])):
            best_x = (spacing_x, None)
        if spacing_y is not None and (best_y is None or abs(spacing_y) < abs(best_y[0])):
            best_y = (spacing_y, None)

        # Guides the person placed win over nearby layers.
        for axis in axes:
            guide = self._guide_snap(target, axis)
            if guide is None:
                continue
            if axis == "x" and (best_x is None or abs(guide) <= abs(best_x[0])):
                best_x = (guide, None)
            elif axis == "y" and (best_y is None or abs(guide) <= abs(best_y[0])):
                best_y = (guide, None)

        if best_x is not None:
            dx += best_x[0]
        if best_y is not None:
            dy += best_y[0]

        # Grid and baseline, when nothing better held.
        if best_x is None and self.grid and "x" in axes:
            dx += self._grid_offset(target.x)
        if best_y is None and self.grid and "y" in axes:
            dy += self._grid_offset(target.y)

        # Whole pixels last, so it never fights a snap that already holds.
        if self.pixel_snap and best_x is None and "x" in axes:
            dx = round(source.x + dx) - source.x
        if self.pixel_snap and best_y is None and "y" in axes:
            dy = round(source.y + dy) - source.y

        settled = source.moved(dx, dy)
        guides.extend(self.alignment_guides(settled, peers))
        spacing_guides, measurements = self.equal_spacing(settled, peers)
        guides.extend(spacing_guides)
        return SnapResult(dx, dy, guides, measurements)

    def _best_axis(self, target: Box, peers: list[LayoutObject],
                   axis: str) -> tuple[float, Guide | None] | None:
        """The nearest edge or centre alignment on one axis, if within reach."""
        if axis == "x":
            mine = (target.x, target.centre_x, target.right)
        else:
            mine = (target.y, target.centre_y, target.bottom)
        best: tuple[float, Guide | None] | None = None
        best_distance = self.threshold
        for peer in peers:
            box = box_of(peer)
            theirs = (box.x, box.centre_x, box.right) if axis == "x" else (box.y, box.centre_y, box.bottom)
            for my_value in mine:
                for their_value in theirs:
                    distance = abs(their_value - my_value)
                    if distance > best_distance:
                        continue
                    # Ties prefer the parent frame.
                    if distance == best_distance and best is not None and peer.id not in self._parent_ids:
                        continue
                    best_distance = distance
                    best = (their_value - my_value, None)
        return best

    def alignment_guides(self, settled: Box, peers: list[LayoutObject]) -> list[Guide]:
        """Red lines through every edge or centre the settled box shares."""
        guides: list[Guide] = []
        epsilon = 0.01
        for axis in ("x", "y"):
            mine = ((settled.x, settled.centre_x, settled.right) if axis == "x"
                    else (settled.y, settled.centre_y, settled.bottom))
            lines: dict[float, list[tuple[Box, LayoutObject]]] = {}
            for peer in peers:
                box = box_of(peer)
                theirs = (box.x, box.centre_x, box.right) if axis == "x" else (box.y, box.centre_y, box.bottom)
                for value in mine:
                    if any(abs(value - other) <= epsilon for other in theirs):
                        lines.setdefault(round(value, 3), []).append((box, peer))
            for value, matches in lines.items():
                if axis == "x":
                    start = min([settled.y] + [box.y for box, _ in matches])
                    end = max([settled.bottom] + [box.bottom for box, _ in matches])
                    marks = sorted({settled.y, settled.bottom,
                                    *[m for box, _ in matches for m in (box.y, box.bottom)]})
                else:
                    start = min([settled.x] + [box.x for box, _ in matches])
                    end = max([settled.right] + [box.right for box, _ in matches])
                    marks = sorted({settled.x, settled.right,
                                    *[m for box, _ in matches for m in (box.x, box.right)]})
                kind = "frame" if any(peer.id in self._parent_ids
                                      and peer.kind in {"frame", "page", "component"}
                                      for _, peer in matches) else "edge"
                guides.append(Guide("vertical" if axis == "x" else "horizontal", value,
                                    start, end, kind, tuple(marks)))
        return guides

    def _grid_offset(self, value: float) -> float:
        if not self.grid:
            return 0.0
        nearest = round(value / self.grid) * self.grid
        return nearest - value if abs(nearest - value) <= self.threshold else 0.0

    # ── equal spacing ────────────────────────────────────────────────────

    @staticmethod
    def _row(target: Box, boxes: list[Box], axis: str) -> list[Box]:
        if axis == "x":
            return [box for box in boxes if box.bottom > target.y and box.y < target.bottom]
        return [box for box in boxes if box.right > target.x and box.x < target.right]

    def _cross_gap_pairs(self, target: Box, boxes: list[Box], axis: str):
        """Infer rhythm only from a clear, adjacent row/column of like boxes."""
        start = (lambda b: b.x) if axis == "x" else (lambda b: b.y)
        end = (lambda b: b.right) if axis == "x" else (lambda b: b.bottom)
        cross_start = (lambda b: b.y) if axis == "x" else (lambda b: b.x)
        cross_end = (lambda b: b.bottom) if axis == "x" else (lambda b: b.right)
        neighbours = self._row(target, boxes, axis)
        before = [b for b in neighbours if end(b) <= start(target)]
        after = [b for b in neighbours if start(b) >= end(target)]
        anchors = [b for b in (max(before, key=end, default=None), min(after, key=start, default=None)) if b is not None]
        pairs = []
        for anchor in anchors:
            row = sorted([b for b in boxes if abs(start(b) - start(anchor)) <= self.threshold
                          and abs(end(b) - end(anchor)) <= self.threshold], key=cross_start)
            for a, b in zip(row, row[1:]):
                gap = cross_start(b) - cross_end(a)
                if (a is anchor or b is anchor) and gap > 0:
                    x0, x1, y0, y1 = ((cross_end(a), cross_start(b), start(a), end(a)) if axis == "y"
                                      else (start(a), end(a), cross_end(a), cross_start(b)))
                    clutter = any(other is not a and other is not b and other.x < x1 and other.right > x0
                                  and other.y < y1 and other.bottom > y0 for other in boxes)
                    if not clutter:
                        pairs.append((a, b, gap))
        return pairs

    def _spacing_snap(self, target: Box, peers: list[LayoutObject], axis: str) -> float | None:
        """Offset that makes a gap equal to its neighbor's, or to an existing gap."""
        boxes = [box_of(peer) for peer in peers if peer.id not in self._parent_ids]
        row = self._row(target, boxes, axis)
        start = (lambda box: box.x) if axis == "x" else (lambda box: box.y)
        end = (lambda box: box.right) if axis == "x" else (lambda box: box.bottom)
        position, size = (target.x, target.width) if axis == "x" else (target.y, target.height)
        before = [box for box in row if end(box) <= position + self.threshold and start(box) < position]
        after = [box for box in row if start(box) >= position + size - self.threshold and end(box) > position + size]
        near_before = max(before, key=end, default=None)
        near_after = min(after, key=start, default=None)
        ordered = sorted(row, key=start)
        gaps = [start(b) - end(a) for a, b in zip(ordered, ordered[1:]) if start(b) - end(a) > 0]
        if not gaps:
            pairs = self._cross_gap_pairs(target, boxes, axis)
            inferred = [gap for _, _, gap in pairs]
            if inferred and max(inferred) - min(inferred) <= self.threshold:
                gaps.append(sum(inferred) / len(inferred))
        candidates: list[float] = []
        if near_before is not None and near_after is not None:
            candidates.append((end(near_before) + start(near_after) - size) / 2)
        for gap in gaps:
            if near_before is not None:
                candidates.append(end(near_before) + gap)
            if near_after is not None:
                candidates.append(start(near_after) - size - gap)
        best = None
        for candidate in candidates:
            offset = candidate - position
            if abs(offset) <= self.threshold and (best is None or abs(offset) < abs(best)):
                best = offset
        return best

    def equal_spacing(self, target: Box,
                      peers: list[LayoutObject]) -> tuple[list[Guide], list[Measurement]]:
        """Say when the gap being made matches a gap that already exists.

        This is the behaviour that makes layout feel precise: it is not enough
        to align two things, the spaces between them have to agree.
        """
        guides: list[Guide] = []
        measurements: list[Measurement] = []
        boxes = [box_of(peer) for peer in peers if peer.id not in self._parent_ids]

        for axis in ("x", "y"):
            row = self._row(target, boxes, axis)
            start = (lambda box: box.x) if axis == "x" else (lambda box: box.y)
            end = (lambda box: box.right) if axis == "x" else (lambda box: box.bottom)
            before = [box for box in row if end(box) <= start(target) + EQUAL_SPACING_TOLERANCE]
            after = [box for box in row if start(box) >= end(target) - EQUAL_SPACING_TOLERANCE]
            near_before = max(before, key=end, default=None)
            near_after = min(after, key=start, default=None)
            my_gaps = []
            if near_before is not None:
                my_gaps.append((near_before, target, start(target) - end(near_before)))
            if near_after is not None:
                my_gaps.append((target, near_after, start(near_after) - end(target)))
            ordered = sorted(row + [target], key=start)
            all_gaps = [(a, b, start(b) - end(a)) for a, b in zip(ordered, ordered[1:])]
            # Show both distances when a perpendicular rhythm is holding.
            pairs = self._cross_gap_pairs(target, boxes, axis)
            for a, b, existing in pairs:
                for near_a, near_b, gap in my_gaps:
                    if gap <= 0 or abs(gap - existing) > EQUAL_SPACING_TOLERANCE:
                        continue
                    if axis == "y":
                        measurements.append(Measurement(a.right, a.centre_y, b.x, a.centre_y, existing, _label(existing)))
                        measurements.append(Measurement(target.centre_x, end(near_a), target.centre_x, start(near_b), gap, _label(gap)))
                    else:
                        measurements.append(Measurement(a.centre_x, a.bottom, a.centre_x, b.y, existing, _label(existing)))
                        measurements.append(Measurement(end(near_a), target.centre_y, start(near_b), target.centre_y, gap, _label(gap)))
                    break
            shown = False
            for _, _, gap in my_gaps:
                if gap <= 0:
                    continue
                equal = [entry for entry in all_gaps if abs(entry[2] - gap) <= EQUAL_SPACING_TOLERANCE]
                if len(equal) < 2 or not any(entry[0] is target or entry[1] is target for entry in equal):
                    continue
                for a, b, value in equal:
                    if axis == "x":
                        middle = max(a.y, b.y, target.y) + (min(a.bottom, b.bottom, target.bottom)
                                                            - max(a.y, b.y, target.y)) / 2
                        measurements.append(Measurement(end(a), middle, start(b), middle, value, _label(value)))
                    else:
                        middle = max(a.x, b.x, target.x) + (min(a.right, b.right, target.right)
                                                            - max(a.x, b.x, target.x)) / 2
                        measurements.append(Measurement(middle, end(a), middle, start(b), value, _label(value)))
                if not shown:
                    first, last = min(equal, key=lambda e: end(e[0])), max(equal, key=lambda e: start(e[1]))
                    if axis == "x":
                        guides.append(Guide("horizontal", target.centre_y, end(first[0]), start(last[1]), "spacing"))
                    else:
                        guides.append(Guide("vertical", target.centre_x, end(first[0]), start(last[1]), "spacing"))
                    shown = True
                break
        # One badge per gap.
        unique: dict[tuple, Measurement] = {}
        for measurement in measurements:
            unique[(round(measurement.x1, 2), round(measurement.y1, 2),
                    round(measurement.x2, 2), round(measurement.y2, 2))] = measurement
        return guides, list(unique.values())

    # ── measuring against whatever is under the pointer ──────────────────

    def distances_to(self, selection_ids: set[str],
                     hovered: LayoutObject | None) -> list[Measurement]:
        """The gaps between the selection and the thing being pointed at.

        Separated on an axis: the distance between facing edges. When the
        hovered layer contains the selection: the distances to its four
        inner edges.
        """
        moving = [item for item in self.document.objects if item.id in selection_ids]
        source = union([box_of(item) for item in moving])
        if source is None or hovered is None or hovered.id in selection_ids:
            return []
        other = box_of(hovered)
        found: list[Measurement] = []

        contains = (other.x <= source.x and other.y <= source.y
                    and other.right >= source.right and other.bottom >= source.bottom)
        if contains:
            cx, cy = source.centre_x, source.centre_y
            for x1, y1, x2, y2 in ((other.x, cy, source.x, cy), (source.right, cy, other.right, cy),
                                   (cx, other.y, cx, source.y), (cx, source.bottom, cx, other.bottom)):
                gap = abs(x2 - x1) + abs(y2 - y1)
                if gap > 0:
                    found.append(Measurement(x1, y1, x2, y2, gap, _label(gap)))
            return found

        if other.right <= source.x:
            gap = source.x - other.right
            y = max(min(source.centre_y, other.bottom), other.y)
            found.append(Measurement(other.right, y, source.x, y, gap, _label(gap)))
        elif source.right <= other.x:
            gap = other.x - source.right
            y = max(min(source.centre_y, other.bottom), other.y)
            found.append(Measurement(source.right, y, other.x, y, gap, _label(gap)))

        if other.bottom <= source.y:
            gap = source.y - other.bottom
            x = max(min(source.centre_x, other.right), other.x)
            found.append(Measurement(x, other.bottom, x, source.y, gap, _label(gap)))
        elif source.bottom <= other.y:
            gap = other.y - source.bottom
            x = max(min(source.centre_x, other.right), other.x)
            found.append(Measurement(x, source.bottom, x, other.y, gap, _label(gap)))
        return found

    # ── points, resizing and rotating ────────────────────────────────────

    def snap_point(self, moving_ids: set[str], x: float, y: float, *, axes: str = "xy",
                   suppressed: bool = False,
                   parent_id: str | None = None) -> tuple[float, float, list[Guide]]:
        """Snap a single point (a dragged edge, a drawing corner, a pen node)."""
        if suppressed:
            return x, y, []
        if moving_ids:
            peers = self._peers(moving_ids)
        else:
            peers = [item for item in self.document.objects
                     if item.visible and item.kind not in {"comment", "slice", "spread", "guide"}
                     and (item.parent_id == parent_id or item.id == parent_id)]
        guides: list[Guide] = []
        for axis in axes:
            value = x if axis == "x" else y
            best = None
            for peer in peers:
                box = box_of(peer)
                options = (box.x, box.centre_x, box.right) if axis == "x" else (box.y, box.centre_y, box.bottom)
                for option in options:
                    if abs(option - value) <= self.threshold and (best is None or abs(option - value) < abs(best[0] - value)):
                        best = (option, box)
            for guide_value in self._guide_values(axis):
                if abs(guide_value - value) <= self.threshold and (best is None or abs(guide_value - value) < abs(best[0] - value)):
                    best = (guide_value, None)
            if best is not None and best[1] is None:
                if axis == "x":
                    x = best[0]
                else:
                    y = best[0]
            elif best is not None:
                option, box = best
                if axis == "x":
                    x = option
                    guides.append(Guide("vertical", option, min(box.y, y), max(box.bottom, y), "edge",
                                        (box.y, box.bottom, y)))
                else:
                    y = option
                    guides.append(Guide("horizontal", option, min(box.x, x), max(box.right, x), "edge",
                                        (box.x, box.right, x)))
            elif self.pixel_snap:
                if axis == "x":
                    x = round(x)
                else:
                    y = round(y)
        return x, y, guides

    def snap_on_ray(self, moving_ids: set[str], origin: tuple[float, float], x: float, y: float, *,
                    parent_id: str | None = None) -> tuple[float, float, list[Guide]]:
        """Shift-drawn lines: snap the end along its 0/45/90 degree ray.

        Horizontal and vertical rays snap their free coordinate; diagonal rays
        meet each candidate edge or center line, and the nearest meeting
        within reach wins.
        """
        ox, oy = origin
        dx, dy = x - ox, y - oy
        if abs(dy) < 1e-9:
            x2, _, guides = self.snap_point(moving_ids, x, y, axes="x", parent_id=parent_id)
            return x2, y, guides
        if abs(dx) < 1e-9:
            _, y2, guides = self.snap_point(moving_ids, x, y, axes="y", parent_id=parent_id)
            return x, y2, guides
        if moving_ids:
            peers = self._peers(moving_ids)
        else:
            peers = [item for item in self.document.objects
                     if item.visible and item.kind not in {"comment", "slice", "spread", "guide"}
                     and (item.parent_id == parent_id or item.id == parent_id)]
        slope = dy / dx
        best: tuple[float, float, float, Box, str] | None = None
        for peer in peers:
            box = box_of(peer)
            for value in (box.x, box.centre_x, box.right):
                cy = oy + (value - ox) * slope
                distance = math.hypot(value - x, cy - y)
                if distance <= self.threshold * 1.5 and (best is None or distance < best[0]):
                    best = (distance, value, cy, box, "x")
            for value in (box.y, box.centre_y, box.bottom):
                cx = ox + (value - oy) / slope
                distance = math.hypot(cx - x, value - y)
                if distance <= self.threshold * 1.5 and (best is None or distance < best[0]):
                    best = (distance, cx, value, box, "y")
        if best is None:
            return x, y, []
        _, sx, sy, box, axis = best
        if axis == "x":
            guide = Guide("vertical", sx, min(box.y, sy), max(box.bottom, sy), "edge", (box.y, box.bottom, sy))
        else:
            guide = Guide("horizontal", sy, min(box.x, sx), max(box.right, sx), "edge", (box.x, box.right, sx))
        return sx, sy, [guide]

    def snap_resize(self, box: Box, edge: str, dx: float, dy: float, *,
                    suppressed: bool = False,
                    moving_ids: set[str] | None = None) -> tuple[float, float, list[Guide]]:
        """Snap the edge being dragged, not the whole object."""
        if suppressed:
            return dx, dy, []
        axes = ("x" if ("left" in edge or "right" in edge) else "") + \
               ("y" if ("top" in edge or "bottom" in edge) else "")
        moving_x = box.x + dx if "left" in edge else box.right + dx
        moving_y = box.y + dy if "top" in edge else box.bottom + dy
        x, y, guides = self.snap_point(moving_ids or set(), moving_x, moving_y, axes=axes)
        return dx + (x - moving_x), dy + (y - moving_y), guides


def snap_rotation(angle: float, *, constrain: bool) -> float:
    """Hold the constrain modifier and rotation lands on 15° steps."""
    if not constrain:
        return angle
    return round(angle / ROTATION_INCREMENT) * ROTATION_INCREMENT


def nudge(amount: float, *, large: bool, step: float = 1.0,
          large_step: float = 10.0) -> float:
    """One pixel, or the configurable larger step."""
    return amount * (large_step if large else step)
