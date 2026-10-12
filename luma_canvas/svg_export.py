# SPDX-License-Identifier: MPL-2.0
"""SVG export that looks the way the canvas draws.

The document is written as a tree of groups that follows the layer tree, so
the file stays editable: every layer is a `<g>` with its id, name and kind.
What the canvas draws, the SVG draws:

* true béziers from `geometry.outline`, rotation and flips as a matrix;
* every fill and stroke in a layer's paint stacks: solid colors, linear and
  radial gradients (radial through a gradient transform, so rotated and
  skewed ellipses stay exact), images with fill, fit, crop and tile;
* inside and outside strokes as a doubled stroke clipped to, or masked by, the
  shape, dashes, caps, joins, line ends and per-side weights;
* drop shadows, inner shadows and layer blur as filters;
* frames that clip their content clip it (corner radii included) and draw
  their own stroke above their children, unclipped, as Figma does;
* masks: alpha and luminance masks as `<mask>`, outline masks as `<clipPath>`;
* text as outlines by default (exact glyphs, as Figma's "Outline text"), or
  as `<text>` with one `<tspan>` per laid-out line;
* layer opacity and blend modes (`mix-blend-mode`).

Paints SVG cannot express (angular and diamond gradients) are drawn by the
scene renderer into an embedded image clipped to the shape.
"""

from __future__ import annotations

import base64
import math
from html import escape
from typing import Any, Iterable

from . import geometry
from .model import Document, LayoutObject, asset_bytes
from .paint import (Color, color_of, group_effects_of, normalized_effect, paint_stack, sorted_stops,
                    to_hex)

BLEND_CSS = {"darken", "multiply", "color-burn", "lighten", "screen", "color-dodge", "overlay",
             "soft-light", "hard-light", "difference", "exclusion", "hue", "saturation", "color",
             "luminosity"}


