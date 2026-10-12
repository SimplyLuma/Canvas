# SPDX-License-Identifier: Apache-2.0
"""Export: SVG, PNG, JPEG and PDF of a page, a selection or any layers.

Every format draws what the canvas draws. PNG, JPEG and PDF go through the
scene renderer itself; SVG is written by `svg_export`, which mirrors it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .model import Document, LayoutObject


def export_svg(document: Document, destination: Path, object_ids: Iterable[str] | None = None, *,
               outline_text: bool = True) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(svg_text(document, object_ids, outline_text=outline_text), encoding="utf-8")
    return destination


def svg_text(document: Document, object_ids: Iterable[str] | None = None, *, outline_text: bool = True) -> str:
    from .svg_export import svg_document
    family, roots = _family(document, object_ids)
    bounds = export_bounds(document, object_ids)
    return svg_document(document, family, roots, bounds, outline_text=outline_text)


# ── raster and print ─────────────────────────────────────────────────────
#
# PNG, JPEG and PDF are drawn by the same scene renderer as the canvas, so an
# export is what the canvas shows: clip content, masks, strokes over frame
# children, gradients, images and effects. GSK render nodes draw into cairo:
# pixels for PNG and JPEG, vectors (and real text) for PDF.

EXPORT_FORMATS = ("svg", "png", "jpeg", "pdf")
SCALES = (1, 2, 3)
SKIPPED_KINDS = {"guide", "comment", "slice"}


def _effect_reach(item: LayoutObject) -> tuple[float, float, float, float]:
    """How far a layer's shadows and blur reach past its box: left, top, right, bottom."""
    left = top = right = bottom = 0.0
    for effect in item.effects or []:
        if not isinstance(effect, dict) or not effect.get("visible", effect.get("enabled", True)):
            continue
        kind = str(effect.get("type") or effect.get("kind") or "").replace("-", "_")
        blur = max(0.0, float(effect.get("blur", effect.get("radius", 0)) or 0))
        if kind == "drop_shadow":
            spread = float(effect.get("spread", 0) or 0)
            dx, dy = float(effect.get("x", 0) or 0), float(effect.get("y", 0) or 0)
            reach = blur + max(spread, 0.0)
            left, right = max(left, reach - dx), max(right, reach + dx)
            top, bottom = max(top, reach - dy), max(bottom, reach + dy)
        elif kind == "layer_blur":
            left, top, right, bottom = (max(value, blur) for value in (left, top, right, bottom))
    return left, top, right, bottom


def _stroke_reach(item: LayoutObject) -> float:
    from . import geometry
    width = float(item.stroke_width or 0)
    if width <= 0 or (item.stroke is None and not item.stroke_gradient):
        return 0.0
    if geometry.is_open_kind(item):
        caps = {item.start_cap, item.end_cap} - {None, "none", "round", "square"}
        return width / 2 + (max(6.0, width * 3.0) if caps else width / 2)
    sides = (item.metadata or {}).get("stroke_sides")
    if isinstance(sides, dict):
        try:
            width = max(float(value or 0) for value in sides.values())
        except (TypeError, ValueError):
            pass
    reach = {"inside": 0.0, "center": width / 2, "outside": width}.get(item.stroke_align, width / 2)
    if item.stroke_join == "miter" and item.kind in {"polygon", "star", "path"}:
        reach *= 2   # miter corners stick out past the half width
    return reach


