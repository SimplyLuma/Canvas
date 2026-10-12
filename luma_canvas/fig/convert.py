# SPDX-License-Identifier: MPL-2.0
"""A decoded Figma document as a Canvas document.

Pages become pages, frames frames, groups groups; shapes keep their kind when
Canvas draws them identically and become vectors when it would not; text
keeps its per-range styles; instances are resolved to their component's
content with overrides, component properties and Figma's own derived layout
applied. Canvas stores absolute coordinates, so every node's transform is
composed with its ancestors'.

Anything carried differently or left out is noted in the `ImportReport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import math
import os
from typing import Any, Callable, Iterable

from ..model import Asset, Document, LayoutObject, Page, migrate_group_paint
from ..paint import set_paint_stack
from . import images, paths
from .document import FigDocument, Guid, guid_of, guid_text
from .paints import PaintConverter, effects as convert_effects, hex_color, number, visible_paints
from .paths import Affine, IDENTITY, Subpath
from .report import ImportReport
from .text import text_fields

MAX_INSTANCE_DEPTH = 48
FRAME_TYPES = {"FRAME", "SYMBOL", "INSTANCE", "SECTION", "SLIDE", "SLIDE_GRID", "SLIDE_ROW",
               "RESPONSIVE_SET", "WEBPAGE", "MODULE", "ASSISTED_LAYOUT", "TABLE", "TABLE_CELL"}
FIGJAM_TYPES = {"STICKY", "SHAPE_WITH_TEXT", "CONNECTOR", "CODE_BLOCK", "STAMP", "WIDGET", "HIGHLIGHT",
                "WASHI_TAPE", "MEDIA", "SECTION_OVERLAY", "TEXT_PATH", "BRUSH", "EMBEDDED_PROTOTYPE",
                "CODE_LAYER", "CODE_INSTANCE", "CODE_EMBED", "JSX", "REPEATER"}
IGNORED_TYPES = {"DOCUMENT", "VARIABLE", "VARIABLE_SET", "VARIABLE_OVERRIDE", "CODE_COMPONENT",
                 "CODE_LIBRARY", "CODE_FILE", "MANAGED_STRING", "KEYFRAME", "KEYFRAME_TRACK", "TRANSFORM",
                 "BINARY_FILE", "CMS_RICH_TEXT", "ANIMATION_PRESET_INSTANCE", "SPEC_BLOCK", "NONE"}
CAPS = {"NONE": "butt", "ROUND": "round", "SQUARE": "square"}
END_CAPS = {"ARROW_LINES": "line_arrow", "ARROW_EQUILATERAL": "triangle_arrow",
            "TRIANGLE_FILLED": "reverse_triangle", "CIRCLE_FILLED": "circle", "DIAMOND_FILLED": "diamond"}
JOINS = {"MITER": "miter", "ROUND": "round", "BEVEL": "bevel"}
ALIGNS = {"INSIDE": "inside", "CENTER": "center", "OUTSIDE": "outside"}
H_CONSTRAINTS = {"MIN": "left", "CENTER": "centre", "MAX": "right", "STRETCH": "left-right", "SCALE": "scale",
                 "FIXED_MIN": "left", "FIXED_MAX": "right"}
V_CONSTRAINTS = {"MIN": "top", "CENTER": "centre", "MAX": "bottom", "STRETCH": "top-bottom", "SCALE": "scale",
                 "FIXED_MIN": "top", "FIXED_MAX": "bottom"}
FULL_TURN = 2 * math.pi


class IdMaker:
    """Layer ids like `model.new_id`, from one random prefix and a counter.

    `uuid4` per layer reads the kernel's random source each time, and each
    of those calls hands the interpreter lock back and forth with the main
    thread, which stalled the window for half a second at a time during a
    large import. One read per import keeps ids unique across imports.
    """

    def __init__(self) -> None:
        self.prefix = os.urandom(4).hex()
        self.counter = itertools.count()

    def __call__(self, kind: str) -> str:
        return f"{kind}-{self.prefix}{next(self.counter):06x}"


@dataclass
class Scope:
    """An instance being expanded: what its content is overridden with."""

    instance: dict[str, Any]
    master: dict[str, Any]
    overrides: dict[tuple, dict[str, Any]]
    derived: dict[tuple, dict[str, Any]]
    assignments: dict[Guid, dict[str, Any]]
    uid: str
    depth: int
    trail: tuple[Guid, ...] = field(default_factory=tuple)


def _path_key(node: dict[str, Any]) -> Guid:
    return guid_of(node.get("overrideKey")) or node["_guid"]


def _path_of(entry: dict[str, Any]) -> tuple | None:
    path = entry.get("guidPath")
    guids = path.get("guids") if isinstance(path, dict) else None
    if not isinstance(guids, list) or not guids:
        return None
    keys = tuple(guid_of(value) for value in guids)
    return None if any(key is None for key in keys) else keys


def _merge_entries(target: dict[tuple, dict[str, Any]], entries: Iterable[Any], prefix: tuple = (),
                   *, precedence: bool) -> None:
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        path = _path_of(entry)
        if path is None:
            continue
        key = prefix + path
        values = {name: value for name, value in entry.items() if name not in {"guidPath", "guid", "parentIndex"}}
        existing = target.get(key)
        if existing is None:
            target[key] = values
        elif precedence:
            existing.update(values)
        else:
            for name, value in values.items():
                existing.setdefault(name, value)


class Converter:
    def __init__(self, fig: FigDocument, report: ImportReport, *, name: str,
                 check: Callable[[], None] | None = None,
                 progress: Callable[[float], None] | None = None) -> None:
        self.fig = fig
        self.new_id = IdMaker()
        self.report = report
        self.name = name
        self.check = check
        self.progress = progress
        self.assets: dict[str, Asset] = {}
        self.embedded: dict[str, bytes] = {}
        self._image_cache: dict[str, str | None] = {}
        self.paints = PaintConverter(report, self._image_asset)
        self.visited = 0
        self.total = max(1, len(fig.nodes))
        self.objects: list[LayoutObject] = []
        self.prototype_sources = []
        self.prototype_starts = []
        self.stats = {"layers": 0, "pages": 0, "instances": 0, "images": 0, "text": 0, "vectors": 0}

    # ── the document ─────────────────────────────────────────────────────

    def run(self) -> Document:
        document = Document.empty(self.name)
        root = self.fig.root
        canvases = [node for node in (self.fig.kids(root) if root else []) if node.get("type") == "CANVAS"]
        if root is None:
            canvases = [node for node in self.fig.nodes.values() if node.get("type") == "CANVAS"]
        if self.fig.editor not in {"design"}:
            self.report.note("editor", extra=self.fig.editor)
        pages: list[Page] = []
        for canvas in canvases:
            if guid_of(canvas.get("prototypeStartNodeID")):
                self.prototype_starts.append(guid_text(guid_of(canvas["prototypeStartNodeID"])))
            if canvas.get("internalOnly"):
                self.report.note("hidden_pages")
                continue
            self.objects = []
            for child in self._children(canvas):
                self._convert(child, IDENTITY, None)
            migrate_group_paint_with_note(self.objects, self.report)
            finish_booleans(self.objects)
            page = Page(self.new_id("page"), str(canvas.get("name") or f"Page {len(pages) + 1}"))
            background = canvas.get("backgroundColor")
            if isinstance(background, dict) and canvas.get("backgroundEnabled", True) is not False:
                page.background = hex_color(background, number(canvas.get("backgroundOpacity"), 1.0))
            page.selected_ids = []
            pages.append(page)
            page.objects = self.objects  # type: ignore[assignment] (live objects until finished)
        if not pages:
            pages.append(Page(self.new_id("page"), "Page 1"))
            pages[0].objects = []  # type: ignore[assignment]
        from .components import link_instances
        link_instances(self,pages)
        self.stats["pages"] = len(pages)
        first = pages[0]
        document.objects = list(first.objects)  # type: ignore[arg-type]
        first.objects = []
        from dataclasses import asdict
        for page in pages[1:]:
            page.objects = [asdict(item) for item in page.objects]  # type: ignore[union-attr]
        document.pages = pages
        document.current_page_id = first.id
        document.assets = list(self.assets.values())
        document.embedded_assets = dict(self.embedded)
        document.select([])
        from .prototypes import import_prototypes
        from .prototype_variables import import_variables
        names=import_variables(document,self.fig,self.report)
        import_prototypes(document, self.prototype_sources, self.prototype_starts, self.report,names)
        self.report.stats.update(self.stats)
        return document

    def _tick(self) -> None:
        self.visited += 1
        if not self.visited & 127:
            if self.check:
                self.check()
            if self.progress:
                self.progress(min(1.0, self.visited / self.total))

    # ── children and instances ───────────────────────────────────────────

    def _children(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        if node.get("type") == "INSTANCE":
            return self._instance_children(node)
        scope: Scope | None = node.get("_scope")
        source = node.get("_source", node)
        kids = self.fig.children.get(source["_guid"], [])
        if scope is None:
            return kids
        prefix = node.get("_path", ())
        return [self._materialize(child, scope, prefix) for child in kids]

    def _instance_children(self, instance: dict[str, Any]) -> list[dict[str, Any]]:
        self.stats["instances"] += 1
        parent_scope: Scope | None = instance.get("_scope")
        depth = parent_scope.depth + 1 if parent_scope else 1
        layer = str(instance.get("name") or "Instance")
        symbol = instance.get("symbolData") if isinstance(instance.get("symbolData"), dict) else {}
        symbol_id = guid_of(instance.get("overriddenSymbolID")) or guid_of(symbol.get("symbolID"))
        master = self.fig.node(symbol_id)
        if master is None:
            self.report.note("components_missing", layer)
            return []
        trail = (parent_scope.trail if parent_scope else ()) + (master["_guid"],)
        if depth > MAX_INSTANCE_DEPTH or master["_guid"] in trail[:-1]:
            self.report.note("instance_depth", layer)
            return []

        overrides: dict[tuple, dict[str, Any]] = {}
        derived: dict[tuple, dict[str, Any]] = {}
        # The outer instance's overrides for this instance's content win over
        # this instance's own.
        if parent_scope is not None:
            prefix = instance.get("_path", ())
            for store, source in ((overrides, parent_scope.overrides), (derived, parent_scope.derived)):
                for key, values in source.items():
                    if len(key) > len(prefix) and key[:len(prefix)] == prefix:
                        store[key[len(prefix):]] = dict(values)
        _merge_entries(overrides, symbol.get("symbolOverrides"), precedence=False)
        _merge_entries(derived, instance.get("derivedSymbolData"), precedence=False)

        assignments: dict[Guid, dict[str, Any]] = {}
        for assignment in instance.get("componentPropAssignments") or []:
            if isinstance(assignment, dict):
                definition = guid_of(assignment.get("defID"))
                if definition is not None and isinstance(assignment.get("value"), dict):
                    assignments[definition] = assignment["value"]

        uid = f"{instance.get('_uid', guid_text(instance['_guid']))}"
        scope = Scope(instance, master, overrides, derived, assignments, uid, depth, trail)
        # A master's own root override (its name, mostly) is already on the instance.
        return [self._materialize(child, scope, ()) for child in self.fig.kids(master)]

    def _materialize(self, node: dict[str, Any], scope: Scope, prefix: tuple) -> dict[str, Any]:
        key = _path_key(node)
        path = prefix + (key,)
        props = dict(node)
        props["_source"] = node
        props["_path"] = path
        props["_scope"] = scope
        props["_uid"] = f"{scope.uid}/{guid_text(node['_guid'])}"
        override = scope.overrides.get(path)
        swapped = guid_of((override or {}).get("overriddenSymbolID"))
        if swapped is not None and node.get("type") == "INSTANCE":
            # A swapped instance looks like its new component unless the
            # swap itself says otherwise.
            self._adopt_master_appearance(props, swapped)
        if override:
            inherited=props.get('prototypeInteractions')
            props.update(override)
            if isinstance(inherited,list) and isinstance(override.get('prototypeInteractions'),list):
                # Figma overrides are keyed patches, including tombstones for old
                # IDs. A tombstone must not erase unrelated newer master actions.
                patched={guid_of(entry.get('id')) or ('master',index):entry for index,entry in enumerate(inherited) if isinstance(entry,dict)}
                for index,entry in enumerate(override['prototypeInteractions']):
                    if isinstance(entry,dict):patched[guid_of(entry.get('id')) or ('override',index)]=entry
                props['prototypeInteractions']=list(patched.values())
        for reference in node.get("componentPropRefs") or []:
            if not isinstance(reference, dict) or reference.get("isDeleted"):
                continue
            value = scope.assignments.get(guid_of(reference.get("defID")))
            if value is None:
                continue
            field_name = reference.get("componentPropNodeField")
            if field_name == "TEXT_DATA" and isinstance(value.get("textValue"), dict):
                text = dict(props.get("textData") or {})
                text.update(value["textValue"])
                props["textData"] = text
            elif field_name == "VISIBLE" and "boolValue" in value:
                props["visible"] = bool(value["boolValue"])
            elif field_name == "OVERRIDDEN_SYMBOL_ID" and guid_of(value.get("guidValue")):
                if node.get("type") == "INSTANCE" and not override:
                    self._adopt_master_appearance(props, guid_of(value["guidValue"]))
                props["overriddenSymbolID"] = value["guidValue"]
        derived = scope.derived.get(path)
        if derived:
            props.update(derived)
        return props

    APPEARANCE_KEYS = ("size", "fillPaints", "strokePaints", "strokeWeight", "strokeAlign", "strokeJoin",
                       "dashPattern", "effects", "cornerRadius", "rectangleCornerRadiiIndependent",
                       "rectangleTopLeftCornerRadius", "rectangleTopRightCornerRadius",
                       "rectangleBottomLeftCornerRadius", "rectangleBottomRightCornerRadius", "cornerSmoothing",
                       "frameMaskDisabled", "opacity", "blendMode", "stackMode", "borderStrokeWeightsIndependent",
                       "borderTopWeight", "borderRightWeight", "borderBottomWeight", "borderLeftWeight")

    def _adopt_master_appearance(self, props: dict[str, Any], symbol: Guid | None) -> None:
        master = self.fig.node(symbol)
        if master is None:
            return
        for key in self.APPEARANCE_KEYS:
            if key in master:
                props[key] = master[key]
            else:
                props.pop(key, None)

    # ── one node ─────────────────────────────────────────────────────────

    def _convert(self, props: dict[str, Any], parent_world: Affine, parent_id: str | None) -> LayoutObject | None:
        self._tick()
        kind = props.get("type")
        layer = str(props.get("name") or (kind or "Layer").title())
        if kind in IGNORED_TYPES or kind is None:
            return None
        if kind == "CANVAS":
            return None
        world = parent_world @ Affine.from_figma(props.get("transform"))
        self._notes_for(props, layer)
        try:
            if kind == "GROUP" or (kind == "FRAME" and props.get("resizeToFit") and not self._container_paint(props)):
                return self._group(props, world, parent_id, layer)
            if kind in FRAME_TYPES:
                return self._frame(props, world, parent_id, layer)
            if kind in {"RECTANGLE", "ROUNDED_RECTANGLE"}:
                return self._rectangle(props, world, parent_id, layer)
            if kind == "ELLIPSE":
                return self._ellipse(props, world, parent_id, layer)
            if kind in {"REGULAR_POLYGON", "STAR"}:
                return self._polygon(props, world, parent_id, layer)
            if kind == "LINE":
                return self._line(props, world, parent_id, layer)
            if kind == "VECTOR":
                return self._vector(props, world, parent_id, layer)
            if kind == "BOOLEAN_OPERATION":
                return self._boolean(props, world, parent_id, layer)
            if kind == "TEXT":
                return self._text(props, world, parent_id, layer)
            if kind == "SLICE":
                item = self._object("slice", props, world, parent_id, layer)
                item.fill = None
                self._add(item)
                return item
            if kind in FIGJAM_TYPES:
                self.report.note("figjam", layer, extra=kind.replace("_", " ").lower())
                return self._fallback(props, world, parent_id, layer)
        except paths.GeometryError:
            self.report.note("geometry", layer)
            return None
        self.report.note("layer_types", layer, extra=str(kind).replace("_", " ").lower())
        return self._fallback(props, world, parent_id, layer, quiet=True)

    def _notes_for(self, props: dict[str, Any], layer: str) -> None:
        if props.get("stackMode")=="GRID" or props.get('stackWrap') in {'WRAP',True}:
            self.report.note("auto_layout", layer, extra="Grid or wrapping auto layout is not supported yet")
        if props.get("layoutGrids"):
            from .layouts import layout_grids
            if len(layout_grids(props))!=len(props['layoutGrids']):self.report.note("layout_grids",layer,extra="Unsupported grid rule")
        if props.get("variableConsumptionMap"):
            self.report.note("variables", layer)
        if props.get("styleIdForFill") or props.get("styleIdForText") or guid_of(props.get("inheritFillStyleID")):
            self.report.note("styles", layer)

    @staticmethod
    def _container_paint(props: dict[str, Any]) -> bool:
        return bool(visible_paints(props.get("fillPaints")) or visible_paints(props.get("strokePaints")))

    # ── placement ────────────────────────────────────────────────────────

    def _object(self, kind: str, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str,
                size: tuple[float, float] | None = None) -> LayoutObject:
        width, height = size if size is not None else self._size(props)
        item = LayoutObject(self.new_id(kind), kind, layer, parent_id,
                            width=width, height=height, fill=None)
        self._place(item, world, width, height, layer)
        item.visible = props.get("visible", True) is not False
        item.locked = bool(props.get("locked", False))
        item.opacity = round(max(0.0, min(1.0, number(props.get("opacity"), 1.0))), 4)
        blend = self.paints.blend_of(props.get("blendMode"), layer)
        blend = None if blend == "normal" else blend
        # `_place` may have recorded the layer's scale and lean: keep those.
        metadata: dict[str, Any] = {**(item.metadata or {}),
                                    "figma_id": props.get("_uid") or guid_text(props["_guid"])}
        if blend:
            metadata["blend"] = blend
        horizontal = H_CONSTRAINTS.get(props.get("horizontalConstraint"))
        vertical = V_CONSTRAINTS.get(props.get("verticalConstraint"))
        if horizontal or vertical:
            item.constraints = {"horizontal": horizontal or "left", "vertical": vertical or "top"}
        settings = self._export_settings(props, layer)
        if settings:
            metadata["export_settings"] = settings
        from .prototypes import frame_settings
        metadata.update(frame_settings(props))
        if props.get('type')=='SYMBOL':metadata['prototype_variant']=True
        if props.get('type')=='INSTANCE':
            metadata['prototype_instance']=True
            master=self.fig.node(guid_of((props.get('symbolData') or {}).get('symbolID')))
            if master and not props.get('prototypeInteractions'):
                props={**props,'prototypeInteractions':master.get('prototypeInteractions',[])}
        item.grow=max(0.,number(props.get('stackChildPrimaryGrow'),0.))
        if props.get('stackChildAlignSelf')=='STRETCH':item.layout['cross_fill']=True
        if props.get('stackPositioning')=='ABSOLUTE':item.layout['positioning']='absolute'
        item.metadata = metadata
        self.prototype_sources.append((item.id, props))
        return item

    EXPORT_FORMATS = {"PNG": "png", "JPEG": "jpeg", "JPG": "jpeg", "SVG": "svg", "PDF": "pdf"}
    EXPORT_CONSTRAINTS = {"CONTENT_SCALE": "scale", "CONTENT_WIDTH": "width", "CONTENT_HEIGHT": "height"}

    def _export_settings(self, props: dict[str, Any], layer: str) -> list[dict[str, Any]]:
        """Figma's export rows as Canvas export settings (see `export.normalize_setting`)."""
        from ..export import normalize_setting
        rows = []
        for entry in props.get("exportSettings") or []:
            if not isinstance(entry, dict):
                continue
            fmt = self.EXPORT_FORMATS.get(str(entry.get("imageType") or "PNG").upper())
            if fmt is None:
                self.report.note("export_settings", layer, extra=str(entry.get("imageType")).lower())
                continue
            constraint = entry.get("constraint") if isinstance(entry.get("constraint"), dict) else {}
            row = normalize_setting({
                "format": fmt, "suffix": entry.get("suffix") or "",
                "constraint": self.EXPORT_CONSTRAINTS.get(constraint.get("type"), "scale"),
                "value": number(constraint.get("value"), 1.0),
            })
            if row is not None:
                rows.append(row)
        return rows

    @staticmethod
    def _size(props: dict[str, Any]) -> tuple[float, float]:
        size = props.get("size") if isinstance(props.get("size"), dict) else {}
        return max(0.0, number(size.get("x"))), max(0.0, number(size.get("y")))

    def _place(self, item: LayoutObject, world: Affine, width: float, height: float, layer: str) -> None:
        cx, cy = world.apply(width / 2, height / 2)
        if not world.is_rigid():
            # The transform as rotation, lean and scale of the layer's own
            # axes: exactly how `scene` and the exporters read a layer back.
            sx = math.hypot(world.a, world.b)
            sy = world.determinant / sx if sx > 1e-9 else 0.0
            if sx > 1e-9 and abs(sy) > 1e-9:
                lean = (world.a * world.c + world.b * world.d) / (sx * sy)
                if abs(lean) > 1e-3 or abs(sx - 1) > 1e-3 or abs(abs(sy) - 1) > 1e-3:
                    height_scale = abs(sy)
                    item.metadata = {**item.metadata, "figma_scale": [round(sx, 5), round(height_scale, 5)]}
                    width, height = width * sx, height * height_scale
                    item.width, item.height = width, height
                    if abs(lean) > 1e-3:
                        item.metadata = {**item.metadata, "skew": round(lean, 6)}
                    if item.kind == "text" and (abs(sx - 1) > 1e-3 or abs(height_scale - 1) > 1e-3):
                        self.report.note("text_scale", layer)
        rotation = world.rotation_degrees()
        flip = world.determinant < 0
        flip_h = False
        if flip and abs(rotation) > 90:
            rotation = rotation - 180 if rotation > 0 else rotation + 180
            flip_h, flip = True, False
        rotation = (rotation + 180.0) % 360.0 - 180.0
        if abs(rotation) < 1e-6:
            rotation = 0.0
        item.rotation = round(rotation, 6)
        item.flip_h, item.flip_v = flip_h, flip
        item.x = round(cx - width / 2, 6)
        item.y = round(cy - height / 2, 6)

    def _add(self, item: LayoutObject) -> None:
        self.objects.append(item)
        self.stats["layers"] += 1

    # ── paint helpers ────────────────────────────────────────────────────

    def _paint(self, item: LayoutObject, props: dict[str, Any], layer: str, *, fills: bool = True,
               strokes: bool = True, effects: bool = True) -> None:
        width, height = item.width, item.height
        if fills:
            converted = self.paints.fills(props, width, height, layer)
            set_paint_stack(item, "fill", converted["stack"])
        if strokes:
            self._stroke(item, props, layer)
        if effects:
            item.effects = convert_effects(props, self.report, layer)

    def _stroke(self, item: LayoutObject, props: dict[str, Any], layer: str) -> None:
        converted = self.paints.stroke(props, item.width, item.height, layer)
        set_paint_stack(item, "stroke", converted["stack"])
        weight = max(0.0, number(props.get("strokeWeight"), 1.0))
        if props.get("borderStrokeWeightsIndependent"):
            sides = {side: 0.0 if props.get(f"border{side.title()}Hidden") else
                     max(0.0, number(props.get(f"border{side.title()}Weight"), weight))
                     for side in ("top", "right", "bottom", "left")}
            if len(set(sides.values())) > 1:
                item.metadata = {**item.metadata, "stroke_sides": sides}
            weight = max(sides.values())
        if item.stroke is None and item.stroke_gradient is None:
            item.stroke_width = 0.0 if weight <= 0 else weight
            if item.stroke_width and not visible_paints(props.get("strokePaints")):
                item.stroke_width = 0.0
        else:
            item.stroke_width = round(weight, 4)
        item.stroke_align = ALIGNS.get(props.get("strokeAlign"), "center")
        item.stroke_cap = CAPS.get(props.get("strokeCap"), "butt")
        end = END_CAPS.get(props.get("strokeCap"))
        if end:
            item.start_cap = item.end_cap = end
        item.stroke_join = JOINS.get(props.get("strokeJoin"), "miter")
        dash = [round(max(0.0, number(value)), 4) for value in props.get("dashPattern") or []]
        if dash and sum(dash) > 0:
            item.stroke_dash = dash
        if number(props.get("miterLimit"), 4.0) != 4.0:
            item.metadata = {**item.metadata, "miter_limit": round(number(props.get("miterLimit"), 4.0), 4)}

    def _image_asset(self, image: dict[str, Any], layer: str, thumbnail: dict[str, Any] | None = None) -> str | None:
        """An embedded image as an asset; a damaged or missing image falls back
        to the smaller preview Figma keeps beside it."""
        asset_id = self._load_image(image)
        if asset_id is None and isinstance(thumbnail, dict) and thumbnail.get("hash") != image.get("hash"):
            asset_id = self._load_image(thumbnail)
            if asset_id is not None:
                self.report.note("image_preview", layer)
                return asset_id
        if asset_id is None:
            self.report.note("images_missing", layer)
        return asset_id

    def _load_image(self, image: dict[str, Any]) -> str | None:
        raw = image.get("hash")
        sha1 = raw.hex() if isinstance(raw, (bytes, bytearray)) else None
        cache_key = sha1 or f"blob:{image.get('dataBlob')}"
        if cache_key in self._image_cache:
            return self._image_cache[cache_key]
        data = self.fig.archive.image(sha1) if sha1 else None
        if data is None:
            data = self.fig.blob(image.get("dataBlob"))
        media_type, width, height = images.sniff(data or b"")
        if not data or media_type == "application/octet-stream":
            self._image_cache[cache_key] = None
            return None
        asset_id = f"figma-image-{sha1 or cache_key.replace(':', '-')}"
        name = str(image.get("name") or sha1 or "Image")
        # Stored under the asset id (the embedded name is the id too), so
        # every reader of `embedded_assets` finds it.
        asset = Asset(asset_id, name, media_type, embedded_name=asset_id, width=width, height=height)
        self.assets[asset_id] = asset
        self.embedded[asset_id] = data
        self._image_cache[cache_key] = asset_id
        self.stats["images"] += 1
        return asset_id

    # ── containers ───────────────────────────────────────────────────────

    def _children_into(self, props: dict[str, Any], world: Affine, parent_id: str) -> list[LayoutObject]:
        """Convert children in paint order; a mask masks the siblings above it,
        which is Canvas's own mask model, so alpha and luminance masks are exact."""
        created: list[LayoutObject] = []
        for child in self._children(props):
            masking = bool(child.get("mask"))
            item = self._convert(dict(child, mask=False) if masking else child, world, parent_id)
            if item is None:
                continue
            if masking:
                kind = str(child.get("maskType") or "ALPHA").upper()
                mode = ("luminance" if kind == "LUMINANCE" else
                        "outline" if kind in {"VECTOR", "OUTLINE"} or child.get("maskIsOutline") else "alpha")
                item.metadata = {**item.metadata, "mask": mode}
            created.append(item)
        return created

    def _frame(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject:
        kind = props.get("type")
        canvas_kind = {"SYMBOL": "component", "SECTION": "section"}.get(kind, "frame")
        item = self._object(canvas_kind, props, world, parent_id, layer)
        self._paint(item, props, layer)
        self._corners(item, props, layer)
        item.clip_content = not bool(props.get("frameMaskDisabled", False)) and kind != "SECTION"
        if kind == "INSTANCE":
            item.metadata = {**item.metadata, "figma_instance": True}
            master = self.fig.node(guid_of(props.get("overriddenSymbolID"))
                                   or guid_of((props.get("symbolData") or {}).get("symbolID")))
            if master is not None:
                item.metadata["figma_component"] = str(master.get("name") or "")
                item.metadata["figma_component_id"] = guid_text(master["_guid"])
        from .layouts import auto_layout,layout_grids
        layout_props=props
        if kind=='INSTANCE' and master is not None:
            layout_props={**master,**props}
        rule=auto_layout(layout_props)
        if rule:
            item.layout={**item.layout,**rule}
            item.metadata={**item.metadata,"figma_auto_layout":_auto_layout(layout_props)}
        grids=layout_grids(layout_props)
        if grids:item.metadata={**item.metadata,'layout_grids':grids}
        item.grow=max(0.,number(props.get('stackChildPrimaryGrow'),0.))
        if props.get('stackChildAlignSelf')=='STRETCH':item.layout['cross_fill']=True
        self._add(item)
        self._children_into(props, world, item.id)
        return item

    def _group(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject | None:
        item = self._object("group", props, world, parent_id, layer)
        item.effects = convert_effects(props, self.report, layer)
        self._add(item)
        index = len(self.objects)
        self._children_into(props, world, item.id)
        children = [child for child in self.objects[index:] if child.parent_id == item.id]
        if not children:
            self.objects.remove(item)
            self.stats["layers"] -= 1
            return None
        from .. import geometry
        bounds = geometry.union_bounds(children)
        if bounds is not None:
            item.x, item.y = bounds[0], bounds[1]
            item.width, item.height = bounds[2] - bounds[0], bounds[3] - bounds[1]
            item.rotation = 0.0
            item.flip_h = item.flip_v = False
        return item

    def _corners(self, item: LayoutObject, props: dict[str, Any], layer: str) -> None:
        radius = max(0.0, number(props.get("cornerRadius")))
        if props.get("rectangleCornerRadiiIndependent"):
            radii = [max(0.0, number(props.get(name), radius)) for name in (
                "rectangleTopLeftCornerRadius", "rectangleTopRightCornerRadius",
                "rectangleBottomRightCornerRadius", "rectangleBottomLeftCornerRadius")]
            if len(set(radii)) == 1:
                item.radius = round(radii[0], 4)
            else:
                item.corner_radii = [round(value, 4) for value in radii]
                item.radius = round(radii[0], 4)
        else:
            item.radius = round(radius, 4)
        smoothing = number(props.get("cornerSmoothing"))
        if smoothing > 1e-3 and (item.radius or item.corner_radii):
            item.metadata = {**item.metadata, "corner_smoothing": round(smoothing, 4)}

    # ── shapes ───────────────────────────────────────────────────────────

    def _rectangle(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject:
        fills = visible_paints(props.get("fillPaints"))
        if not world.is_rigid():
            return self._as_path(props, world, parent_id, layer, self._fill_geometry(props)
                                 or [paths.rounded_rect(*self._size(props), self._radii(props))])
        image = fills[0] if len(fills) == 1 and fills[0].get("type") == "IMAGE" else None
        width, height = self._size(props)
        if image is not None and not self._radii_any(props) and not visible_paints(props.get("strokePaints")) \
                and self._image_exact(image, width, height):
            item = self._object("image", props, world, parent_id, layer)
            item.asset_id = self._image_asset(image.get("image") or {}, layer, image.get("imageThumbnail"))
            if item.asset_id is None:
                item.metadata = {**item.metadata, "figma_missing_image": True}
            item.opacity = round(item.opacity * number(image.get("opacity"), 1.0), 4)
            item.effects = convert_effects(props, self.report, layer)
            self._add(item)
            return item
        item = self._object("rectangle", props, world, parent_id, layer)
        self._paint(item, props, layer)
        self._corners(item, props, layer)
        self._add(item)
        return item

    def _image_exact(self, paint: dict[str, Any], width: float, height: float) -> bool:
        """Whether Canvas's image layer (the picture stretched to its box) draws this fill exactly."""
        if paint.get("paintFilter") or paint.get("filterColorAdjust") or number(paint.get("rotation")):
            return False
        image_width = number(paint.get("originalImageWidth"))
        image_height = number(paint.get("originalImageHeight"))
        mode = paint.get("imageScaleMode", "FILL")
        transform = Affine.from_figma(paint.get("transform"))
        if mode == "STRETCH":
            return transform == IDENTITY or all(abs(a - b) < 1e-5 for a, b in zip(
                (transform.a, transform.b, transform.c, transform.d, transform.e, transform.f), (1, 0, 0, 1, 0, 0)))
        if mode in {"FILL", "FIT"} and image_width > 0 and image_height > 0 and width > 0 and height > 0:
            return abs(image_width / image_height - width / height) <= 0.002 * (width / height)
        return False

    @staticmethod
    def _radii(props: dict[str, Any]) -> tuple[float, float, float, float]:
        radius = max(0.0, number(props.get("cornerRadius")))
        if props.get("rectangleCornerRadiiIndependent"):
            return tuple(max(0.0, number(props.get(name), radius)) for name in (  # type: ignore[return-value]
                "rectangleTopLeftCornerRadius", "rectangleTopRightCornerRadius",
                "rectangleBottomRightCornerRadius", "rectangleBottomLeftCornerRadius"))
        return (radius,) * 4  # type: ignore[return-value]

    def _radii_any(self, props: dict[str, Any]) -> bool:
        return any(value > 0 for value in self._radii(props))

    def _ellipse(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject:
        arc = props.get("arcData") if isinstance(props.get("arcData"), dict) else {}
        start = number(arc.get("startingAngle"))
        end = number(arc.get("endingAngle"), FULL_TURN)
        inner = number(arc.get("innerRadius"))
        full = abs(abs(end - start) - FULL_TURN) < 1e-4 and inner <= 1e-6
        if not full or not world.is_rigid():
            geometry = self._fill_geometry(props) or [paths.ellipse(*self._size(props))]
            item = self._as_path(props, world, parent_id, layer, geometry)
            if not full:
                item.metadata = {**item.metadata, "arc": {"start": start, "end": end, "inner_ratio": inner}}
            return item
        item = self._object("ellipse", props, world, parent_id, layer)
        self._paint(item, props, layer)
        self._add(item)
        return item

    def _polygon(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject:
        kind = "star" if props.get("type") == "STAR" else "polygon"
        count = int(number(props.get("count"), 5 if kind == "star" else 3))
        ratio = max(0.0, min(1.0, number(props.get("starInnerScale"), 0.382)))
        width, height = self._size(props)
        geometry = self._fill_geometry(props)
        parametric = None
        if 3 <= count <= 60 and world.is_rigid() and not number(props.get("cornerRadius")):
            from .. import geometry as canvas_geometry
            vertices = canvas_geometry.polygon_vertices(width, height, count, ratio if kind == "star" else None)
            parametric = Subpath(vertices[0], [("L", x, y) for x, y in vertices[1:]], True)
        if parametric is not None and (not geometry or paths.outlines_match([parametric], geometry,
                                                                             max(0.5, 0.005 * max(width, height)))):
            item = self._object(kind, props, world, parent_id, layer)
            item.point_count = count
            if kind == "star":
                item.inner_ratio = round(ratio, 5)
            self._paint(item, props, layer)
            self._add(item)
            return item
        if not geometry:
            from .. import geometry as canvas_geometry
            vertices = canvas_geometry.polygon_vertices(width, height, max(3, min(60, count)),
                                                        ratio if kind == "star" else None)
            geometry = [Subpath(vertices[0], [("L", x, y) for x, y in vertices[1:]], True)]
        item = self._as_path(props, world, parent_id, layer, geometry)
        item.metadata = {**item.metadata, "figma_shape": kind, "point_count": count}
        return item

    def _line(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject:
        width, _ = self._size(props)
        if not world.is_rigid():
            return self._as_path(props, world, parent_id, layer, [Subpath((0.0, 0.0), [("L", width, 0.0)])],
                                 fills=False)
        item = self._object("line", props, world, parent_id, layer, size=(width, 0.0))
        item.path = [{"x": 0.0, "y": 0.0}, {"x": round(width, 4), "y": 0.0}]
        self._paint(item, props, layer, fills=False)
        item.fill = None
        self._add(item)
        return item

    def _fill_geometry(self, props: dict[str, Any], key: str = "fillGeometry") -> list[Subpath]:
        result: list[Subpath] = []
        for entry in props.get(key) or []:
            if not isinstance(entry, dict):
                continue
            blob = self.fig.blob(entry.get("commandsBlob"))
            if blob:
                result.extend(paths.decode_commands(blob))
        return result

    def _winding(self, props: dict[str, Any], key: str = "fillGeometry") -> str:
        rules = {entry.get("windingRule") for entry in props.get(key) or [] if isinstance(entry, dict)}
        return "evenodd" if rules == {"ODD"} else "nonzero"

    def _as_path(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str,
                 geometry: list[Subpath], *, fills: bool = True, winding: str | None = None,
                 style: dict[str, Any] | None = None, name: str | None = None) -> LayoutObject:
        """A vector layer from local geometry; unrigid transforms are baked in."""
        width, height = self._size(props)
        style_props = dict(props)
        if style:
            style_props.update(style)
        if world.is_rigid():
            item = self._object("path", props, world, parent_id, name or layer, size=(width, height))
            item.path = paths.to_nodes(geometry)
        else:
            baked = [subpath.transformed(world) for subpath in geometry]
            box = paths.bounds(baked) or (0.0, 0.0, 0.0, 0.0)
            left, top = box[0], box[1]
            shifted = [subpath.transformed(Affine(1, 0, 0, 1, -left, -top)) for subpath in baked]
            item = self._object("path", props, IDENTITY, parent_id, name or layer,
                                size=(box[2] - box[0], box[3] - box[1]))
            item.x, item.y, item.rotation = left, top, 0.0
            item.flip_h = item.flip_v = False
            item.path = paths.to_nodes(shifted)
            scale = math.sqrt(abs(world.determinant)) or 1.0
            if abs(scale - 1) > 1e-3:
                style_props["strokeWeight"] = number(props.get("strokeWeight"), 1.0) * scale
        if len(item.path) < 2:
            item.path = [{"x": 0.0, "y": 0.0}, {"x": round(width, 4), "y": round(height, 4)}]
        self._paint(item, style_props, layer, fills=fills)
        if not fills:
            item.fill = None
        rule = winding or self._winding(props)
        if rule == "evenodd":
            item.metadata = {**item.metadata, "fill_rule": "evenodd"}
        self.stats["vectors"] += 1
        self._add(item)
        return item

    def _vector(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject | None:
        width, height = self._size(props)
        vector = props.get("vectorData") if isinstance(props.get("vectorData"), dict) else {}
        blob = self.fig.blob(vector.get("vectorNetworkBlob"))
        shapes = None
        if blob:
            try:
                network = paths.decode_network(blob)
                normalized = vector.get("normalizedSize") if isinstance(vector.get("normalizedSize"), dict) else {}
                shapes = paths.network_shapes(network, (width, height),
                                              (number(normalized.get("x"), width), number(normalized.get("y"), height)))
            except paths.GeometryError:
                shapes = None
        styles: dict[int, dict[str, Any]] = {}
        for entry in vector.get("styleOverrideTable") or []:
            if isinstance(entry, dict) and isinstance(entry.get("styleID"), int):
                styles[entry["styleID"]] = entry
        rounded = number(props.get("cornerRadius")) > 0 or any(
            number(entry.get("cornerRadius")) > 0 for entry in styles.values())
        fill_geometry = self._fill_geometry(props)
        has_fill = bool(visible_paints(props.get("fillPaints"))) or any(
            visible_paints(entry.get("fillPaints")) for entry in styles.values())

        if shapes is None:
            if fill_geometry:
                return self._as_path(props, world, parent_id, layer, fill_geometry)
            stroke_geometry = self._fill_geometry(props, "strokeGeometry")
            if stroke_geometry:
                self.report.note("stroke_geometry", layer)
                outlined = dict(props, fillPaints=props.get("strokePaints"), strokePaints=[])
                return self._as_path(outlined, world, parent_id, layer, stroke_geometry, winding="nonzero")
            return None

        node_radius = max(0.0, number(props.get("cornerRadius")))

        def radius_of(vertex: int) -> float:
            style_id = shapes.vertex_styles[vertex] if 0 <= vertex < len(shapes.vertex_styles) else 0
            entry = styles.get(style_id)
            if entry is not None and "cornerRadius" in entry:
                return max(0.0, number(entry.get("cornerRadius")))
            return node_radius

        def rounded_shapes(subpaths: list[Subpath]) -> list[Subpath]:
            return [paths.round_corners(subpath, radius_of) for subpath in subpaths] if rounded else subpaths

        tolerance = max(0.5, 0.004 * max(width, height))
        distinct = {style for _winding, style, _loops in shapes.regions}
        pieces: list[tuple[str, list[Subpath], str, dict[str, Any] | None, bool]] = []
        if has_fill and shapes.regions:
            if len(distinct) <= 1:
                closed = rounded_shapes([subpath for _w, _s, loops in shapes.regions for subpath in loops])
                winding = "evenodd" if all(w == "ODD" for w, _s, _l in shapes.regions) else "nonzero"
                style = styles.get(next(iter(distinct))) if distinct else None
                geometry = closed
                if fill_geometry and (rounded or winding == "evenodd") and not paths.outlines_match(
                        closed, fill_geometry, tolerance):
                    geometry, winding = fill_geometry, self._winding(props)
                pieces.append((layer, geometry, winding, style, True))
            else:
                for index, (winding, style_id, loops) in enumerate(shapes.regions):
                    pieces.append((f"{layer} {index + 1}", rounded_shapes(loops),
                                   "evenodd" if winding == "ODD" else "nonzero", styles.get(style_id), True))
            lines = rounded_shapes(shapes.open_paths + shapes.loose_closed)
            if lines:
                pieces.append((f"{layer} lines", lines, "nonzero", None, False))
        else:
            everything = [subpath for _w, _s, loops in shapes.regions for subpath in loops]
            # Open paths first: line end decorations belong to the first subpath.
            everything = rounded_shapes(shapes.open_paths + everything + shapes.loose_closed)
            if not everything:
                return None
            pieces.append((layer, everything, "nonzero", None, has_fill))

        caps = self._vertex_caps(props, shapes, styles)
        if len(pieces) == 1:
            name, geometry, winding, style, fills = pieces[0]
            item = self._as_path(props, world, parent_id, layer, geometry, fills=fills, winding=winding, style=style)
            if caps:
                item.start_cap, item.end_cap = caps
            return item
        group = self._object("group", props, world, parent_id, layer)
        self._add(group)
        start = len(self.objects)
        child_props = dict(props, opacity=1.0, blendMode="PASS_THROUGH", horizontalConstraint=None,
                           verticalConstraint=None)
        for name, geometry, winding, style, fills in pieces:
            item = self._as_path(child_props, world, group.id, name, geometry, fills=fills, winding=winding,
                                 style=style)
            item.metadata = {**item.metadata, "figma_id": f"{item.metadata.get('figma_id')}#{name}"}
            if not fills and caps:
                item.start_cap, item.end_cap = caps
        from .. import geometry as canvas_geometry
        children = [child for child in self.objects[start:] if child.parent_id == group.id]
        bounds = canvas_geometry.union_bounds(children)
        if bounds:
            group.x, group.y, group.width, group.height = bounds[0], bounds[1], bounds[2] - bounds[0], bounds[3] - bounds[1]
            group.rotation, group.flip_h, group.flip_v = 0.0, False, False
        return group

    @staticmethod
    def _vertex_caps(props, shapes, styles) -> tuple[str, str] | None:
        """Line ends of the first open path: a vertex's own cap, else the layer's."""
        if not shapes.open_paths:
            return None
        default = END_CAPS.get(props.get("strokeCap"), "none")
        first = shapes.open_paths[0]
        if len(first.vertices) < 2:
            return default, default

        def cap(vertex: int) -> str:
            style_id = shapes.vertex_styles[vertex] if 0 <= vertex < len(shapes.vertex_styles) else 0
            entry = styles.get(style_id)
            if entry is not None and "strokeCap" in entry:
                return END_CAPS.get(entry.get("strokeCap"), "none")
            return default
        return cap(first.vertices[0]), cap(first.vertices[-1])

    def _boolean(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject | None:
        geometry = self._fill_geometry(props)
        if not geometry:
            # No computed result: keep the operands visible in a group.
            self.report.note("boolean", layer)
            return self._group(dict(props, type="GROUP"), world, parent_id, layer)
        # A live Canvas boolean: Figma's computed result is kept (see `finish_booleans`)
        # and the shapes it was made from stay editable inside it.
        item = self._as_path(props, world, parent_id, layer, geometry)
        operation = str(props.get("booleanOperation") or "UNION").lower()
        item.kind = "boolean"
        item.metadata = {**item.metadata, "figma_boolean": operation,
                         "boolean_operation": operation if operation in {"union", "subtract", "intersect", "exclude"}
                         else "union"}
        if item.rotation or item.flip_h or item.flip_v:
            from ..booleans import _world_subpaths, to_nodes
            from .. import geometry as canvas_geometry
            world_shapes = _world_subpaths(item)
            box = canvas_geometry.subpath_bounds(world_shapes)
            if box is not None:
                item.path = to_nodes(world_shapes, (box[0], box[1]))
                item.x, item.y = box[0], box[1]
                item.width, item.height = box[2] - box[0], box[3] - box[1]
            item.rotation, item.flip_h, item.flip_v = 0.0, False, False
        self._children_into(props, world, item.id)
        return item

    def _text(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str) -> LayoutObject:
        item = self._object("text", props, world, parent_id, layer)
        width, height = item.width, item.height

        def fill_for(style_props: dict[str, Any]) -> dict[str, Any]:
            return self.paints.fills(style_props, width, height, layer)

        fields = text_fields(props, fill_for, self.report, layer)
        item.text = fields["text"]
        item.metadata = {**item.metadata, **fields["metadata"]}
        # The layer's own fills are a stack, as on any other layer; a run with
        # a fill of its own keeps painting in that color (Pango attributes), so
        # a layer stack and per-run fills cannot both apply.
        stack = self.paints.fills(props, width, height, layer)["stack"]
        runs_paint = any("fill" in run or "fill_gradient" in run
                         for run in fields["metadata"].get("ranges") or [])
        if runs_paint and len(stack) > 1:
            self.report.note("text_fill", layer)
            stack = stack[:1]
        set_paint_stack(item, "fill", stack)
        if item.fill is None and item.fill_gradient is None:
            item.fill = "#00000000"
        item.effects = convert_effects(props, self.report, layer)
        if visible_paints(props.get("strokePaints")) and number(props.get("strokeWeight"), 1.0) > 0:
            self._stroke(item, props, layer)
        self.stats["text"] += 1
        self._add(item)
        return item

    def _fallback(self, props: dict[str, Any], world: Affine, parent_id: str | None, layer: str,
                  quiet: bool = False) -> LayoutObject | None:
        """Unknown or FigJam layers: their computed outline, text and children."""
        geometry = self._fill_geometry(props)
        kids = self._children(props)
        text = props.get("textData") if isinstance(props.get("textData"), dict) else None
        if not geometry and not kids and not (text and text.get("characters")):
            stroke_geometry = self._fill_geometry(props, "strokeGeometry")
            if stroke_geometry:
                outlined = dict(props, fillPaints=props.get("strokePaints"), strokePaints=[])
                return self._as_path(outlined, world, parent_id, layer, stroke_geometry, winding="nonzero")
            return None
        if not kids and not (text and text.get("characters")):
            return self._as_path(props, world, parent_id, layer, geometry)
        frame = self._object("frame", props, world, parent_id, layer)
        frame.clip_content = False
        self._add(frame)
        if geometry:
            self._as_path(dict(props, horizontalConstraint=None, verticalConstraint=None, opacity=1.0,
                               blendMode="PASS_THROUGH", effects=props.get("effects")),
                          world, frame.id, f"{layer} shape", geometry)
        if text and text.get("characters"):
            self._text(dict(props, fillPaints=props.get("textFillPaints") or [
                {"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0, "a": 1}}], effects=[], opacity=1.0,
                blendMode="PASS_THROUGH"), world, frame.id, f"{layer} text")
        self._children_into(props, world, frame.id)
        return frame


def _auto_layout(props: dict[str, Any]) -> dict[str, Any]:
    keys = ("stackMode", "stackSpacing", "stackPadding", "stackHorizontalPadding", "stackVerticalPadding",
            "stackPaddingRight", "stackPaddingBottom", "stackPrimarySizing", "stackCounterSizing",
            "stackPrimaryAlignItems", "stackCounterAlignItems", "stackWrap", "stackCounterSpacing")
    return {key: props[key] for key in keys if key in props and isinstance(props[key], (int, float, str, bool))}


def finish_booleans(objects: list[LayoutObject]) -> None:
    """Imported booleans keep the result Figma computed until their shapes change."""
    from .. import geometry as canvas_geometry
    from ..booleans import fingerprint
    tree = canvas_geometry.Tree(objects)
    for item in objects:
        if item.kind == "boolean":
            item.metadata = {**item.metadata, "boolean_key": fingerprint(tree, item)}


def migrate_group_paint_with_note(objects: list[LayoutObject], report: ImportReport) -> None:
    """A group keeps its shadow and blur; only an inner shadow, which needs a
    shape to fall inside, moves to the layers in the group (see
    `model.migrate_group_paint`). Say so when it does."""
    from ..model import group_inner_shadows
    for group in objects:
        if group.kind == "group" and group_inner_shadows(list(group.effects or [])):
            report.note("group_inner_shadow", group.name)
    holder = Document(id="import", name="import", objects=objects)
    migrate_group_paint(holder)
