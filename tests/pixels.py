# SPDX-License-Identifier: MPL-2.0
"""Reading pixels back out of a document, the way each renderer draws it.

`cairo_pixels` draws the scene's render node into an image surface, which is
exactly what PNG and PDF export do. `renderer_pixels` hands the same node to a
live GSK renderer (the one GSK_RENDERER asks for: cairo, opengl or vulkan), so
the GPU paths are checked against the same numbers.
"""

from __future__ import annotations

from typing import Any

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Gdk, Graphene, Gsk, Gtk
    HAVE_GTK = True
except (ImportError, ValueError):  # pragma: no cover - no GTK here
    HAVE_GTK = False

from luma_canvas.export import render_node


def _surface(document: Any, scale: float = 1.0):
    import cairo
    node, bounds = render_node(document)
    width = max(1, int(round(bounds[2] * scale)))
    height = max(1, int(round(bounds[3] * scale)))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.scale(scale, scale)
    context.translate(-bounds[0], -bounds[1])
    if node is not None:
        node.draw(context)
    surface.flush()
    return surface, bounds


def _unpremultiply(b: int, g: int, r: int, a: int) -> tuple[int, int, int, int]:
    if a in (0, 255):
        return (r, g, b, a)
    return (min(255, round(r * 255 / a)), min(255, round(g * 255 / a)), min(255, round(b * 255 / a)), a)


def cairo_pixels(document: Any, points: list[tuple[float, float]], scale: float = 1.0) -> list[tuple[int, ...]]:
    """The color at each world point, drawn the way PNG export draws it."""
    surface, bounds = _surface(document, scale)
    data = bytes(surface.get_data())
    stride = surface.get_stride()
    width, height = surface.get_width(), surface.get_height()
    result = []
    for x, y in points:
        px = min(width - 1, max(0, int((x - bounds[0]) * scale)))
        py = min(height - 1, max(0, int((y - bounds[1]) * scale)))
        offset = py * stride + px * 4
        blue, green, red, alpha = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
        result.append(_unpremultiply(blue, green, red, alpha))
    return result


def texture_pixels(texture, bounds, points: list[tuple[float, float]]) -> list[tuple[int, ...]]:
    """The color at each world point of a rendered texture.

    The texture is read back through a PNG, which every GSK renderer writes the
    same way (8 bit sRGB, straight alpha), so GPU and Cairo results compare.
    """
    import cairo
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "texture.png"
        texture.save_to_png(str(path))
        surface = cairo.ImageSurface.create_from_png(str(path))
    data = bytes(surface.get_data())
    stride = surface.get_stride()
    width, height = surface.get_width(), surface.get_height()
    result = []
    for x, y in points:
        px = min(width - 1, max(0, int(x - bounds[0])))
        py = min(height - 1, max(0, int(y - bounds[1])))
        offset = py * stride + px * 4
        blue, green, red, alpha = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
        result.append(_unpremultiply(blue, green, red, alpha))
    return result


class OffscreenRenderer:
    """A realized GSK renderer of the running GSK_RENDERER kind."""

    def __init__(self) -> None:
        self.window = Gtk.Window()
        self.window.set_default_size(64, 64)
        self.window.set_child(Gtk.Box())
        self.window.present()
        self._pump()
        native = self.window.get_native()
        self.renderer = native.get_renderer() if native is not None else None
        if self.renderer is None:  # pragma: no cover - no display
            raise RuntimeError("no GSK renderer")

    @staticmethod
    def _pump() -> None:
        from gi.repository import GLib
        context = GLib.MainContext.default()
        for _ in range(200):
            if not context.pending():
                break
            context.iteration(False)

    @property
    def name(self) -> str:
        return type(self.renderer).__name__

    def pixels(self, document: Any, points: list[tuple[float, float]]) -> list[tuple[int, ...]]:
        node, bounds = render_node(document)
        if node is None:
            return [(0, 0, 0, 0)] * len(points)
        area = Graphene.Rect().init(bounds[0], bounds[1], max(bounds[2], 1.0), max(bounds[3], 1.0))
        texture = self.renderer.render_texture(node, area)
        return texture_pixels(texture, bounds, points)

    def close(self) -> None:
        self.window.destroy()
        self._pump()


def difference(found: tuple[int, ...], wanted: tuple[int, int, int]) -> int:
    return max(abs(found[index] - wanted[index]) for index in range(3))