def render_bounds(tree, item: LayoutObject) -> tuple[float, float, float, float] | None:
    """The world area a layer draws into, children, strokes and effects included."""
    from . import geometry
    if not item.visible or item.kind in SKIPPED_KINDS:
        return None
    left, top, right, bottom = geometry.world_bounds(item)
    reach = _stroke_reach(item)
    el, et, er, eb = _effect_reach(item)
    own = (left - reach - el, top - reach - et, right + reach + er, bottom + reach + eb)
    drawn = item.kind not in geometry.TRANSPARENT_CONTAINERS or item.kind == "layout"
    boxes = [own] if drawn else []
    children = tree.children.get(item.id, []) if item.kind != "boolean" else []
    if children and not geometry.clips(item):
        masks = geometry.masks_below(children)
        for child, below in zip(children, masks):
            if geometry.mask_mode(child):
                continue
            box = render_bounds(tree, child)
            if box is None:
                continue
            for mask in below:
                ml, mt, mr, mb = geometry.world_bounds(mask)
                box = (max(box[0], ml), max(box[1], mt), min(box[2], mr), min(box[3], mb))
            if box[2] > box[0] and box[3] > box[1]:
                boxes.append(box)
    elif children:
        boxes.append((left, top, right, bottom))
    if not boxes:
        return None
    box = (min(box[0] for box in boxes), min(box[1] for box in boxes),
           max(box[2] for box in boxes), max(box[3] for box in boxes))
    if not drawn and children:
        # A group's own shadow and blur are cast by its layers together, so
        # they reach past them (see `scene.group_effects_of`).
        return (box[0] - el, box[1] - et, box[2] + er, box[3] + eb)
    return box


def _family(document: Document, object_ids: Iterable[str] | None) -> tuple[list[LayoutObject], list[LayoutObject]]:
    """The exported layers (roots and everything inside them, in paint order) and the roots."""
    from . import geometry
    objects = document.objects
    if object_ids:
        requested = set(object_ids)
        chosen = [item for item in objects if item.id in requested]
        chosen_ids = {item.id for item in chosen}
        by_id = {item.id: item for item in objects}

        def inside_chosen(item: LayoutObject) -> bool:
            parent = by_id.get(item.parent_id)
            while parent is not None:
                if parent.id in chosen_ids:
                    return True
                parent = by_id.get(parent.parent_id)
            return False
        roots = [item for item in chosen if not inside_chosen(item)]
        included = {item.id for item in roots}
        tree = geometry.Tree(objects)
        pending = [item.id for item in roots]
        while pending:
            for child in tree.children.get(pending.pop(), []):
                included.add(child.id)
                pending.append(child.id)
        family = [item for item in objects if item.id in included]
    else:
        family = list(objects)
        known = {item.id for item in family}
        roots = [item for item in family if item.parent_id not in known]
    family = [item for item in family if item.kind not in SKIPPED_KINDS]
    return family, [item for item in roots if item.visible and item.kind not in SKIPPED_KINDS]


def export_bounds(document: Document, object_ids: Iterable[str] | None = None) -> tuple[float, float, float, float]:
    """left, top, width, height of what an export covers."""
    from . import geometry
    family, roots = _family(document, object_ids)
    tree = geometry.Tree(family)
    boxes = [box for box in (render_bounds(tree, root) for root in roots) if box is not None]
    if not boxes:
        raise ValueError("nothing visible to export")
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[2] for box in boxes)
    bottom = max(box[3] for box in boxes)
    return left, top, max(1.0, right - left), max(1.0, bottom - top)


class _Subset:
    def __init__(self, document: Document, objects: list[LayoutObject]) -> None:
        self.objects = objects
        self.assets = document.assets
        self.embedded_assets = document.embedded_assets


def render_node(document: Document, object_ids: Iterable[str] | None = None, *, renderer=None):
    """The exported layers as one GSK render node in world units, and its bounds."""
    import gi
    gi.require_version("Gtk", "4.0")
    from .scene import SceneRenderer
    family, _roots = _family(document, object_ids)
    bounds = export_bounds(document, object_ids)
    node = (renderer or SceneRenderer(None)).scene(_Subset(document, family))
    return node, bounds


def _pixel_size(width: float, height: float, scale: float) -> tuple[int, int]:
    import math
    return (max(1, math.ceil(width * scale - 1e-6)), max(1, math.ceil(height * scale - 1e-6)))


