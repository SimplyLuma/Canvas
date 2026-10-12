# SPDX-License-Identifier: Apache-2.0
"""LumaUI media family: shared plumbing for the media, image and measure parts.

- The family's tokens live in config/shared/design-tokens.d/media.json: the
  generator turns them into `--lumaui-media-*` variables, `@luma_*` colours
  and `lumaui_tokens.MEDIA`; the kit loads luma-appkit-media.css after the
  base sheet (install_lumaui()).
- `colour(widget, role)` resolves an `@luma_*` role for drawing (snapshot or
  Cairo), so parts that draw their own pixels never hold a literal.
- `glyph_path(name)` is a Lucide glyph as a `Gsk.Path`, for glyphs a part must
  fill (a favourite heart) or draw thousands of times.
- `TextureLoader` decodes pictures off the main loop into a small LRU cache,
  with a generation check so a recycled tile never shows a stale picture.
"""
from __future__ import annotations

import os
import re
import threading
import warnings
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

# ── Drawing helpers ────────────────────────────────────────────────────────

# GTK 4.10 deprecated the style context, but it is still the only way to read
# a named colour for drawing; the warning is silenced for this module only.
warnings.filterwarnings("ignore", category=DeprecationWarning, module=__name__)

def colour(widget: Gtk.Widget, role: str, alpha: float | None = None) -> Gdk.RGBA:
    """An `@luma_*` colour role for drawing; the widget's ink when the role is missing."""
    found, value = False, None
    try:
        found, value = widget.get_style_context().lookup_color(role)
    except (AttributeError, TypeError):
        pass
    if not found or value is None:
        value = widget.get_color()
    if alpha is not None:
        value = value.copy()
        value.alpha *= alpha
    return value


def rgba(red: float, green: float, blue: float, alpha: float = 1.0) -> Gdk.RGBA:
    value = Gdk.RGBA()
    value.red, value.green, value.blue, value.alpha = red, green, blue, alpha
    return value


def rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, max(0.0, width), max(0.0, height))


def rounded(bounds: Graphene.Rect, tl: float, tr: float | None = None, br: float | None = None,
            bl: float | None = None) -> Gsk.RoundedRect:
    """A rounded rectangle; one radius for all corners, or four (top-left, clockwise)."""
    tr = tl if tr is None else tr
    br = tl if br is None else br
    bl = tl if bl is None else bl
    shape = Gsk.RoundedRect()
    shape.init(bounds, *(Graphene.Size().init(r, r) for r in (tl, tr, br, bl)))
    return shape


_glyphs: dict[str, Gsk.Path | None] = {}
_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def _shape_path(tag: str, attributes: str) -> str:
    """An SVG rect, circle or ellipse as path data (Lucide draws pause, square and some dots this way)."""
    values = {k: float(v) for k, v in re.findall(r'(?:^|\s)(x|y|width|height|rx|ry|cx|cy|r)="([-\d.]+)"', attributes)}
    if tag == "rect":
        x, y, w, h = values.get("x", 0.0), values.get("y", 0.0), values.get("width", 0.0), values.get("height", 0.0)
        rx = min(values.get("rx", values.get("ry", 0.0)), w / 2, h / 2)
        if rx <= 0:
            return f" M{x} {y} h{w} v{h} h{-w} Z"
        return (f" M{x + rx} {y} h{w - 2 * rx} a{rx} {rx} 0 0 1 {rx} {rx} v{h - 2 * rx}"
                f" a{rx} {rx} 0 0 1 {-rx} {rx} h{-(w - 2 * rx)} a{rx} {rx} 0 0 1 {-rx} {-rx}"
                f" v{-(h - 2 * rx)} a{rx} {rx} 0 0 1 {rx} {-rx} Z")
    cx, cy = values.get("cx", 0.0), values.get("cy", 0.0)
    rx = values.get("r", values.get("rx", 0.0))
    ry = values.get("r", values.get("ry", rx))
    if rx <= 0:
        return ""
    return f" M{cx - rx} {cy} a{rx} {ry} 0 1 0 {2 * rx} 0 a{rx} {ry} 0 1 0 {-2 * rx} 0 Z"