def _n(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _attrs(values: dict[str, Any]) -> str:
    return "".join(f' {key}="{escape(str(value), quote=True)}"' for key, value in values.items()
                   if value is not None)


def path_data(shapes: list[geometry.Subpath]) -> str:
    parts: list[str] = []
    for shape in shapes:
        parts.append(f"M{_n(shape.start[0])} {_n(shape.start[1])}")
        for segment in shape.segments:
            if segment[0] == "L":
                parts.append(f"L{_n(segment[1])} {_n(segment[2])}")
            else:
                parts.append("C" + " ".join(_n(value) for value in segment[1:]))
        if shape.closed:
            parts.append("Z")
    return "".join(parts)


def local_matrix(item: Any) -> tuple[float, float, float, float, float, float]:
    """The layer's box (0..w, 0..h) to world coordinates."""
    angle = math.radians(float(getattr(item, "rotation", 0.0) or 0.0))
    sx = -1.0 if getattr(item, "flip_h", False) else 1.0
    sy = -1.0 if getattr(item, "flip_v", False) else 1.0
    cos, sin = math.cos(angle), math.sin(angle)
    skew = geometry.skew_of(item)
    a, b, c, d = cos * sx, sin * sx, (cos * skew - sin) * sy, (sin * skew + cos) * sy
    cx, cy = item.x + item.width / 2, item.y + item.height / 2
    return a, b, c, d, cx - item.width / 2 * a - item.height / 2 * c, cy - item.width / 2 * b - item.height / 2 * d


def _matrix_text(matrix) -> str | None:
    a, b, c, d, e, f = matrix
    if abs(a - 1) < 1e-9 and abs(b) < 1e-9 and abs(c) < 1e-9 and abs(d - 1) < 1e-9:
        return None if abs(e) < 1e-9 and abs(f) < 1e-9 else f"translate({_n(e)} {_n(f)})"
    return "matrix(" + " ".join(_n(value) for value in matrix) + ")"


def _color_attrs(color: Color, opacity: float, prefix: str) -> dict[str, str]:
    values = {prefix: to_hex(color.with_alpha(1.0))}
    alpha = max(0.0, min(1.0, color.a * opacity))
    if alpha < 0.9995:
        values[f"{prefix}-opacity"] = _n(alpha)
    return values


class SvgWriter:
    def __init__(self, document: Document, objects: list[LayoutObject], *, outline_text: bool = True) -> None:
        self.document = document
        self.objects = objects
        self.tree = geometry.Tree(objects)
        self.outline_text = outline_text
        self.defs: list[str] = []
        self._ids = 0
        self._pango = None
        self._legacy_masks: set[str] = set()

    def new_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    # ── the tree ─────────────────────────────────────────────────────────

    def write(self, bounds: tuple[float, float, float, float], roots: list[LayoutObject]) -> str:
        left, top, width, height = bounds
        body = self.children([root for root in roots])
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'width="{_n(width)}" height="{_n(height)}" viewBox="{_n(left)} {_n(top)} {_n(width)} {_n(height)}">'
                f'{defs}{body}</svg>\n')

    def children(self, siblings: list[LayoutObject]) -> str:
        """Siblings in paint order; a mask masks every sibling above it."""
        out: list[str] = []
        for index, item in enumerate(siblings):
            mode = geometry.mask_mode(item)
            if mode is None or not item.visible:
                out.append(self.layer(item))
                continue
            rest = siblings[index + 1:]
            if not rest:
                break
            inner = self.children(rest)
            ident = self.new_id("mask")
            if mode == "outline":
                shape = path_data(geometry.outline(item))
                self.defs.append(f'<clipPath id="{ident}" clipPathUnits="userSpaceOnUse">'
                                 f'<path d="{shape}"{_attrs({"transform": _matrix_text(local_matrix(item)), "clip-rule": "evenodd" if geometry.fill_rule(item) == "evenodd" else None})}/></clipPath>')
                out.append(f'<g clip-path="url(#{ident})">{inner}</g>')
            else:
                box = self._big_box()
                kind = "alpha" if mode == "alpha" else "luminance"
                self.defs.append(f'<mask id="{ident}" maskUnits="userSpaceOnUse" x="{_n(box[0])}" y="{_n(box[1])}" '
                                 f'width="{_n(box[2])}" height="{_n(box[3])}" style="mask-type:{kind}">'
                                 f'{self.layer(item, force_visible=True)}</mask>')
                out.append(f'<g mask="url(#{ident})">{inner}</g>')
            break
        return "".join(out)

    def _big_box(self) -> tuple[float, float, float, float]:
        bounds = geometry.union_bounds([item for item in self.objects]) or (0, 0, 1, 1)
        pad = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
        return bounds[0] - pad, bounds[1] - pad, bounds[2] - bounds[0] + pad * 2, bounds[3] - bounds[1] + pad * 2

    def layer(self, item: LayoutObject, *, force_visible: bool = False) -> str:
        if (not item.visible and not force_visible) or item.kind in {"guide", "comment", "slice"}:
            return ""
        children = self.tree.children.get(item.id, []) if item.kind != "boolean" else []
        style = []
        blend = (item.metadata or {}).get("blend")
        if blend in BLEND_CSS:
            style.append(f"mix-blend-mode:{blend}")
        attrs = {"id": item.id, "data-name": item.name, "data-canvas-kind": item.kind,
                 "opacity": _n(item.opacity) if item.opacity < 0.9995 else None,
                 "style": ";".join(style) or None}
        drawn = item.kind not in geometry.TRANSPARENT_CONTAINERS or item.kind == "layout"
        stroke_on_top = bool(children) and item.kind in geometry.FRAME_LIKE
        parts: list[str] = []
        matrix = _matrix_text(local_matrix(item))
        if drawn:
            body = self.content(item, include_stroke=not stroke_on_top)
            if body:
                parts.append(f"<g{_attrs({'transform': matrix})}>{body}</g>")
        if children:
            inner = self.children(children)
            if geometry.clips(item) and inner:
                ident = f"clip-{item.id}"
                custom = (item.metadata or {}).get("clip_path")
                shape = path_data(geometry.nodes_outlines(custom)) if custom else path_data(geometry.outline(item))
                self.defs.append(f'<clipPath id="{ident}" clipPathUnits="userSpaceOnUse">'
                                 f'<path d="{shape}"{_attrs({"transform": matrix})}/></clipPath>')
                inner = f'<g clip-path="url(#{ident})">{inner}</g>'
            parts.append(inner)
        if drawn and stroke_on_top:
            stroke = self.strokes(item)
            if stroke:
                parts.append(f"<g{_attrs({'transform': matrix})}>{stroke}</g>")
        if not parts:
            return ""
        group_effects = group_effects_of(item) if children else []
        if group_effects:
            # A group's shadow and blur are cast by everything in it at once.
            shadows = [effect for effect in group_effects if effect["kind"] == "drop_shadow"]
            blur = max((effect["blur"] for effect in group_effects if effect["kind"] == "layer_blur"), default=0.0)
            ident = self.outer_filter(item, shadows, blur, world=True)
            parts = [f'<g filter="url(#{ident})">{"".join(parts)}</g>']
        mask = self.tree.by_id.get(item.mask_id) if item.mask_id else None
        if mask is not None and mask.kind == "mask":
            # Older documents: a `mask` layer referenced by `mask_id` clips by its shape.
            ident = f"clip-{mask.id}"
            if ident not in self._legacy_masks:
                self._legacy_masks.add(ident)
                self.defs.append(f'<clipPath id="{ident}" clipPathUnits="userSpaceOnUse"><path d="{path_data(geometry.outline(mask))}"'
                                 f'{_attrs({"transform": _matrix_text(local_matrix(mask))})}/></clipPath>')
            attrs["clip-path"] = f"url(#{ident})"
        return f"<g{_attrs(attrs)}>{''.join(parts)}</g>"

    # ── one layer, in its own box ────────────────────────────────────────

    def content(self, item: LayoutObject, *, include_stroke: bool = True) -> str:
        effects = [value for value in (normalized_effect(effect) for effect in item.effects or []
                                       if isinstance(effect, dict)) if value]
        shapes = geometry.outline(item)
        closed = bool(shapes) and all(shape.closed for shape in shapes)
        d = path_data(shapes)
        fills = ""
        if item.kind == "image":
            fills = self.image_layer(item)
        elif item.kind == "text":
            fills = self.text(item)
        elif closed and d:
            fills = self.fills(item, d)
        inner_shadows = [effect for effect in effects if effect["kind"] == "inner_shadow"]
        if inner_shadows and fills and closed:
            fills = f'<g filter="url(#{self.inner_shadow_filter(item, inner_shadows)})">{fills}</g>'
        if not include_stroke:
            stroke = ""
        elif item.kind == "text":
            glyphs = self.text_outline_data(item)
            stroke = self.strokes(item, glyphs) if glyphs else ""
        else:
            stroke = self.strokes(item)
        body = fills + stroke
        if not body:
            return ""
        shadows = [effect for effect in effects if effect["kind"] == "drop_shadow"]
        blur = max((effect["blur"] for effect in effects if effect["kind"] == "layer_blur"), default=0.0)
        if shadows or blur > 0:
            body = f'<g filter="url(#{self.outer_filter(item, shadows, blur)})">{body}</g>'
        return body

    def _filter_region(self, item: LayoutObject, margin: float, world: bool = False) -> str:
        """The area a filter may draw into: the layer's own box, or its box in
        the page's coordinates (a group, whose children are placed there)."""
        left, top, right, bottom = geometry.world_bounds(item) if world else (0, 0, item.width, item.height)
        return (f'filterUnits="userSpaceOnUse" x="{_n(left - margin)}" y="{_n(top - margin)}" '
                f'width="{_n(right - left + margin * 2)}" height="{_n(bottom - top + margin * 2)}"')

    def outer_filter(self, item: LayoutObject, shadows: list[dict], blur: float, *, world: bool = False) -> str:
        ident = self.new_id("fx")
        margin = max([effect["blur"] * 3 + abs(effect["x"]) + abs(effect["y"]) + abs(effect["spread"]) for effect in shadows]
                     + [blur * 3]) + item.stroke_width * 2 + 4
        steps: list[str] = []
        names: list[str] = []
        solid_alpha = any(not effect.get("behind", True) for effect in shadows)
        if solid_alpha:
            # A shadow that must not show behind the layer is cast and cut by
            # the layer's shape, not by how see-through it is, as the canvas
            # draws it (and as a CSS box shadow behaves).
            steps.append('<feComponentTransfer in="SourceAlpha" result="shape">'
                         '<feFuncA type="linear" slope="255" intercept="0"/></feComponentTransfer>')
        for index, effect in enumerate(shadows):
            behind = effect.get("behind", True)
            source = "SourceAlpha" if behind else "shape"
            if effect["spread"] > 0:
                steps.append(f'<feMorphology in="{source}" operator="dilate" radius="{_n(effect["spread"])}" result="s{index}"/>')
                source = f"s{index}"
            elif effect["spread"] < 0:
                steps.append(f'<feMorphology in="{source}" operator="erode" radius="{_n(-effect["spread"])}" result="s{index}"/>')
                source = f"s{index}"
            color = effect["color"]
            steps.append(f'<feGaussianBlur in="{source}" stdDeviation="{_n(effect["blur"] / 2)}"/>'
                         f'<feOffset dx="{_n(effect["x"])}" dy="{_n(effect["y"])}" result="o{index}"/>'
                         f'<feFlood flood-color="{to_hex(color.with_alpha(1))}" flood-opacity="{_n(color.a)}"/>'
                         f'<feComposite in2="o{index}" operator="in" result="d{index}"/>')
            if not behind:
                # A primitive must not name its own input as its result.
                steps.append(f'<feComposite in="d{index}" in2="shape" operator="out" result="k{index}"/>')
                names.append(f"k{index}")
            else:
                names.append(f"d{index}")
        if shadows:
            merged = "".join(f'<feMergeNode in="{name}"/>' for name in names)
            steps.append(f'<feMerge result="shadowed">{merged}<feMergeNode in="SourceGraphic"/></feMerge>')
        if blur > 0:
            steps.append(f'<feGaussianBlur in="{"shadowed" if shadows else "SourceGraphic"}" stdDeviation="{_n(blur / 2)}"/>')
        self.defs.append(f'<filter id="{ident}" {self._filter_region(item, margin, world)} color-interpolation-filters="sRGB">'
                         f'{"".join(steps)}</filter>')
        return ident

    def inner_shadow_filter(self, item: LayoutObject, shadows: list[dict]) -> str:
        ident = self.new_id("fx")
        steps: list[str] = []
        names: list[str] = []
        for index, effect in enumerate(shadows):
            color = effect["color"]
            steps.append(f'<feComponentTransfer in="SourceAlpha" result="inv{index}"><feFuncA type="table" tableValues="1 0"/></feComponentTransfer>'
                         f'<feGaussianBlur in="inv{index}" stdDeviation="{_n(effect["blur"] / 2)}"/>'
                         f'<feOffset dx="{_n(effect["x"])}" dy="{_n(effect["y"])}" result="o{index}"/>'
                         f'<feFlood flood-color="{to_hex(color.with_alpha(1))}" flood-opacity="{_n(color.a)}"/>'
                         f'<feComposite in2="o{index}" operator="in"/>'
                         f'<feComposite in2="SourceAlpha" operator="in" result="i{index}"/>')
            names.append(f"i{index}")
        merged = "".join(f'<feMergeNode in="{name}"/>' for name in names)
        self.defs.append(f'<filter id="{ident}" {self._filter_region(item, 4)} color-interpolation-filters="sRGB">'
                         f'{"".join(steps)}<feMerge><feMergeNode in="SourceGraphic"/>{merged}</feMerge></filter>')
        return ident

    # ── paints ───────────────────────────────────────────────────────────

    def fills(self, item: LayoutObject, d: str) -> str:
        rule = "evenodd" if geometry.fill_rule(item) == "evenodd" else None
        out: list[str] = []
        for paint in paint_stack(item, "fill"):
            if paint.get("visible", True) is False:
                continue
            out.append(self.paint_shape(item, paint, d, rule))
        return "".join(out)

    def paint_shape(self, item: LayoutObject, paint: dict, d: str, rule: str | None) -> str:
        kind = paint.get("type", "solid")
        opacity = float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0)
        blend = paint.get("blend")
        style = {"style": f"mix-blend-mode:{blend}"} if blend in BLEND_CSS else {}
        if kind == "solid":
            color = color_of(paint.get("color"))
            if color is None:
                return ""
            return f'<path d="{d}"{_attrs({**_color_attrs(color, opacity, "fill"), "fill-rule": rule, **style})}/>'
        if kind in {"linear", "radial"}:
            ident = self.gradient(item, paint)
            return f'<path d="{d}"{_attrs({"fill": f"url(#{ident})", "fill-rule": rule, **style})}/>'
        clip = self.new_id("clip")
        self.defs.append(f'<clipPath id="{clip}"><path d="{d}"{_attrs({"clip-rule": rule})}/></clipPath>')
        if kind == "image":
            return f'<g clip-path="url(#{clip})"{_attrs(style)}>{self.image_paint(item, paint)}</g>'
        return f'<g clip-path="url(#{clip})"{_attrs(style)}>{self.raster_paint(item, paint)}</g>'

    def gradient(self, item: LayoutObject, paint: dict) -> str:
        ident = self.new_id("paint")
        opacity = float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0)
        stops = "".join(
            f'<stop offset="{_n(max(0.0, min(1.0, float(stop["position"]))))}"'
            f'{_attrs(_color_attrs(Color.from_dict(stop["color"]), opacity, "stop-color"))}/>'
            for stop in sorted_stops(paint))
        handles = paint.get("handles") or []
        width, height = max(item.width, 1e-6), max(item.height, 1e-6)
        if paint.get("type") == "radial":
            cx, cy = handles[0] if handles else (0.5, 0.5)
            ex, ey = handles[1] if len(handles) > 1 else (0.5, 1.0)
            wx, wy = handles[2] if len(handles) > 2 else (1.0, 0.5)
            matrix = ((wx - cx) * width, (wy - cy) * height, (ex - cx) * width, (ey - cy) * height,
                      cx * width, cy * height)
            self.defs.append(f'<radialGradient id="{ident}" gradientUnits="userSpaceOnUse" cx="0" cy="0" r="1" '
                             f'gradientTransform="matrix({" ".join(_n(value) for value in matrix)})">{stops}</radialGradient>')
        else:
            sx, sy = handles[0] if handles else (0.5, 0.0)
            ex, ey = handles[1] if len(handles) > 1 else (0.5, 1.0)
            self.defs.append(f'<linearGradient id="{ident}" gradientUnits="userSpaceOnUse" x1="{_n(sx * width)}" '
                             f'y1="{_n(sy * height)}" x2="{_n(ex * width)}" y2="{_n(ey * height)}">{stops}</linearGradient>')
        return ident

    def _data_uri(self, asset_id: str | None) -> tuple[str, float, float] | None:
        asset = next((value for value in self.document.assets if value.id == asset_id), None)
        if asset is None:
            return None
        data = asset_bytes(self.document.embedded_assets, asset)
        if not data:
            return None
        width, height = float(asset.width or 0), float(asset.height or 0)
        if not width or not height:
            try:
                import gi
                gi.require_version("GdkPixbuf", "2.0")
                from gi.repository import GdkPixbuf
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
                width, height = float(pixbuf.get_width()), float(pixbuf.get_height())
            except Exception:
                width, height = 1.0, 1.0
        media = asset.media_type or "image/png"
        return f"data:{media};base64,{base64.b64encode(data).decode('ascii')}", width, height

    def image_layer(self, item: LayoutObject) -> str:
        found = self._data_uri(item.asset_id)
        if found is None:
            return self.raster_layer(item)
        uri = found[0]
        return (f'<image x="0" y="0" width="{_n(item.width)}" height="{_n(item.height)}" '
                f'preserveAspectRatio="none" href="{uri}"/>')

    def image_paint(self, item: LayoutObject, paint: dict) -> str:
        found = self._data_uri(paint.get("asset_id"))
        if found is None:
            return self.raster_paint(item, paint)
        uri, image_w, image_h = found
        opacity = float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0)
        extra = {"opacity": _n(opacity) if opacity < 0.9995 else None}
        width, height = float(item.width), float(item.height)
        mode = paint.get("scale_mode", "fill")
        if mode == "tile":
            scale = float(paint.get("scale", 1.0) or 1.0)
            ident = self.new_id("tile")
            self.defs.append(f'<pattern id="{ident}" patternUnits="userSpaceOnUse" width="{_n(image_w * scale)}" '
                             f'height="{_n(image_h * scale)}"><image width="{_n(image_w * scale)}" '
                             f'height="{_n(image_h * scale)}" preserveAspectRatio="none" href="{uri}"/></pattern>')
            return f'<rect width="{_n(width)}" height="{_n(height)}" fill="url(#{ident})"{_attrs(extra)}/>'
        if mode == "crop":
            values = paint.get("transform") or [1, 0, 0, 1, 0, 0]
            from .scene import invert_affine
            to_unit = invert_affine(*[float(value) for value in values]) if len(values) == 6 else None
            if to_unit is None:
                return ""
            a, b, c, d, e, f = to_unit
            matrix = (a * width / image_w, b * height / image_w, c * width / image_h, d * height / image_h,
                      e * width, f * height)
            return (f'<image width="{_n(image_w)}" height="{_n(image_h)}" preserveAspectRatio="none" href="{uri}"'
                    f'{_attrs({"transform": "matrix(" + " ".join(_n(v) for v in matrix) + ")", **extra})}/>')
        turns = int(round(float(paint.get("rotation", 0.0) or 0.0) / 90.0)) % 4
        turned_w, turned_h = (image_h, image_w) if turns % 2 else (image_w, image_h)
        ratio = (min(width / turned_w, height / turned_h) if mode == "fit"
                 else max(width / turned_w, height / turned_h))
        drawn_w, drawn_h = image_w * ratio, image_h * ratio
        transform = f"translate({_n(width / 2)} {_n(height / 2)})" + (f" rotate({90 * turns})" if turns else "")
        return (f'<image x="{_n(-drawn_w / 2)}" y="{_n(-drawn_h / 2)}" width="{_n(drawn_w)}" height="{_n(drawn_h)}" '
                f'preserveAspectRatio="none" href="{uri}"{_attrs({"transform": transform, **extra})}/>')

    def _render_png(self, draw, left: float, top: float, width: float, height: float, scale: float = 2.0) -> str:
        """Pixels the scene renderer draws, as an embedded PNG covering the area."""
        import io
        import cairo
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        snapshot = Gtk.Snapshot()
        draw(snapshot)
        node = snapshot.to_node()
        pixels_w, pixels_h = max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixels_w, pixels_h)
        context = cairo.Context(surface)
        context.scale(scale, scale)
        context.translate(-left, -top)
        if node is not None:
            node.draw(context)
        buffer = io.BytesIO()
        surface.write_to_png(buffer)
        uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        return (f'<image x="{_n(left)}" y="{_n(top)}" width="{_n(width)}" height="{_n(height)}" '
                f'preserveAspectRatio="none" href="{uri}"/>')

    def _scene(self):
        from .scene import SceneRenderer
        renderer = SceneRenderer(None)
        renderer._document = self.document
        return renderer

    def raster_paint(self, item: LayoutObject, paint: dict) -> str:
        try:
            renderer = self._scene()
        except (ImportError, ValueError):    # no GTK here: a neutral placeholder
            return f'<rect width="{_n(item.width)}" height="{_n(item.height)}" fill="#808080" fill-opacity="0.25"/>'
        bounds = (0.0, 0.0, max(float(item.width), 1.0), max(float(item.height), 1.0))
        return self._render_png(lambda snapshot: renderer._paint_layer(snapshot, item, paint, bounds), *bounds)

    def raster_layer(self, item: LayoutObject) -> str:
        try:
            renderer = self._scene()
        except (ImportError, ValueError):
            return f'<rect width="{_n(item.width)}" height="{_n(item.height)}" fill="#808080" fill-opacity="0.25"/>'
        node = renderer._build(item, "body")

        def draw(snapshot):
            if node is not None:
                snapshot.append_node(node)
        return self._render_png(draw, 0.0, 0.0, max(float(item.width), 1.0), max(float(item.height), 1.0))

    # ── strokes ──────────────────────────────────────────────────────────

    def strokes(self, item: LayoutObject, outline: str | None = None) -> str:
        """The layer's strokes, bottom to top; `outline` strokes that path
        instead of the layer's own (a text layer's glyph outlines)."""
        width = float(item.stroke_width or 0)
        paints = [paint for paint in paint_stack(item, "stroke") if paint.get("visible", True) is not False]
        if width <= 0 or not paints:
            return ""
        if outline is not None:
            shapes, closed, d = [], True, outline
        else:
            shapes = geometry.outline(item)
            if not shapes:
                return ""
            closed = all(shape.closed for shape in shapes)
            d = path_data(shapes)
        align = item.stroke_align if closed else "center"
        sides = (item.metadata or {}).get("stroke_sides")
        out: list[str] = []
        for paint in paints:
            if isinstance(sides, dict) and closed and item.kind not in {"ellipse", "polygon", "star", "path", "line"}:
                out.append(self.side_strokes(item, paint, sides, align, d))
                continue
            drawn = width * 2 if align in {"inside", "outside"} else width
            attrs = {"fill": "none", "stroke-width": _n(drawn),
                     "stroke-linecap": {"round": "round", "square": "square"}.get(item.stroke_cap, "butt"),
                     "stroke-linejoin": item.stroke_join if item.stroke_join in {"round", "bevel"} else "miter"}
            dash = [float(value) for value in item.stroke_dash or [] if float(value) >= 0]
            if dash and sum(dash) > 0:
                attrs["stroke-dasharray"] = " ".join(_n(value) for value in dash)
            blend = paint.get("blend")
            if blend in BLEND_CSS:
                attrs["style"] = f"mix-blend-mode:{blend}"
            if paint.get("type", "solid") in {"image", "angular", "diamond"}:
                # No SVG paint server draws these: the stroke band becomes a
                # mask and the paint is drawn through it, as the canvas does.
                element = self._painted_band(item, paint, d, attrs)
            else:
                attrs.update(self._stroke_paint(item, paint))
                element = f'<path d="{d}"{_attrs(attrs)}/>'
            if align == "inside":
                ident = self.new_id("clip")
                rule = "evenodd" if geometry.fill_rule(item) == "evenodd" else None
                self.defs.append(f'<clipPath id="{ident}"><path d="{d}"{_attrs({"clip-rule": rule})}/></clipPath>')
                element = f'<g clip-path="url(#{ident})">{element}</g>'
            elif align == "outside":
                ident = self.new_id("mask")
                reach = drawn + 4
                box = (-reach, -reach, item.width + reach * 2, item.height + reach * 2)
                rule = "evenodd" if geometry.fill_rule(item) == "evenodd" else None
                self.defs.append(f'<mask id="{ident}" maskUnits="userSpaceOnUse" x="{_n(box[0])}" y="{_n(box[1])}" '
                                 f'width="{_n(box[2])}" height="{_n(box[3])}">'
                                 f'<rect x="{_n(box[0])}" y="{_n(box[1])}" width="{_n(box[2])}" height="{_n(box[3])}" fill="#fff"/>'
                                 f'<path d="{d}" fill="#000"{_attrs({"fill-rule": rule})}/></mask>')
                element = f'<g mask="url(#{ident})">{element}</g>'
            out.append(element)
            if not closed:
                out.append(self.line_ends(item, shapes, width, paint))
        return "".join(out)

    def _painted_band(self, item: LayoutObject, paint: dict, d: str, attrs: dict[str, str]) -> str:
        """An image or four-cornered gradient along a stroke: the stroke band
        as a mask, with the paint drawn through it."""
        ident = self.new_id("mask")
        reach = float(attrs.get("stroke-width", 0) or 0) + 8
        box = (-reach, -reach, item.width + reach * 2, item.height + reach * 2)
        band = dict(attrs, stroke="#ffffff")
        band.pop("style", None)
        self.defs.append(f'<mask id="{ident}" maskUnits="userSpaceOnUse" x="{_n(box[0])}" y="{_n(box[1])}" '
                         f'width="{_n(box[2])}" height="{_n(box[3])}">'
                         f'<path d="{d}"{_attrs(band)}/></mask>')
        inside = (self.image_paint(item, paint) if paint.get("type") == "image"
                  else self.raster_paint(item, paint))
        style = {"style": attrs["style"]} if "style" in attrs else {}
        return f'<g mask="url(#{ident})"{_attrs(style)}>{inside}</g>'

    def _stroke_paint(self, item: LayoutObject, paint: dict) -> dict[str, str]:
        kind = paint.get("type", "solid")
        opacity = float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0)
        if kind in {"linear", "radial"}:
            return {"stroke": f"url(#{self.gradient(item, paint)})"}
        from .paint import paint_color
        color = paint_color(paint) if kind != "image" else Color(0.5, 0.5, 0.5, 1)
        return _color_attrs(color or Color(0, 0, 0, 1), opacity, "stroke")

    def side_strokes(self, item: LayoutObject, paint: dict, sides: dict, align: str, d: str) -> str:
        def weight(name: str) -> float:
            try:
                return max(0.0, float(sides.get(name, 0.0) or 0.0))
            except (TypeError, ValueError):
                return 0.0
        top, right, bottom, left = weight("top"), weight("right"), weight("bottom"), weight("left")
        outward = {"inside": 0.0, "center": 0.5, "outside": 1.0}.get(align, 0.0)
        x0, x1 = -left * outward, item.width + right * outward
        y0, y1 = -top * outward, item.height + bottom * outward
        bands = ((x0, y0, x1 - x0, top), (x0, y1 - bottom, x1 - x0, bottom),
                 (x0, y0, left, y1 - y0), (x1 - right, y0, right, y1 - y0))
        data = "".join(f"M{_n(bx)} {_n(by)}h{_n(bw)}v{_n(bh)}h{_n(-bw)}Z" for bx, by, bw, bh in bands if bw > 0 and bh > 0)
        if not data:
            return ""
        paint_attrs = self._stroke_paint(item, paint)
        fill = {("fill" + key[6:]): value for key, value in paint_attrs.items()}
        element = f'<path d="{data}"{_attrs(fill)}/>'
        if align == "inside" and any(geometry.corner_radii_of(item)):
            ident = self.new_id("clip")
            self.defs.append(f'<clipPath id="{ident}"><path d="{d}"/></clipPath>')
            element = f'<g clip-path="url(#{ident})">{element}</g>'
        return element

    def line_ends(self, item: LayoutObject, shapes: list[geometry.Subpath], width: float, paint: dict) -> str:
        out: list[str] = []
        attrs = self._stroke_paint(item, paint)
        fill = {("fill" + key[6:]): value for key, value in attrs.items()}
        for kind, points in geometry.line_end_shapes(item, shapes, width):
            if kind == "stroke":
                data = "M" + "L".join(f"{_n(x)} {_n(y)}" for x, y in points)
                out.append(f'<path d="{data}" fill="none" stroke-width="{_n(width)}" stroke-linecap="round" '
                           f'stroke-linejoin="round"{_attrs(attrs)}/>')
            elif kind == "circle":
                (cx, cy), radius = points
                out.append(f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(radius)}"{_attrs(fill)}/>')
            else:
                data = "M" + "L".join(f"{_n(x)} {_n(y)}" for x, y in points) + "Z"
                out.append(f'<path d="{data}"{_attrs(fill)}/>')
        return "".join(out)

    # ── text ─────────────────────────────────────────────────────────────

    def _layout(self, item: LayoutObject):
        import gi
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        from .typography import build_layout
        if self._pango is None:
            self._pango = PangoCairo.FontMap.get_default().create_context()
            self._pango.set_round_glyph_positions(False)
        return build_layout(self._pango, item)

    def text(self, item: LayoutObject) -> str:
        try:
            from .typography import type_settings
            text = self._layout(item)
        except (ImportError, ValueError):   # no Pango here: an estimated layout
            return self._estimated_text(item)
        paints = [paint for paint in paint_stack(item, "fill") if paint.get("visible", True) is not False]
        if not paints:
            paints = [{"type": "solid", "color": {"r": 0, "g": 0, "b": 0, "a": 1}, "opacity": 1.0}]
        clip = type_settings(item.metadata)["sizing"] == "fixed"
        body = self._text_outline(item, text, paints) if self.outline_text else self._text_spans(item, text, paints)
        if clip and body:
            ident = self.new_id("clip")
            self.defs.append(f'<clipPath id="{ident}"><rect width="{_n(max(item.width, 1))}" '
                             f'height="{_n(max(item.height, 1))}"/></clipPath>')
            body = f'<g clip-path="url(#{ident})">{body}</g>'
        return body

    def _estimated_text(self, item: LayoutObject) -> str:
        import textwrap
        size = float((item.metadata or {}).get("font_size", 16) or 16)
        per_line = max(1, round(item.width / max(1.0, size * 0.56)))
        lines = [line for paragraph in (item.text or "").splitlines() or [""]
                 for line in (textwrap.wrap(paragraph, width=per_line) or [""])]
        color = color_of(item.fill, Color(0, 0, 0, 1))
        spans = "".join(f'<tspan x="0" y="{_n(size * (0.9 + 1.2 * index))}">{escape(line)}</tspan>'
                        for index, line in enumerate(lines))
        family = (item.metadata or {}).get("font_family", "Figtree")
        return f'<text{_attrs({"font-family": family, "font-size": _n(size), **_color_attrs(color, 1.0, "fill")})}>{spans}</text>'

    def text_outline_data(self, item: LayoutObject) -> str:
        """A text layer's glyph outlines as path data, or "" without Pango."""
        try:
            text = self._layout(item)
        except (ImportError, ValueError):
            return ""
        return self._glyph_path(text)

    @staticmethod
    def _glyph_path(text) -> str:
        import cairo
        import gi
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        surface = cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None)
        context = cairo.Context(surface)
        context.translate(0, text.offset_y)
        PangoCairo.layout_path(context, text.layout)
        parts: list[str] = []
        for kind, points in context.copy_path():
            if kind == cairo.PATH_MOVE_TO:
                parts.append(f"M{_n(points[0])} {_n(points[1])}")
            elif kind == cairo.PATH_LINE_TO:
                parts.append(f"L{_n(points[0])} {_n(points[1])}")
            elif kind == cairo.PATH_CURVE_TO:
                parts.append("C" + " ".join(_n(value) for value in points))
            elif kind == cairo.PATH_CLOSE_PATH:
                parts.append("Z")
        return "".join(parts)

    def _text_outline(self, item: LayoutObject, text, paints: list[dict]) -> str:
        d = self._glyph_path(text)
        if not d:
            return ""
        runs = self._colored_runs(item, text)
        if runs:
            return self._text_runs_outline(item, text, runs, paints)
        return "".join(self.paint_shape(item, paint, d, None) for paint in paints)

    def _colored_runs(self, item: LayoutObject, text) -> list[tuple[int, int, Color]]:
        ranges = (item.metadata or {}).get("ranges") or []
        result = []
        for run in ranges:
            if isinstance(run, dict) and "fill" in run:
                color = color_of(run.get("fill"))
                if color is not None:
                    start = text.byte_index(int(run.get("start", 0)))
                    end = text.byte_index(int(run.get("end", 0)))
                    if end > start:
                        result.append((start, end, color))
        return result

    def _text_runs_outline(self, item: LayoutObject, text, runs, paints: list[dict]) -> str:
        """Text with per-range colors: glyph outlines per run, colored by the range covering it."""
        import cairo
        import gi
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
        out: list[str] = []
        iterator = text.layout.get_iter()
        while True:
            run = iterator.get_run_readonly()
            if run is not None:
                baseline = iterator.get_baseline() / Pango.SCALE
                extents = iterator.get_run_extents()[1]
                surface = cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None)
                context = cairo.Context(surface)
                context.translate(extents.x / Pango.SCALE, text.offset_y + baseline)
                PangoCairo.glyph_string_path(context, run.item.analysis.font, run.glyphs)
                parts: list[str] = []
                for kind, points in context.copy_path():
                    if kind == cairo.PATH_MOVE_TO:
                        parts.append(f"M{_n(points[0])} {_n(points[1])}")
                    elif kind == cairo.PATH_LINE_TO:
                        parts.append(f"L{_n(points[0])} {_n(points[1])}")
                    elif kind == cairo.PATH_CURVE_TO:
                        parts.append("C" + " ".join(_n(value) for value in points))
                    elif kind == cairo.PATH_CLOSE_PATH:
                        parts.append("Z")
                d = "".join(parts)
                if d:
                    offset = run.item.offset
                    color = next((value for start, end, value in runs if start <= offset < end), None)
                    if color is not None:
                        out.append(f'<path d="{d}"{_attrs(_color_attrs(color, 1.0, "fill"))}/>')
                    else:
                        out.extend(self.paint_shape(item, paint, d, None) for paint in paints)
            if not iterator.next_run():
                break
        return "".join(out)

    def _text_spans(self, item: LayoutObject, text, paints: list[dict]) -> str:
        from gi.repository import Pango
        from .typography import type_settings
        settings = type_settings(item.metadata)
        display = text.display.encode("utf-8")
        spans: list[str] = []
        iterator = text.layout.get_iter()
        while True:
            line = iterator.get_line_readonly()
            baseline = iterator.get_baseline() / Pango.SCALE
            extents = iterator.get_line_extents()[1]
            start = line.start_index
            chunk = display[start:start + line.length].decode("utf-8", errors="ignore").rstrip("\n")
            spans.append(f'<tspan x="{_n(extents.x / Pango.SCALE)}" y="{_n(text.offset_y + baseline)}">{escape(chunk)}</tspan>')
            if not iterator.next_line():
                break
        paint = paints[-1]
        fill = self._stroke_paint(item, paint)
        fill = {("fill" + key[6:]): value for key, value in fill.items()}
        weight = int(float(settings["font_weight"] or 400))
        attrs = {"font-family": settings["font_family"], "font-size": _n(float(settings["font_size"] or 16)),
                 "font-weight": weight, "font-style": "italic" if settings["italic"] else None,
                 "xml:space": "preserve", **fill}
        return f"<text{_attrs(attrs)}>{''.join(spans)}</text>"


def svg_document(document: Document, objects: list[LayoutObject], roots: list[LayoutObject],
                 bounds: tuple[float, float, float, float], *, outline_text: bool = True) -> str:
    return SvgWriter(document, objects, outline_text=outline_text).write(bounds, roots)


def attribute_list(values: Iterable[tuple[str, str]]) -> str:
    return _attrs(dict(values))