def _draw_into(context, node, bounds: tuple[float, float, float, float], scale: float) -> None:
    if node is None:
        return
    left, top, _width, _height = bounds
    context.save()
    context.scale(scale, scale)
    context.translate(-left, -top)
    node.draw(context)
    context.restore()


def _surface(document: Document, object_ids, scale: float, background: tuple[float, float, float] | None):
    import cairo
    node, bounds = render_node(document, object_ids)
    width, height = _pixel_size(bounds[2], bounds[3], scale)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32 if background is None else cairo.FORMAT_RGB24, width, height)
    context = cairo.Context(surface)
    if background is not None:
        context.set_source_rgb(*background)
        context.paint()
    _draw_into(context, node, bounds, scale)
    surface.flush()
    return surface


def export_png(document: Document, destination: Path, *, scale: float = 1,
               object_ids: Iterable[str] | None = None) -> Path:
    """A lossless raster, at any scale (1×, 2×, 3× in the dialog)."""
    surface = _surface(document, object_ids, scale, None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(destination))
    return destination


def export_jpeg(document: Document, destination: Path, *, scale: float = 1,
                quality: int = 92, object_ids: Iterable[str] | None = None) -> Path:
    """The only lossy format Canvas writes; transparency becomes white."""
    surface = _surface(document, object_ids, scale, (1.0, 1.0, 1.0))
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
    except (ImportError, ValueError) as error:
        raise ValueError("JPEG export needs GdkPixbuf") from error
    pixbuf = Gdk.pixbuf_get_from_surface(surface, 0, 0, surface.get_width(), surface.get_height())
    if pixbuf is None:
        raise ValueError("could not read the rendered image back")
    pixbuf.savev(str(destination), "jpeg", ["quality"], [str(max(1, min(100, quality)))])
    return destination


def export_pdf(document: Document, destination: Path, *,
               object_ids: Iterable[str] | None = None,
               bleed: float = 0.0) -> Path:
    """Vector out (text stays text), with a page's bleed honoured when it has one."""
    import cairo
    node, bounds = render_node(document, object_ids)
    family, roots = _family(document, object_ids)
    page = next((item for item in roots if item.kind in {"page", "frame"}), None)
    if page is not None and page.bleed:
        bleed = max(bleed, page.bleed)
    destination.parent.mkdir(parents=True, exist_ok=True)
    surface = cairo.PDFSurface(str(destination), bounds[2] + bleed * 2, bounds[3] + bleed * 2)
    context = cairo.Context(surface)
    context.translate(bleed, bleed)
    _draw_into(context, node, bounds, 1.0)
    context.show_page()
    surface.finish()
    return destination


def thumbnail_png(document: Document, size: int = 512) -> bytes:
    import io
    left, top, width, height = export_bounds(document, None)
    scale = min(1.0, size / max(width, height))
    surface = _surface(document, None, scale, None)
    buffer = io.BytesIO()
    surface.write_to_png(buffer)
    return buffer.getvalue()