def _absolute_start(d: str) -> str:
    """A path's first moveto is absolute even when written "m"; once joined to another path it would not be.

    "m17 2 4 4-4 4" becomes "M17 2 l4 4-4 4" (the pairs after a moveto are linetos of the same case).
    """
    d = d.strip()
    if not d.startswith("m"):
        return d
    match = re.match(rf"m\s*({_NUMBER})[\s,]*({_NUMBER})(.*)", d, re.S)
    if not match:
        return "M" + d[1:]
    rest = match.group(3).lstrip(" ,")
    if rest and not rest[0].isalpha():
        rest = "l" + rest
    return f"M{match.group(1)} {match.group(2)} {rest}"


def glyph_path(lucide: str) -> Gsk.Path | None:
    """A Lucide glyph's geometry (24 × 24 units) as one `Gsk.Path`, or None if it was never imported."""
    if lucide in _glyphs:
        return _glyphs[lucide]
    from . import icons

    path = None
    for folder in icons.search_paths():
        svg = folder / f"{icons.icon_name(lucide)}.svg"
        if svg.is_file():
            text = svg.read_text()
            data = " ".join(_absolute_start(d) for d in re.findall(r'\sd="([^"]+)"', text))
            data += "".join(_shape_path(tag, attrs) for tag, attrs in re.findall(r"<(rect|circle|ellipse)\s([^>]*)>", text))
            if data.strip():
                path = Gsk.Path.parse(data)
            break
    _glyphs[lucide] = path
    return path


def draw_glyph(snapshot: Gtk.Snapshot, lucide: str, x: float, y: float, size: float, ink: Gdk.RGBA,
               *, filled: bool = False, stroke: float = 1.6) -> None:
    """Draw a Lucide glyph `size` px square at (x, y): stroked like the icon theme, or filled."""
    path = glyph_path(lucide)
    if path is None:
        return
    snapshot.save()
    snapshot.translate(Graphene.Point().init(x, y))
    snapshot.scale(size / 24.0, size / 24.0)
    if filled:
        snapshot.append_fill(path, Gsk.FillRule.WINDING, ink)
    line = Gsk.Stroke.new(stroke)
    line.set_line_cap(Gsk.LineCap.ROUND)
    line.set_line_join(Gsk.LineJoin.ROUND)
    snapshot.append_stroke(path, line, ink)
    snapshot.restore()


def text_layout(widget: Gtk.Widget, text: str, size: float, weight: int = 400, *, tabular: bool = False):
    """A Pango layout in the kit face at `size` px and `weight`, for parts that draw their own text."""
    gi.require_version("Pango", "1.0")
    from gi.repository import Pango

    layout = widget.create_pango_layout(text)
    description = widget.get_pango_context().get_font_description() or Pango.FontDescription()
    description = description.copy()
    description.set_absolute_size(size * Pango.SCALE)
    description.set_weight(weight)
    layout.set_font_description(description)
    if tabular:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_font_features_new("tnum=1"))
        layout.set_attributes(attrs)
    return layout


def append_layout(snapshot: Gtk.Snapshot, layout, x: float, y: float, ink: Gdk.RGBA) -> None:
    snapshot.save()
    snapshot.translate(Graphene.Point().init(x, y))
    snapshot.append_layout(layout, ink)
    snapshot.restore()


def cover_rect(texture_width: float, texture_height: float, box: Graphene.Rect) -> Graphene.Rect:
    """Where a picture goes so it covers `box` (object-fit: cover), centred."""
    if texture_width <= 0 or texture_height <= 0:
        return box
    scale = max(box.get_width() / texture_width, box.get_height() / texture_height)
    width, height = texture_width * scale, texture_height * scale
    return rect(box.get_x() + (box.get_width() - width) / 2, box.get_y() + (box.get_height() - height) / 2,
                width, height)


