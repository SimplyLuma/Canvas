# SPDX-License-Identifier: Apache-2.0

"""Frames that hold their shape: constraints, and auto-layout.

Two rules decide where a child sits when its parent changes.

*Constraints* say how a child follows a resize — pinned to an edge, held in
the centre, or scaled with the parent. This is what makes a frame a container
rather than a group.

*Auto-layout* stacks children along an axis with a gap and padding, and the
frame either hugs what it contains or fills what contains it. It is how
interface layout is actually done, so Canvas follows Figma's model closely
rather than inventing one.

Both operate on the document graph and nothing else; the canvas is a view.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Document, LayoutObject


HORIZONTAL = ("left", "right", "left-right", "centre", "scale")
VERTICAL = ("top", "bottom", "top-bottom", "centre", "scale")


@dataclass(frozen=True)
class AutoLayout:
    """A frame's stacking rule, read from its free-form layout dictionary."""

    direction: str = "vertical"       # vertical | horizontal
    gap: float = 12.0
    padding_top: float = 16.0
    padding_right: float = 16.0
    padding_bottom: float = 16.0
    padding_left: float = 16.0
    align: str = "start"              # start | centre | end | stretch
    sizing: str = "fixed"             # fixed | hug | fill
    main_sizing: str | None = None
    cross_sizing: str | None = None
    primary_align: str = 'start'

    @classmethod
    def read(cls, item: LayoutObject) -> "AutoLayout | None":
        layout = item.layout or {}
        if not layout.get("auto"):
            return None
        padding = layout.get("padding", 16.0)
        if isinstance(padding, (int, float)):
            padding = {"top": padding, "right": padding, "bottom": padding, "left": padding}
        return cls(
            direction=layout.get("direction", "vertical"),
            gap=float(layout.get("gap", 12.0)),
            padding_top=float(padding.get("top", 16.0)),
            padding_right=float(padding.get("right", 16.0)),
            padding_bottom=float(padding.get("bottom", 16.0)),
            padding_left=float(padding.get("left", 16.0)),
            align=layout.get("align", "start"),
            sizing=layout.get("sizing", "fixed"),
            main_sizing=layout.get('main_sizing'),cross_sizing=layout.get('cross_sizing'),
            primary_align=layout.get('primary_align','start'),
        )

    def write(self) -> dict:
        return {
            "auto": True,
            "direction": self.direction,
            "gap": self.gap,
            "padding": {"top": self.padding_top, "right": self.padding_right,
                        "bottom": self.padding_bottom, "left": self.padding_left},
            "align": self.align,
            "sizing": self.sizing,
            "main_sizing":self.main_sizing,"cross_sizing":self.cross_sizing,
            "primary_align":self.primary_align,
        }


def apply_constraints(document: Document, frame: LayoutObject,
                      old_width: float, old_height: float) -> list[LayoutObject]:
    """Move and resize a frame's children to honour their constraints.

    Called after the frame's own geometry has changed, with the size it had
    before. Children with no constraint keep their offset from the top left,
    which is what "left" and "top" mean and the right default.
    """
    changed: list[LayoutObject] = []
    width_delta = frame.width - old_width
    height_delta = frame.height - old_height
    if not width_delta and not height_delta:
        return changed
    scale_x = frame.width / old_width if old_width else 1.0
    scale_y = frame.height / old_height if old_height else 1.0

    for child in document.children(frame.id):
        horizontal = (child.constraints or {}).get("horizontal", "left")
        vertical = (child.constraints or {}).get("vertical", "top")
        before = (child.x, child.y, child.width, child.height)

        # Offsets are measured inside the frame.
        left = child.x - frame.x
        top = child.y - frame.y
        right = old_width - (left + child.width)
        bottom = old_height - (top + child.height)

        if horizontal == "right":
            child.x = frame.x + frame.width - right - child.width
        elif horizontal in {"left-right","left_right"}:
            child.x = frame.x + left
            child.width = max(1.0, frame.width - left - right)
        elif horizontal in {"centre","center"}:
            child.x = frame.x + left + width_delta/2
        elif horizontal == "scale":
            child.x = frame.x + left * scale_x
            child.width = max(1.0, child.width * scale_x)
        else:
            child.x = frame.x + left

        if vertical == "bottom":
            child.y = frame.y + frame.height - bottom - child.height
        elif vertical in {"top-bottom","top_bottom"}:
            child.y = frame.y + top
            child.height = max(1.0, frame.height - top - bottom)
        elif vertical in {"centre","center"}:
            child.y = frame.y + top + height_delta/2
        elif vertical == "scale":
            child.y = frame.y + top * scale_y
            child.height = max(1.0, child.height * scale_y)
        else:
            child.y = frame.y + top

        if (child.x, child.y, child.width, child.height) != before:
            changed.append(child)
        dx,dy=child.x-before[0],child.y-before[1]
        if dx or dy:
            for descendant in document.descendants(child.id):descendant.x+=dx;descendant.y+=dy
        if child.kind in {"frame", "component", "layout", "instance"}:
            changed.extend(reflow(document,child,before[2],before[3]))
    return changed


