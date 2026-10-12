# SPDX-License-Identifier: Apache-2.0

"""Pages, spreads, masters, and text that flows between frames.

This is the half of Canvas that InDesign occupies, and linked text is the part
of it most likely to be skipped — so it is here, and it is real: text that
does not fit one frame continues in the next frame of its thread, and text
with nowhere left to go is reported as overflowing rather than silently
disappearing.

Nothing here measures glyphs. Canvas lays text out by the same estimate the
canvas draws with, so the thread's arithmetic and the drawing agree; when a
real text engine arrives, both change together.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Document, LayoutObject, new_id


@dataclass(frozen=True)
class TextMetrics:
    """How much text a frame of a given size can hold."""

    size: float = 13.0
    line_height: float = 1.55
    average_character: float = 0.52   # of the type size, for a mixed face

    def characters_per_line(self, width: float) -> int:
        per = max(1.0, self.size * self.average_character)
        return max(1, int(width // per))

    def lines(self, height: float) -> int:
        line = max(1.0, self.size * self.line_height)
        return max(0, int(height // line))

    def capacity(self, item: LayoutObject) -> int:
        columns = max(1, item.columns)
        gap = item.column_gap * (columns - 1)
        column_width = max(1.0, (item.width - gap) / columns)
        return self.characters_per_line(column_width) * self.lines(item.height) * columns


def thread(document: Document, start: LayoutObject) -> list[LayoutObject]:
    """Every frame in a text thread, in order, starting from one of them."""
    # Walk back to the head first: a thread can be entered from the middle.
    head = start
    seen = {start.id}
    while True:
        previous = next(
            (item for item in document.objects
             if item.text_flow_next_id == head.id and item.id not in seen),
            None,
        )
        if previous is None:
            break
        head = previous
        seen.add(previous.id)

    chain = [head]
    seen = {head.id}
    current = head
    while current.text_flow_next_id:
        following = document.object(current.text_flow_next_id)
        if following is None or following.id in seen:
            break
        chain.append(following)
        seen.add(following.id)
        current = following
    return chain


def link(document: Document, source: LayoutObject, target: LayoutObject) -> None:
    """Thread one text frame into another."""
    if source.kind != "text" or target.kind != "text":
        raise ValueError("only text frames can be threaded")
    if target.id == source.id:
        raise ValueError("a text frame cannot flow into itself")
    if source.id in {item.id for item in thread(document, target)}:
        raise ValueError("that would make the thread loop")
    source.text_flow_next_id = target.id


def unlink(source: LayoutObject) -> None:
    source.text_flow_next_id = None


@dataclass(frozen=True)
class Flow:
    """The result of pouring a thread's text through its frames."""

    pieces: dict[str, str]
    overflow: str

    @property
    def overflows(self) -> bool:
        return bool(self.overflow)


def flow_text(document: Document, start: LayoutObject,
              metrics: TextMetrics | None = None) -> Flow:
    """Pour the thread's text through its frames, and say what did not fit.

    The whole thread's text is the head frame's text: threading is a way of
    laying out one body of text across several frames, not of stitching
    several bodies together.
    """
    metrics = metrics or TextMetrics()
    chain = thread(document, start)
    remaining = chain[0].text or ""
    pieces: dict[str, str] = {}

    for frame in chain:
        capacity = metrics.capacity(frame)
        if len(remaining) <= capacity:
            pieces[frame.id] = remaining
            remaining = ""
            continue
        # Break on a space so a word is not cut in half.
        cut = remaining.rfind(" ", 0, capacity + 1)
        if cut <= 0:
            cut = capacity
        pieces[frame.id] = remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip()

    return Flow(pieces, remaining)


def pages(document: Document) -> list[LayoutObject]:
    return [item for item in document.objects if item.kind in {"page", "spread"}]


def masters(document: Document) -> list[LayoutObject]:
    """Pages marked as masters — a page others are built from."""
    return [item for item in document.objects
            if item.kind == "page" and item.metadata.get("master")]


def make_master(page: LayoutObject) -> LayoutObject:
    page.metadata = {**page.metadata, "master": True}
    return page


def apply_master(document: Document, page: LayoutObject,
                 master: LayoutObject) -> list[LayoutObject]:
    """Put a master's contents on a page, without overwriting its own work.

    A master item that the page has already overridden — an object with the
    same name that the page owns outright — is left alone. That is what
    "per-page override" means, and it is why applying a master twice is safe.
    """
    page.master_id = master.id
    own = {child.name for child in document.children(page.id)
           if not child.metadata.get("from_master")}
    added: list[LayoutObject] = []

    existing = {child.name: child for child in document.children(page.id)
                if child.metadata.get("from_master")}

    for source in document.children(master.id):
        if source.name in own:
            continue
        clone = existing.pop(source.name, None)
        if clone is None:
            clone = LayoutObject(new_id(source.kind), source.kind, source.name,
                                 parent_id=page.id)
            document.objects.append(clone)
            added.append(clone)
        for field in ("width", "height", "fill", "stroke", "stroke_width",
                      "text", "radius", "corner_radii", "opacity", "path"):
            setattr(clone, field, getattr(source, field))
        clone.x = page.x + (source.x - master.x)
        clone.y = page.y + (source.y - master.y)
        clone.metadata = {**clone.metadata, "from_master": master.id}
        clone.locked = True

    # A master item that no longer exists leaves the page too.
    for orphan in existing.values():
        document.objects.remove(orphan)
    return added


def override_master_item(document: Document, item: LayoutObject) -> LayoutObject:
    """Let a page edit something its master put there."""
    metadata = dict(item.metadata)
    metadata.pop("from_master", None)
    item.metadata = metadata
    item.locked = False
    return item


def spread_pages(document: Document, spread: LayoutObject) -> list[LayoutObject]:
    """The pages that face each other in a spread."""
    return [child for child in document.children(spread.id) if child.kind == "page"]


def reorder_pages(document: Document, order: list[str]) -> None:
    """Put the pages in the given order, leaving everything else alone."""
    known = {item.id: item for item in pages(document)}
    if set(order) != set(known):
        raise ValueError("the new order must name every page exactly once")
    remaining = [item for item in document.objects if item.id not in known]
    document.objects = [known[page_id] for page_id in order] + remaining