def contain_rect(texture_width: float, texture_height: float, box: Graphene.Rect) -> Graphene.Rect:
    """Where a picture goes so all of it fits `box` (object-fit: contain), centred."""
    if texture_width <= 0 or texture_height <= 0:
        return box
    scale = min(box.get_width() / texture_width, box.get_height() / texture_height)
    width, height = texture_width * scale, texture_height * scale
    return rect(box.get_x() + (box.get_width() - width) / 2, box.get_y() + (box.get_height() - height) / 2,
                width, height)


def grayscale_matrix(amount: float) -> tuple[Graphene.Matrix, Graphene.Vec4]:
    """A colour matrix for CSS `grayscale(amount)`."""
    a = max(0.0, min(1.0, amount))
    r, g, b = 0.2126, 0.7152, 0.0722
    rows = [
        (r + (1 - r) * (1 - a), g - g * (1 - a), b - b * (1 - a)),
        (r - r * (1 - a), g + (1 - g) * (1 - a), b - b * (1 - a)),
        (r - r * (1 - a), g - g * (1 - a), b + (1 - b) * (1 - a)),
    ]
    # Graphene matrices multiply row vectors: column j of the CSS matrix is row j here.
    values = [rows[0][0], rows[1][0], rows[2][0], 0,
              rows[0][1], rows[1][1], rows[2][1], 0,
              rows[0][2], rows[1][2], rows[2][2], 0,
              0, 0, 0, 1]
    return Graphene.Matrix().init_from_float(values), Graphene.Vec4().init(0, 0, 0, 0)


def brightness_matrix(amount: float) -> tuple[Graphene.Matrix, Graphene.Vec4]:
    """A colour matrix for CSS `brightness(amount)`."""
    values = [amount, 0, 0, 0, 0, amount, 0, 0, 0, 0, amount, 0, 0, 0, 0, 1]
    return Graphene.Matrix().init_from_float(values), Graphene.Vec4().init(0, 0, 0, 0)


# ── Pictures, decoded off the main loop ────────────────────────────────────

class TextureLoader:
    """Decode picture files on worker threads; hand textures back on the main loop.

    `request(key, source, side, callback)` calls `callback(texture)` on the
    main loop, from the cache at once when it can. The caller keeps its own
    generation (the key it last asked for) and ignores late answers, which is
    what makes it safe for recycled grid tiles.
    """

    _shared: "TextureLoader | None" = None

    def __init__(self, capacity: int = 600, workers: int | None = None) -> None:
        self._cache: OrderedDict[tuple, Gdk.Texture] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers or min(4, os.cpu_count() or 2),
                                        thread_name_prefix="lumaui-media")

    @classmethod
    def shared(cls) -> "TextureLoader":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @staticmethod
    def bucket(side: float) -> int:
        """Round a requested size up to a step so nearby sizes share a decode."""
        side = max(32, int(side))
        step = 64 if side <= 512 else 256
        return (side + step - 1) // step * step

    def cached(self, source: str, side: float) -> Gdk.Texture | None:
        key = (source, self.bucket(side))
        with self._lock:
            texture = self._cache.get(key)
            if texture is not None:
                self._cache.move_to_end(key)
            return texture

    def request(self, source: str, side: float, callback: Callable[[Gdk.Texture | None], None]) -> None:
        texture = self.cached(source, side)
        if texture is not None:
            callback(texture)
            return
        key = (source, self.bucket(side))
        self._pool.submit(self._decode, key, callback)

    def _decode(self, key: tuple, callback: Callable[[Gdk.Texture | None], None]) -> None:
        source, side = key
        texture = None
        try:
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            _format, width, height = GdkPixbuf.Pixbuf.get_file_info(source)
            if width and height:
                scale = min(1.0, side / min(width, height))
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(source, max(1, round(width * scale)),
                                                                  max(1, round(height * scale)), True)
            else:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(source)
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
            texture = Gdk.MemoryTexture.new(
                pixbuf.get_width(), pixbuf.get_height(),
                Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8,
                pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())
        except Exception:  # noqa: BLE001 - an unreadable picture shows the empty tile
            texture = None
        if texture is not None:
            with self._lock:
                self._cache[key] = texture
                while len(self._cache) > self._capacity:
                    self._cache.popitem(last=False)

        def deliver() -> bool:
            callback(texture)
            return False

        GLib.idle_add(deliver)