def apply_auto_layout(document: Document, frame: LayoutObject) -> list[LayoutObject]:
    """Stack a frame's children, and size the frame if it hugs them."""
    rule = AutoLayout.read(frame)
    if rule is None:
        return []
    children=[child for child in document.children(frame.id) if child.visible and child.layout.get('positioning')!='absolute']
    if not children:return []
    changed=[]
    horizontal=rule.direction=='horizontal'
    main='width' if horizontal else 'height';cross='height' if horizontal else 'width'
    main_start=rule.padding_left if horizontal else rule.padding_top
    main_end=rule.padding_right if horizontal else rule.padding_bottom
    cross_start=rule.padding_top if horizontal else rule.padding_left
    cross_end=rule.padding_bottom if horizontal else rule.padding_right
    if (rule.main_sizing or rule.sizing)=='hug':
        setattr(frame,main,sum(getattr(child,main) for child in children)+rule.gap*(len(children)-1)+main_start+main_end)
    if (rule.cross_sizing or rule.sizing)=='hug':
        setattr(frame,cross,max(getattr(child,cross) for child in children)+cross_start+cross_end)
    inner_main=max(0.,getattr(frame,main)-main_start-main_end)
    inner_cross=max(0.,getattr(frame,cross)-cross_start-cross_end)
    growers=[child for child in children if child.grow]
    fixed=sum(getattr(child,main) for child in children if not child.grow)
    gap=rule.gap
    spare=max(0.,inner_main-fixed-gap*(len(children)-1))
    total_grow=sum(child.grow for child in growers) or 1.
    for child in growers:setattr(child,main,max(1.,spare*child.grow/total_grow))
    free=inner_main-sum(getattr(child,main) for child in children)-gap*(len(children)-1)
    if rule.primary_align=='space-between' and len(children)>1:gap+=max(0.,free)/(len(children)-1);free=0.
    cursor=main_start+(free/2 if rule.primary_align=='centre' else free if rule.primary_align=='end' else 0.)
    for child in children:
        before=(child.x,child.y,child.width,child.height)
        align='stretch' if child.layout.get('cross_fill') else rule.align
        if align=='stretch':setattr(child,cross,max(1.,inner_cross))
        cross_offset=cross_start+( (inner_cross-getattr(child,cross))/2 if align=='centre' else inner_cross-getattr(child,cross) if align=='end' else 0.)
        x=frame.x+(cursor if horizontal else cross_offset)
        y=frame.y+(cross_offset if horizontal else cursor)
        dx,dy=x-child.x,y-child.y
        child.x,child.y=x,y
        if dx or dy:
            for descendant in document.descendants(child.id):descendant.x+=dx;descendant.y+=dy
        if (child.x,child.y,child.width,child.height)!=before:changed.append(child)
        cursor+=getattr(child,main)+gap
    # A nested auto-layout frame lays itself out after its parent placed it.
    for child in children:
        if AutoLayout.read(child):
            changed.extend(apply_auto_layout(document, child))
    return changed


def reflow(document: Document, frame: LayoutObject,
           old_width: float, old_height: float) -> list[LayoutObject]:
    """Whichever rule the frame carries, applied after it changed size."""
    if AutoLayout.read(frame):
        return apply_auto_layout(document, frame)
    return apply_constraints(document, frame, old_width, old_height)