def export(document: Document, destination: Path, *, fmt: str = "svg",
           scale: float = 1, object_ids: Iterable[str] | None = None) -> Path:
    """One way in, whichever format is asked for."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Canvas cannot write {fmt}")
    if fmt == "svg":
        return export_svg(document, destination, object_ids)
    if fmt == "png":
        return export_png(document, destination, scale=scale, object_ids=object_ids)
    if fmt == "jpeg":
        return export_jpeg(document, destination, scale=scale, object_ids=object_ids)
    return export_pdf(document, destination, object_ids=object_ids)


# ── export settings (Figma's Export section) ────────────────────────────
#
# A layer keeps a list of export rows in `metadata["export_settings"]`, each
# `{"format", "constraint", "value", "suffix"}`: constraint "scale" (value is
# the factor), "width" or "height" (value is pixels, the other side follows).
# Lists are replaced, never changed in place, so undo snapshots stay intact.

SETTINGS_KEY = "export_settings"
FORMAT_NAMES = {"png": "PNG", "jpeg": "JPG", "svg": "SVG", "pdf": "PDF"}
EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "svg": ".svg", "pdf": ".pdf"}
RASTER_FORMATS = frozenset({"png", "jpeg"})
CONSTRAINTS = ("scale", "width", "height")
SCALE_PRESETS = ("0.5x", "0.75x", "1x", "1.5x", "2x", "3x", "4x", "512w", "512h")
MAX_SCALE = 16.0
MAX_PIXELS = 16384
_FORMAT_ALIASES = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "svg": "svg", "pdf": "pdf"}
_UNSAFE = set('\\:*?"<>|')


def default_setting() -> dict:
    return {"format": "png", "constraint": "scale", "value": 1.0, "suffix": ""}


def parse_constraint(text: str) -> tuple[str, float] | None:
    """Figma's size field: `2x`, `0.5`, `512w`, `300h`, `512px` (width)."""
    value = str(text or "").strip().lower().replace(",", ".").replace(" ", "")
    if not value:
        return None
    constraint = "scale"
    if value.endswith("px"):
        value, constraint = value[:-2], "width"
    elif value[-1] in "wh":
        value, constraint = value[:-1], {"w": "width", "h": "height"}[value[-1]]
    elif value[-1] in "x×":
        value = value[:-1]
    if value.startswith("@"):
        value = value[1:]
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if constraint == "scale":
        return constraint, min(number, MAX_SCALE)
    return constraint, float(min(max(1, round(number)), MAX_PIXELS))


def constraint_label(setting: dict) -> str:
    value = float(setting.get("value", 1) or 1)
    constraint = setting.get("constraint", "scale")
    if constraint == "width":
        return f"{value:g}w"
    if constraint == "height":
        return f"{value:g}h"
    return f"{value:g}x"


def normalize_setting(value: object) -> dict | None:
    """A clean export row from anything stored or imported, or None."""
    if not isinstance(value, dict):
        return None
    fmt = _FORMAT_ALIASES.get(str(value.get("format", "png")).lower())
    if fmt is None:
        return None
    constraint = value.get("constraint", "scale")
    if constraint not in CONSTRAINTS:
        constraint = "scale"
    try:
        number = float(value.get("value", 1))
    except (TypeError, ValueError):
        number = 1.0
    unit = {"scale": "x", "width": "w", "height": "h"}[constraint]
    parsed = parse_constraint(f"{number!r}{unit}") if math.isfinite(number) and number > 0 else None
    constraint, number = parsed or ("scale", 1.0)
    suffix = value.get("suffix") or ""
    suffix = "".join(ch for ch in str(suffix) if ch.isprintable() and ch not in _UNSAFE and ch != "/")[:64]
    return {"format": fmt, "constraint": constraint, "value": number, "suffix": suffix}


def export_settings(item: LayoutObject) -> list[dict]:
    """A layer's export rows, cleaned (a new list each call)."""
    stored = (item.metadata or {}).get(SETTINGS_KEY)
    if not isinstance(stored, list):
        return []
    return [row for row in (normalize_setting(value) for value in stored) if row is not None]


def with_export_settings(metadata: dict | None, settings: list[dict]) -> dict:
    """New metadata carrying `settings`; the old dict and list are left alone."""
    result = {key: value for key, value in (metadata or {}).items() if key != SETTINGS_KEY}
    rows = [row for row in (normalize_setting(value) for value in settings) if row is not None]
    if rows:
        result[SETTINGS_KEY] = rows
    return result


def next_setting(settings: list[dict]) -> dict:
    """What + adds: 1x first, then the next whole scale with its @Nx suffix (Figma)."""
    if not settings:
        return default_setting()
    last = settings[-1]
    scales = [float(row.get("value", 1)) for row in settings if row.get("constraint", "scale") == "scale"]
    scale = float(math.floor(max(scales)) + 1) if scales else 2.0
    scale = min(scale, MAX_SCALE)
    return {"format": last.get("format", "png"), "constraint": "scale", "value": scale,
            "suffix": f"@{scale:g}x"}


def export_scale(setting: dict, bounds: tuple[float, float, float, float]) -> float:
    """The factor a row renders at for an export area of `bounds`."""
    value = float(setting.get("value", 1) or 1)
    constraint = setting.get("constraint", "scale")
    if constraint == "width":
        value = value / max(bounds[2], 1e-6)
    elif constraint == "height":
        value = value / max(bounds[3], 1e-6)
    longest = max(bounds[2], bounds[3], 1e-6)
    return max(1e-3, min(value, MAX_PIXELS / longest))


def _clean_part(part: str) -> str:
    text = "".join("_" if (ch in _UNSAFE or not ch.isprintable()) else ch for ch in part)
    return text.strip(". ")


def file_name(name: str, setting: dict) -> str:
    """The relative file a row writes: the layer name plus suffix and extension.

    A slash in a layer name makes folders, as in Figma (`icons/home` writes
    `icons/home.png`). Characters no file system accepts become `_`.
    """
    parts = [_clean_part(part) for part in str(name or "").split("/")]
    parts = [part for part in parts if part] or ["Untitled"]
    parts[-1] = (parts[-1] + str(setting.get("suffix") or "")).strip() or "Untitled"
    return "/".join(parts) + EXTENSIONS.get(setting.get("format", "png"), ".png")


@dataclass(frozen=True)
class ExportJob:
    """One file to write: where (relative), what, and how."""
    path: str
    object_ids: tuple[str, ...] | None
    setting: dict = field(compare=False)
    name: str = ""


def plan_exports(targets: Iterable[tuple[str, Iterable[str] | None, list[dict]]]) -> list[ExportJob]:
    """Files for `(name, object_ids, settings)` targets: one per row per target.

    Two rows that would write the same file keep both: the later one gets
    its scale (`@2x`), then a number, so nothing is silently overwritten.
    """
    jobs: list[ExportJob] = []
    taken: set[str] = set()
    for name, object_ids, settings in targets:
        ids = tuple(object_ids) if object_ids is not None else None
        for setting in settings:
            row = normalize_setting(setting)
            if row is None:
                continue
            path = file_name(name, row)
            if path.casefold() in taken and not row["suffix"] and row["format"] in RASTER_FORMATS:
                path = file_name(name, {**row, "suffix": f"@{constraint_label(row)}"})
            stem, extension = path[: -len(EXTENSIONS[row["format"]])], EXTENSIONS[row["format"]]
            counter = 2
            while path.casefold() in taken:
                path = f"{stem} {counter}{extension}"
                counter += 1
            taken.add(path.casefold())
            jobs.append(ExportJob(path, ids, row, name))
    return jobs


def write_export(document: Document, destination: Path, setting: dict,
                 object_ids: Iterable[str] | None = None) -> Path:
    """Write one export row. SVG and PDF are vectors, so their size is 1x."""
    row = normalize_setting(setting) or default_setting()
    ids = list(object_ids) if object_ids is not None else None
    scale = 1.0
    if row["format"] in RASTER_FORMATS:
        scale = export_scale(row, export_bounds(document, ids))
    return export(document, destination, fmt=row["format"], scale=scale, object_ids=ids)


def run_exports(document: Document, directory: Path, jobs: Iterable[ExportJob]) -> list[Path]:
    """Write every job under `directory`; returns the files written.

    A job whose layers draw nothing is skipped rather than failing the batch;
    if every job is empty the ValueError is raised.
    """
    written: list[Path] = []
    errors: list[ValueError] = []
    root = Path(directory).resolve()
    for job in jobs:
        destination = (root / job.path).resolve()
        if root not in destination.parents:
            raise ValueError(f"refusing to write outside the export folder: {job.path}")
        try:
            written.append(write_export(document, destination, job.setting, job.object_ids))
        except ValueError as error:
            errors.append(error)
    if not written and errors:
        raise errors[0]
    return written
