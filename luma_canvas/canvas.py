# SPDX-License-Identifier: Apache-2.0
"""GTK canvas view for direct manipulation of the unified document graph.

One input router maps pointer, touchpad, pinch and keyboard input to the
active tool (spec 3.4, 4, 5, 7):

* Ctrl+wheel and pinch zoom at the pointer; the wheel and touchpad pan in both
  axes with unquantized deltas and kinetic deceleration; Shift+wheel pans
  sideways; middle drag and Space+drag pan from any tool.
* Figma's click scope, Ctrl deep select, marquee (touch, Alt encloses, Ctrl
  deep), eight resize handles, rotation just outside the corners, Alt-drag
  duplicate, nudges, corner radius handles, and a size badge.
* Smart guides, equal spacing and pixel snapping with Ctrl held to suspend
  them, and Alt-hover measurement.
* Drawing rectangles, ellipses, lines, arrows, polygons, stars, frames, text
  and pen paths in the current style.

Modifier state is read from each event, never cached, so a modifier pressed
mid-drag takes effect on the next motion, and a hovered canvas in a window
without keyboard focus still zooms on Ctrl+scroll when the compositor reports
modifiers with pointer events. Keys that are held rather than modifiers (Space
to pan) are forgotten when keyboard focus leaves, because their release is
delivered to whichever window has focus by then. The scene is drawn by
`scene.SceneRenderer` with cached per-object render nodes; overlays are drawn
in screen space on top.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

import cairo
import gi

gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import geometry  # noqa: E402
from .editing import KIND_NAMES, EditError, Editor  # noqa: E402
from .model import Document, LayoutObject, new_id  # noqa: E402
from .paint import (  # noqa: E402
    Color, RecentColors, color_of, default_current_style, format_number, normalized_style,
    rgb_to_hsl, rgb_to_hsv, solid, to_hex,
)
from .scene import SceneRenderer, gsk_path, point, rect, rgba  # noqa: E402
from .snapping import SCREEN_THRESHOLD, Measurement, SnapEngine, snap_rotation  # noqa: E402
from . import clipboard, nodes, rulers, text_edit  # noqa: E402
from .typography import fitted_size, font_description, type_settings  # noqa: E402
from .vector import close_path, node, set_handle  # noqa: E402
from .viewport import (  # noqa: E402
    ANIMATION_MS, View, clamp_zoom, fit_rect, interpolate_view, ladder_step, scroll_action,
)

# Instrumentation colors (spec 14.7): overlays are instruments over document
# content, so these are literal and do not change with the appearance.
GUIDE_RED = Color(0xF2 / 255, 0x4E / 255, 0x1E / 255, 1.0)
HANDLE_FILL = Color(1, 1, 1, 1)
BADGE_DARK = Color(0.10, 0.11, 0.13, 0.92)
DRAW_TOOLS = {"rectangle", "ellipse", "line", "arrow", "polygon", "star", "frame", "text", "image"}
TOOL_KIND = {"arrow": "line"}
DRAG_THRESHOLD = 3.0
HANDLE_SIZE = 8.0
HANDLE_HIT = 8.0
ROTATE_ZONE = 16.0
LABEL_GAP = 4.0
LABEL_HEIGHT = 14.0
CURSORS = {"select": "default", "direct": "default", "scale": "default", "hand": "grab",
           "zoom": "zoom-in", "text": "text", "dropper": "crosshair"}


def _modifiers(state: Gdk.ModifierType) -> tuple[bool, bool, bool]:
    return (bool(state & Gdk.ModifierType.SHIFT_MASK),
            bool(state & Gdk.ModifierType.CONTROL_MASK),
            bool(state & Gdk.ModifierType.ALT_MASK))


def _white(alpha: float = 1.0) -> Gdk.RGBA:
    return Gdk.RGBA(red=1.0, green=1.0, blue=1.0, alpha=alpha)


def _black(alpha: float = 1.0) -> Gdk.RGBA:
    return Gdk.RGBA(red=0.0, green=0.0, blue=0.0, alpha=alpha)


class LayoutCanvas(Gtk.Widget):
    __gtype_name__ = "LumaCanvasCanvas"
    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "document-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "context-requested": (GObject.SignalFlags.RUN_LAST, None, (float, float, object)),
        "comment-requested": (GObject.SignalFlags.RUN_LAST, None, (float, float)),
        "image-needed": (GObject.SignalFlags.RUN_LAST, None, (float, float)),
        # The canvas changed tool by itself: a shape finished, Escape, or a
        # tool key the rail does not show.
        "tool-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # Zoom or scroll changed.
        "view-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # A move, resize, rotate or radius drag changed geometry live.
        "transform-preview": (GObject.SignalFlags.RUN_LAST, None, ()),
        # The shown page changed.
        "page-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # On-canvas text editing started (True) or ended (False).
        "text-editing-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        # The current style or the selection's paint changed (style chip).
        "style-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # Vector edit mode started (True) or ended (False).
        "vector-editing-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        # Rulers were shown (True) or hidden (False).
        "rulers-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    def __init__(self, editor: Editor) -> None:
        super().__init__()
        self.editor = editor
        self.tool = "select"
        self.show_grid = False
        self.show_rulers = False
        self._guide_selected: str | None = None
        self._guide_hover: str | None = None
        self._autoscroll: dict[str, Any] | None = None
        self.show_pixel_grid = True
        self.outline = False
        self.snap = True
        self.pixel_snap = True
        self.grid_size = 0.0
        self.reduced_motion = False
        self.renderer = SceneRenderer(self)
        self._scene_node: Gsk.RenderNode | None = None
        self._scene_key: tuple | None = None
        self._scene_dirty = True

        # The renderer hears about layer changes only while the canvas is
        # shown, so a closed window stops listening to every edit in the app.
        def shown(*_args) -> None:
            self.renderer.resume()
            self._scene_dirty = True

        def hidden(*_args) -> None:
            self.renderer.close()
        self.connect("map", shown)
        self.connect("unmap", hidden)

        self._pointer: tuple[float, float] | None = None
        self._press: dict[str, Any] | None = None
        self._mode: str | None = None
        self._space_held = False
        self._last_click = (0.0, 0.0, 0.0, 0)
        self._hover_id: str | None = None
        self._hover_label_id: str | None = None
        self._hover_handle: tuple[str, Any] | None = None
        self._snap_guides: list = []
        self._snap_measurements: list[Measurement] = []
        self._endpoint_snap: tuple[float, float] | None = None
        self._hover_measurements: list[Measurement] = []
        self._badge: tuple[str, float, float] | None = None
        self._marquee: tuple[float, float, float, float] | None = None
        self._reparent_target: str | None = None
        self._pen_path: list[dict] = []
        self._pen_dragging = False
        self._vector: dict[str, Any] | None = None
        self._dropper: dict[str, Any] | None = None
        self._animation: dict[str, Any] | None = None
        self._kinetic: dict[str, Any] | None = None
        self._pinch: dict[str, Any] | None = None
        self._fake_gesture: _FakeGesture | None = None
        self._cursor_cache: dict[str, Gdk.Cursor] = {}
        self.gradient_editor: dict[str, Any] | None = None
        self.supports_text_editing = True
        # A locked tool stays active after drawing until Escape or V (Figma and
        # XD: double-click a tool to keep it).
        self.tool_locked = False
        self._rename: Gtk.Entry | None = None
        self.supported_shape_tools = ("rectangle", "ellipse", "line", "arrow", "polygon", "star")
        self._text: text_edit.TextSession | None = None
        self._local_clipboard: dict[str, Any] | None = None
        self._caret_on = True
        self._caret_source = 0
        self._im = Gtk.IMMulticontext()
        self._im.connect("commit", self._im_commit)
        self._im.connect("preedit-changed", self._im_preedit)
        self._im.connect("retrieve-surrounding", self._im_surrounding)
        self._im.connect("delete-surrounding", self._im_delete_surrounding)

        self.set_focusable(True)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Canvas"])

        drag = Gtk.GestureDrag(button=0)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("enter", self._motion)
        motion.connect("leave", self._leave)
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES | Gtk.EventControllerScrollFlags.KINETIC
        )
        scroll.connect("scroll", self._scroll)
        scroll.connect("scroll-begin", lambda *_: self._stop_kinetic())
        scroll.connect("decelerate", self._decelerate)
        self.add_controller(scroll)

        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._pinch_begin)
        pinch.connect("scale-changed", self._pinch_scale)
        pinch.connect("end", lambda *_: setattr(self, "_pinch", None))
        self.add_controller(pinch)

        keys = Gtk.EventControllerKey()
        self._keys = keys
        keys.connect("key-pressed", self._key_pressed)
        keys.connect("key-released", self._key_released)
        keys.connect("modifiers", self._modifiers_changed)
        self.add_controller(keys)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", self._focus_entered)
        focus.connect("leave", self._focus_left)
        self.add_controller(focus)

        self.connect("map", lambda *_: GLib.idle_add(self._take_focus))
        self.connect("unmap", lambda *_: self.end_text_edit())

    # ── compatibility and state ──────────────────────────────────────────

    @property
    def document(self) -> Document:
        return self.editor.document

    @property
    def view(self) -> View:
        return View(self.document.zoom, self.document.canvas_x, self.document.canvas_y)

    def _set_view(self, view: View, *, notify: bool = True) -> None:
        self.document.zoom = clamp_zoom(view.zoom)
        self.document.canvas_x, self.document.canvas_y = view.x, view.y
        self._queue_view()
        if notify:
            self.emit("view-changed")

    def _take_focus(self) -> bool:
        root = self.get_root()
        if self.get_mapped() and root is not None:
            focus = root.get_focus()
            if focus is None or not isinstance(focus, (Gtk.Editable, Gtk.TextView)):
                self.grab_focus()
        return GLib.SOURCE_REMOVE

    def _focus_soon(self) -> None:
        """Take keyboard focus after the current event.

        Grabbing focus inside the press handler makes a focused inspector
        field commit and rebuild while GTK is still moving focus, which
        released the field twice (`g_object_unref` assertion).
        """
        if self.has_focus():
            return

        def grab() -> bool:
            if self._rename is not None or self._text is not None:
                return GLib.SOURCE_REMOVE
            if self.get_mapped() and self.get_root() is not None and not self.has_focus():
                self.grab_focus()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(grab)

    def set_hover_object(self, object_id: str | None) -> None:
        """Outline a layer hovered elsewhere (a Layers row)."""
        if object_id != self._hover_id:
            self._hover_id = object_id
            self._queue_view()

    def zoom_to(self, zoom: float) -> None:
        """Zoom to an exact value about the view center (the zoom entry)."""
        self._animate_to(self.view.zoomed_at(zoom, self.get_width() / 2, self.get_height() / 2))

    def replace_editor(self, editor: Editor) -> None:
        if editor is self.editor:
            # The window calls this on every refresh; keep caches and edits.
            self.queue_draw()
            return
        self.end_text_edit()
        self.end_vector_edit()
        self.editor = editor
        self.renderer.invalidate()
        self.queue_draw()

    # ── pages ────────────────────────────────────────────────────────────

    def show_page(self, page_id: str) -> bool:
        """Show another page: its layers, selection and remembered view."""
        self.end_text_edit()
        self.end_vector_edit()
        self.end_rename()
        if self._pen_path:
            self._finish_pen(close=False)
        if not self.editor.show_page(page_id):
            return False
        self._hover_id = None
        self._scene_dirty = True
        self.queue_draw()
        self.emit("page-changed")
        self.emit("selection-changed")
        self.emit("view-changed")
        return True

    def queue_draw(self) -> None:
        """The document may have changed: rebuild what changed and redraw."""
        # Live booleans follow their operands while they are dragged or edited.
        from .booleans import update_booleans
        update_booleans(self.document.objects)
        self._scene_dirty = True
        Gtk.Widget.queue_draw(self)

    def _queue_view(self) -> None:
        """Only the viewport or the overlays changed: reuse the composed scene."""
        Gtk.Widget.queue_draw(self)

    def set_tool(self, tool: str, *, locked: bool | None = None) -> None:
        """Choose a tool; `locked=True` keeps a drawing tool after each shape."""
        if locked is not None:
            self.tool_locked = bool(locked) and tool not in {"select", "direct", "hand", "scale"}
        elif tool != self.tool:
            self.tool_locked = False
        if tool != "path" and self._pen_path:
            self._finish_pen(close=False)
        if self._dropper and tool != "dropper":
            self.cancel_eyedropper(notify=False)
        self.tool = tool
        if tool == "dropper":
            self.begin_eyedropper(self._apply_dropper_to_selection, return_tool="select")
        self._update_cursor()
        self._queue_view()

    def world_to_view(self, x: float, y: float) -> tuple[float, float]:
        return self.view.to_view(x, y)

    def view_to_world(self, x: float, y: float) -> tuple[float, float]:
        return self.view.to_document(x, y)

    # Held modifiers are read from events now; these stay for older callers.
    def set_snap_suppressed(self, suppressed: bool) -> None:
        return None

    def set_measuring(self, measuring: bool) -> None:
        return None

    # ── rendering ────────────────────────────────────────────────────────

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        # Pickers and menus pointing at inspector rows are parented here,
        # because the canvas is never rebuilt; popovers must be presented.
        child = self.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Popover) and child.get_visible():
                child.present()
            child = child.get_next_sibling()
        self._allocate_rename()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        view = self.view
        self._snapshot_scene(snapshot, width, height, view)
        self._snapshot_overlays(snapshot, width, height, view)

    @staticmethod
    def _visible_world(width: float, height: float, view: View,
                       margin: float) -> tuple[float, float, float, float]:
        left, top = view.to_document(-width * margin, -height * margin)
        right, bottom = view.to_document(width * (1 + margin), height * (1 + margin))
        return left, top, right, bottom

    def device_scale(self) -> float:
        """Device pixels per view pixel, fractional scaling included."""
        native = self.get_native()
        surface = native.get_surface() if native is not None else None
        if surface is not None and hasattr(surface, "get_scale"):
            try:
                return float(surface.get_scale())
            except (TypeError, ValueError):
                pass
        return float(self.get_scale_factor() or 1)

    def _snapshot_scene(self, snapshot: Gtk.Snapshot, width: float, height: float,
                        view: View, scale: float | None = None) -> None:
        scale = scale or self.device_scale()
        grid = view.zoom * scale
        visible = self._visible_world(width, height, view, 0.0)
        key = self._scene_key
        if (key is not None and self._scene_node is not None and not self._scene_dirty
                and abs(key[1]-grid)>1e-9 and getattr(self,'_zoom_ready_grid',None)!=grid):
            # Scaling a retained vector scene is immediate. Re-snap geometry
            # once the gesture settles, rather than rebuild every wheel tick.
            if getattr(self,'_zoom_requested_grid',None)!=grid:
                source=getattr(self,'_zoom_rebuild_source',0)
                if source:GLib.source_remove(source)
                self._zoom_requested_grid=grid
                requested=grid
                def settle_zoom():
                    self._zoom_rebuild_source=0
                    self._zoom_ready_grid=requested
                    self._queue_view()
                    return False
                self._zoom_rebuild_source=GLib.timeout_add(120,settle_zoom)
            grid=key[1]
        reuse = (not self._scene_dirty and not self.renderer.stale and self._scene_node is not None
                 and key is not None and key[0] == self.outline and abs(key[1] - grid) < 1e-9
                 and key[2][0] <= visible[0] and key[2][1] <= visible[1]
                 and key[2][2] >= visible[2] and key[2][3] >= visible[3])
        if not reuse:
            culled = self._visible_world(width, height, view, 1.0)
            # The renderer reuses every top-level layer that did not change,
            # so this costs what changed, not the size of the document.
            self._scene_node = self.renderer.scene(self.document, visible=culled, outline_mode=self.outline,
                                                   zoom=grid/scale, device_scale=grid, tree=self._tree())
            self._scene_key = (self.outline, grid, culled)
            self._scene_dirty = False
            if self.renderer.stale and not getattr(self, "_stale_source", 0):
                # Some layers still show their last zoom's snapping, or are not
                # built yet: finish them over the next frames, never now.
                def again() -> bool:
                    self._stale_source = 0
                    Gtk.Widget.queue_draw(self)
                    return False
                self._stale_source = GLib.timeout_add(0, again)
        if self._scene_node is None:
            return
        snapshot.save()
        # The document origin sits on a whole device pixel, so snapped edges
        # stay on whole pixels after the view transform.
        snapshot.translate(point(round(view.x * scale) / scale, round(view.y * scale) / scale))
        snapshot.scale(view.zoom, view.zoom)
        # The document goes in behind an opacity node of 1. GTK 4.22's GPU
        # renderers otherwise walk into it looking for occluders, and a clip
        # there that fails that pass leaks onto everything drawn after it
        # ("Clipping is broken"; layers vanish on partial redraws, seen on
        # Intel GPUs). Nodes without an occlusion handler are drawn whole.
        snapshot.append_node(Gsk.OpacityNode.new(self._scene_node, 1.0))
        snapshot.restore()

    def _draw_item(self, cr: cairo.Context, item: LayoutObject) -> None:
        """Paint one object into a cairo context at its view position."""
        content = self.renderer.content_node(item)
        if content is None:
            return
        view = self.view
        snapshot = Gtk.Snapshot()
        snapshot.translate(point(view.x, view.y))
        snapshot.scale(view.zoom, view.zoom)
        SceneRenderer._apply_transform(snapshot, item)
        snapshot.append_node(content)
        node = snapshot.to_node()
        if node is not None:
            node.draw(cr)

    def _token(self, name: str, fallback: str) -> Gdk.RGBA:
        found, color = self.get_style_context().lookup_color(name)
        if found:
            return color
        value = Gdk.RGBA()
        value.parse(fallback)
        return value

    def _snapshot_overlays(self, snapshot: Gtk.Snapshot, width: float, height: float,
                           view: View) -> None:
        accent = self._token("luma_accent", "#527fae")
        if self.show_pixel_grid and view.zoom >= 4.0:
            self._draw_pixel_grid(snapshot, width, height, view)
        if self.show_grid:
            self._draw_grid(snapshot, width, height, view)
        from .layout_grids import draw as draw_layout_grids
        draw_layout_grids(self,snapshot,view)
        self._draw_guides(snapshot, width, height, view)
        self._draw_frame_labels(snapshot, view, accent)
        for index, comment in enumerate(item for item in self.document.objects if item.kind == "comment"):
            cx, cy = view.to_view(comment.x, comment.y)
            self._draw_badge(snapshot, str(index + 1), cx, cy, accent)
        hover = self.document.object(self._hover_id) if self._hover_id else None
        if (hover is not None and hover.id not in self.document.selected_ids
                and self._mode in {None, "marquee"} and not self._dropper):
            self._stroke_outline(snapshot, hover, view, accent, 1.5, dashed=hover.kind == "group")
        if self._reparent_target and self._reparent_target != "__page__":
            target = self.document.object(self._reparent_target)
            if target is not None:
                self._stroke_outline(snapshot, target, view, accent, 1.0)
        if self._text is not None:
            self._draw_text_editing(snapshot, view, accent)
        elif self._vector is None:
            self._draw_selection(snapshot, view, accent)
        self._draw_vector_edit(snapshot, view, accent)
        self._draw_pen(snapshot, view, accent)
        self._draw_gradient_handles(snapshot, view, accent)
        self._draw_snapping(snapshot, view)
        if self._marquee is not None:
            x0, y0 = view.to_view(self._marquee[0], self._marquee[1])
            x1, y1 = view.to_view(self._marquee[2], self._marquee[3])
            left, top = round(min(x0, x1)), round(min(y0, y1))
            box_w, box_h = round(abs(x1 - x0)), round(abs(y1 - y0))
            fill = Gdk.RGBA(red=accent.red, green=accent.green, blue=accent.blue, alpha=0.10)
            snapshot.append_color(fill, rect(left, top, box_w, box_h))
            builder = Gsk.PathBuilder.new()
            builder.add_rect(rect(left + 0.5, top + 0.5, max(box_w - 1, 0), max(box_h - 1, 0)))
            snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(1.0), accent)
        if self._badge is not None:
            text, bx, by = self._badge
            self._draw_badge(snapshot, text, bx, by, accent, anchor="top", measurement=True)
        if self.show_rulers:
            self._draw_rulers(snapshot, width, height, view, accent)
        self._draw_loupe(snapshot, width, height)
        if self._rename is not None:
            self.snapshot_child(self._rename, snapshot)

    def _stroke_outline(self, snapshot: Gtk.Snapshot, item: LayoutObject, view: View,
                        color: Gdk.RGBA, width: float, *, dashed: bool = False) -> None:
        if item.kind in {"text", "image", "group", "instance", "component", "layout"}:
            shapes = [geometry.rounded_rect(item.width, item.height, (0, 0, 0, 0))]
        else:
            shapes = geometry.outline(item)
        path = gsk_path(shapes, lambda x, y: view.to_view(*geometry.to_world(item, x, y)))
        if path is not None:
            stroke = Gsk.Stroke.new(width)
            if dashed:
                stroke.set_dash([4.0, 3.0])
            snapshot.append_stroke(path, stroke, color)

    def _draw_pixel_grid(self, snapshot: Gtk.Snapshot, width: float, height: float,
                         view: View) -> None:
        ink = self.get_color()
        ink.alpha = 0.08
        left, top = view.to_document(0, 0)
        right, bottom = view.to_document(width, height)
        builder = Gsk.PathBuilder.new()
        for x in range(math.floor(left), math.ceil(right) + 1):
            vx = round(view.to_view(x, 0)[0]) + 0.5
            builder.move_to(vx, 0)
            builder.line_to(vx, height)
        for y in range(math.floor(top), math.ceil(bottom) + 1):
            vy = round(view.to_view(0, y)[1]) + 0.5
            builder.move_to(0, vy)
            builder.line_to(width, vy)
        snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(1.0), ink)

    def _draw_grid(self, snapshot: Gtk.Snapshot, width: float, height: float, view: View) -> None:
        spacing = 24 * view.zoom
        if spacing < 8:
            return
        ink = self.get_color()
        ink.alpha = 0.10
        builder = Gsk.PathBuilder.new()
        x = view.x % spacing
        while x < width:
            builder.move_to(round(x) + 0.5, 0)
            builder.line_to(round(x) + 0.5, height)
            x += spacing
        y = view.y % spacing
        while y < height:
            builder.move_to(0, round(y) + 0.5)
            builder.line_to(width, round(y) + 0.5)
            y += spacing
        snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(1.0), ink)

    def _layout(self, text: str, size: float, weight: int) -> Pango.Layout:
        layout = self.create_pango_layout(text)
        layout.set_font_description(font_description("Figtree", weight, False, size))
        attributes = Pango.AttrList()
        attributes.insert(Pango.attr_font_features_new("tnum"))
        layout.set_attributes(attributes)
        return layout

    # ── frame labels (Figma: names above top-level frames) ───────────────

    def frame_labels(self, view: View | None = None) -> list[tuple[LayoutObject, Graphene.Rect, Pango.Layout]]:
        """Label layout and hit rectangle for each top-level frame, in view px.

        Labels are a constant 11px on screen, 4px above the frame's top-left,
        truncated to the frame's on-screen width and hidden below 24px.
        """
        view = view or self.view
        result = []
        tree = self._tree()
        layouts = getattr(self, "_label_layouts", None)
        if layouts is None or len(layouts) > 1024:
            layouts = self._label_layouts = {}
        for item in tree.children.get(None, []):
            if item.kind not in geometry.FRAME_LIKE or not item.visible:
                continue
            left, top, right, _ = geometry.world_bounds(item) if item.rotation else (
                item.x, item.y, item.x + item.width, 0)
            x, y = view.to_view(item.x, item.y) if not item.rotation else view.to_view(left, top)
            width = (item.width if not item.rotation else right - left) * view.zoom
            if width < 24:
                continue
            # Laid out once per name and width: a page of hundreds of frames
            # would otherwise lay out every label on every frame drawn.
            key = (item.name, round(width))
            cached = layouts.get(key)
            if cached is None:
                layout = self._layout(item.name, 11, 550)
                layout.set_width(round(width * Pango.SCALE))
                layout.set_ellipsize(Pango.EllipsizeMode.END)
                cached = layouts[key] = (layout, layout.get_pixel_extents()[1])
            layout, logical = cached
            area = rect(round(x), round(y - LABEL_GAP - LABEL_HEIGHT), min(width, logical.width + 2),
                        LABEL_HEIGHT)
            result.append((item, area, layout))
        return result

    def _label_at(self, x: float, y: float) -> LayoutObject | None:
        for item, area, _ in reversed(self.frame_labels()):
            if area.contains_point(point(x, y)):
                return item
        return None

    def _draw_frame_labels(self, snapshot: Gtk.Snapshot, view: View, accent: Gdk.RGBA) -> None:
        muted = self._token("luma_muted", "#757e87")
        ink = self.get_color()
        selected = set(self.document.selected_ids)
        for item, area, layout in self.frame_labels(view):
            if self._rename is not None and item.id == self._rename.rename_id:
                continue
            if item.id in selected:
                color = accent
            elif item.id in {self._hover_id, self._hover_label_id}:
                color = ink
            else:
                color = muted
            _, logical = layout.get_pixel_extents()
            snapshot.save()
            snapshot.translate(point(area.get_x(), area.get_y() + LABEL_HEIGHT - logical.height))
            snapshot.append_layout(layout, color)
            snapshot.restore()

    def _draw_badge(self, snapshot: Gtk.Snapshot, text: str, x: float, y: float,
                    background: Gdk.RGBA | Color, *, anchor: str = "center", measurement: bool = False) -> None:
        layout = self._layout(text, 11, 600)
        _, logical = layout.get_pixel_extents()
        width, height = logical.width + 12, logical.height + 8
        left = round(x - width / 2)
        top = round(y if anchor == "top" else y - height / 2)
        box = Gsk.RoundedRect()
        box.init_from_rect(rect(left, top, width, height), 4)
        color = background if isinstance(background, Gdk.RGBA) else rgba(background)
        foreground = _white()
        if measurement:
            context = self.get_style_context()
            found, surface = context.lookup_color("luma_float_surface")
            if found:
                color = surface
            found, ink = context.lookup_color("luma_ink")
            if found:
                foreground = ink
        snapshot.push_rounded_clip(box)
        snapshot.append_color(color, rect(left, top, width, height))
        snapshot.pop()
        snapshot.save()
        snapshot.translate(point(left + 6, top + 4))
        snapshot.append_layout(layout, foreground)
        snapshot.restore()

    def _handle_square(self, snapshot: Gtk.Snapshot, x: float, y: float, accent: Gdk.RGBA,
                       *, round_handle: bool = False, size: float = HANDLE_SIZE) -> None:
        x, y = round(x) + 0.5, round(y) + 0.5
        builder = Gsk.PathBuilder.new()
        if round_handle:
            builder.add_circle(point(x, y), size / 2)
        else:
            area = Gsk.RoundedRect()
            area.init_from_rect(rect(x - size / 2, y - size / 2, size, size), 1)
            builder.add_rounded_rect(area)
        path = builder.to_path()
        snapshot.append_fill(path, Gsk.FillRule.WINDING, rgba(HANDLE_FILL))
        snapshot.append_stroke(path, Gsk.Stroke.new(1.0), accent)

    # ── selection geometry on screen ─────────────────────────────────────

    def _selection_items(self) -> list[LayoutObject]:
        return [item for item in self.document.selection if item.visible]

    def _selection_frame(self, view: View | None = None) -> dict[str, Any] | None:
        """Screen corners (nw, ne, se, sw) of the selection box."""
        view = view or self.view
        items = self._selection_items()
        if not items:
            return None
        if len(items) == 1:
            item = items[0]
            corners = [view.to_view(*corner) for corner in geometry.world_corners(item)]
            return {"item": item, "corners": corners, "rotation": item.rotation}
        bounds = geometry.union_bounds(items)
        left, top = view.to_view(bounds[0], bounds[1])
        right, bottom = view.to_view(bounds[2], bounds[3])
        return {"item": None, "corners": [(left, top), (right, top), (right, bottom), (left, bottom)],
                "rotation": 0.0}

    @staticmethod
    def _handle_points(corners: list[tuple[float, float]]) -> dict[str, tuple[float, float]]:
        nw, ne, se, sw = corners

        def mid(a, b):
            return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        return {"nw": nw, "n": mid(nw, ne), "ne": ne, "e": mid(ne, se), "se": se,
                "s": mid(se, sw), "sw": sw, "w": mid(sw, nw)}

    @staticmethod
    def _is_line(item: LayoutObject | None) -> bool:
        return item is not None and item.kind == "line" and len(item.path or []) == 2

    def _draw_selection(self, snapshot: Gtk.Snapshot, view: View, accent: Gdk.RGBA) -> None:
        frame = self._selection_frame(view)
        if frame is None:
            return
        item = frame["item"]
        if self._badge is None:
            if item is not None:
                width, height = item.width, item.height
            else:
                left, top, right, bottom = geometry.union_bounds(self._selection_items())
                width, height = right - left, bottom - top
            corners = frame["corners"]
            self._draw_badge(snapshot, f"{round(width)} × {round(height)}",
                             sum(point[0] for point in corners) / len(corners),
                             max(point[1] for point in corners) + 8, accent, anchor="top", measurement=True)
        if len(self.document.selected_ids) > 1:
            for selected in self._selection_items():
                self._stroke_outline(snapshot, selected, view, accent, 1.0)
        if self._is_line(item) and self.tool != "scale":
            self._stroke_outline(snapshot, item, view, accent, 1.0)
            if self._mode != "move":
                for entry in item.path[:2]:
                    self._handle_square(snapshot, *view.to_view(*geometry.to_world(item, entry["x"], entry["y"])), accent)
            return
        corners = frame["corners"]
        builder = Gsk.PathBuilder.new()
        aligned = corners
        if not frame["rotation"]:
            left, top = corners[0]
            right, bottom = corners[2]
            left, top = math.floor(left) + .5, math.floor(top) + .5
            right, bottom = math.ceil(right) - .5, math.ceil(bottom) - .5
            aligned = [(left, top), (right, top), (right, bottom), (left, bottom)]
        builder.move_to(*aligned[0])
        for corner in aligned[1:]:
            builder.line_to(*corner)
        builder.close()
        snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(1.0), accent)
        if self._mode in {"move", "rotate"} or self.tool not in {"select", "direct", "scale"}:
            return
        handles = self._handle_points(corners)
        box_w = math.dist(corners[0], corners[1])
        box_h = math.dist(corners[1], corners[2])
        for name, (hx, hy) in handles.items():
            if len(name) == 1 and ((name in "ns" and box_w < 24) or (name in "ew" and box_h < 24)):
                continue
            self._handle_square(snapshot, hx, hy, accent)
        if (item is not None and item.kind == "rectangle" and box_w >= 40 and box_h >= 40
                and (self._hover_id == item.id or self._mode == "radius"
                     or (self._hover_handle or ("",))[0] == "radius")):
            for hx, hy in self._radius_handle_points(item, view):
                self._handle_square(snapshot, hx, hy, accent, round_handle=True)

    def _radius_handle_points(self, item: LayoutObject, view: View) -> list[tuple[float, float]]:
        radii = geometry.corner_radii_of(item)
        inset_min = 12 / max(view.zoom, 1e-6)
        corners = [(0, 0, 1, 1), (item.width, 0, -1, 1), (item.width, item.height, -1, -1),
                   (0, item.height, 1, -1)]
        limit = min(item.width, item.height) / 2
        points = []
        for (cx, cy, sx, sy), radius in zip(corners, radii):
            inset = min(max(inset_min, radius), limit)
            points.append(view.to_view(*geometry.to_world(item, cx + sx * inset, cy + sy * inset)))
        return points

    def _draw_nodes(self, snapshot: Gtk.Snapshot, view: View, accent: Gdk.RGBA) -> None:
        if self.tool != "direct":
            return
        for item in self._selection_items():
            if not item.path:
                continue
            for entry in item.path:
                x, y = view.to_view(*geometry.to_world(item, float(entry["x"]), float(entry["y"])))
                for prefix in ("in", "out"):
                    if f"{prefix}_x" not in entry:
                        continue
                    hx, hy = view.to_view(*geometry.to_world(item, float(entry[f"{prefix}_x"]),
                                                             float(entry[f"{prefix}_y"])))
                    line = Gsk.PathBuilder.new()
                    line.move_to(x, y)
                    line.line_to(hx, hy)
                    snapshot.append_stroke(line.to_path(), Gsk.Stroke.new(1.0), accent)
                    self._handle_square(snapshot, hx, hy, accent, round_handle=True, size=6)
                self._handle_square(snapshot, x, y, accent)

    def _draw_pen(self, snapshot: Gtk.Snapshot, view: View, accent: Gdk.RGBA) -> None:
        if not self._pen_path:
            return
        preview = [dict(entry) for entry in self._pen_path]
        if self._pointer is not None and not self._pen_dragging:
            preview.append(node(*view.to_document(*self._pointer)))
        outline = geometry.nodes_outline(preview)
        if outline is not None:
            path = gsk_path([outline], view.to_view)
            if path is not None:
                snapshot.append_stroke(path, Gsk.Stroke.new(1.5), accent)
        for entry in self._pen_path:
            self._handle_square(snapshot, *view.to_view(float(entry["x"]), float(entry["y"])), accent)

    @staticmethod
    def _edited_gradient(item: LayoutObject, target: str, index: int | None) -> dict[str, Any] | None:
        """The gradient the open picker edits: one paint of the stack, or the bottom one."""
        if index is None:
            return getattr(item, f"{target}_gradient", None)
        from .paint import stack_paint
        paint = stack_paint(item, target, index)
        return paint if paint and paint.get("type") in {"linear", "radial", "angular", "diamond"} else None

    def _gradient_handle_points(self, view: View) -> list[tuple[LayoutObject, list[tuple[float, float]]]]:
        editor = self.gradient_editor or {}
        target = editor.get("target", "fill")
        result = []
        for item in self._selection_items():
            paint = self._edited_gradient(item, target, editor.get("index"))
            if not paint:
                continue
            handles = paint.get("handles") or []
            count = 3 if paint.get("type") == "radial" else 2
            points = [view.to_view(*geometry.to_world(item, hx * item.width, hy * item.height))
                      for hx, hy in handles[:count]]
            result.append((item, points))
        return result

    def _draw_gradient_handles(self, snapshot: Gtk.Snapshot, view: View, accent: Gdk.RGBA) -> None:
        if not self.gradient_editor:
            return
        for _item, points in self._gradient_handle_points(view):
            if len(points) < 2:
                continue
            line = Gsk.PathBuilder.new()
            line.move_to(*points[0])
            line.line_to(*points[1])
            if len(points) > 2:
                line.move_to(*points[0])
                line.line_to(*points[2])
            path = line.to_path()
            snapshot.append_stroke(path, Gsk.Stroke.new(3.0), _black(0.25))
            snapshot.append_stroke(path, Gsk.Stroke.new(1.5), _white())
            for index, (hx, hy) in enumerate(points):
                self._handle_square(snapshot, hx, hy, accent, round_handle=True, size=10 if index < 2 else 8)

    def _draw_snapping(self, snapshot: Gtk.Snapshot, view: View) -> None:
        red = rgba(GUIDE_RED)
        builder = Gsk.PathBuilder.new()
        drew = False
        for guide in self._snap_guides:
            if guide.orientation == "vertical":
                x = round(view.to_view(guide.position, 0)[0]) + 0.5
                builder.move_to(x, view.to_view(0, guide.start)[1])
                builder.line_to(x, view.to_view(0, guide.end)[1])
                marks = [(x, view.to_view(0, mark)[1]) for mark in guide.marks]
            else:
                y = round(view.to_view(0, guide.position)[1]) + 0.5
                builder.move_to(view.to_view(guide.start, 0)[0], y)
                builder.line_to(view.to_view(guide.end, 0)[0], y)
                marks = [(view.to_view(mark, 0)[0], y) for mark in guide.marks]
            if guide.kind != "spacing":
                for mx, my in marks:
                    builder.move_to(mx - 2.5, my - 2.5)
                    builder.line_to(mx + 2.5, my + 2.5)
                    builder.move_to(mx - 2.5, my + 2.5)
                    builder.line_to(mx + 2.5, my - 2.5)
            drew = True
        if drew:
            snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(1.0), red)
        if self._endpoint_snap is not None:
            self._handle_square(snapshot, *self._endpoint_snap, red, round_handle=True, size=11)
        for measurement in [*self._snap_measurements, *self._hover_measurements]:
            self._draw_measurement(snapshot, view, measurement, red)

    def _draw_measurement(self, snapshot: Gtk.Snapshot, view: View, measurement: Measurement,
                          red: Gdk.RGBA) -> None:
        x1, y1 = view.to_view(measurement.x1, measurement.y1)
        x2, y2 = view.to_view(measurement.x2, measurement.y2)
        builder = Gsk.PathBuilder.new()
        if abs(y2 - y1) < 0.5:
            y = round(y1) + 0.5
            builder.move_to(x1, y)
            builder.line_to(x2, y)
            for x in (x1, x2):
                builder.move_to(round(x) + 0.5, y - 4)
                builder.line_to(round(x) + 0.5, y + 4)
        else:
            x = round(x1) + 0.5
            builder.move_to(x, y1)
            builder.line_to(x, y2)
            for y in (y1, y2):
                builder.move_to(x - 4, round(y) + 0.5)
                builder.line_to(x + 4, round(y) + 0.5)
        snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(1.0), red)
        self._draw_badge(snapshot, measurement.label, (x1 + x2) / 2, (y1 + y2) / 2, red)

    # ── hit testing ──────────────────────────────────────────────────────

    def _tree(self) -> geometry.Tree:
        """The layer tree. Hit testing asks on every pointer motion and drawing
        on every frame, so it is rebuilt only when layers are added, removed,
        reordered or reparented (two list comparisons, not a new tree)."""
        objects = self.document.objects
        ids = [item.id for item in objects]
        parents = [item.parent_id for item in objects]
        cached = getattr(self, "_tree_cache", None)
        if cached is not None and cached[0] is objects and cached[1] == ids and cached[2] == parents:
            return cached[3]
        tree = geometry.Tree(objects)
        self._tree_cache = (objects, ids, parents, tree)
        return tree

    def _tolerance(self) -> float:
        # Six screen pixels, so thin lines are easy to grab.
        return 6.0 / max(self.document.zoom, 1e-6)

    def _hit_chain(self, view_x: float, view_y: float) -> tuple[geometry.Tree, list[LayoutObject]]:
        tree = self._tree()
        wx, wy = self.view_to_world(view_x, view_y)
        return tree, geometry.hit_chain(tree, wx, wy, tolerance=self._tolerance())

    def _hit(self, view_x: float, view_y: float) -> LayoutObject | None:
        """The layer a click here would select (presentation and menus use this)."""
        tree, chain = self._hit_chain(view_x, view_y)
        return geometry.click_target(tree, chain, self.document.selected_ids)

    def _handle_at(self, x: float, y: float) -> tuple[str, Any] | None:
        """A transform handle under a view point. Handles win over objects."""
        if self.tool not in {"select", "direct", "scale"} or self._dropper:
            return None
        if self.gradient_editor:
            for item, points in self._gradient_handle_points(self.view):
                for index, (hx, hy) in enumerate(points):
                    if math.hypot(hx - x, hy - y) <= HANDLE_HIT:
                        return ("gradient", (item.id, index))
        frame = self._selection_frame()
        if frame is None:
            return None
        item = frame["item"]
        if self._is_line(item) and self.tool != "scale":
            ends = [self.world_to_view(*geometry.to_world(item, entry["x"], entry["y"])) for entry in item.path[:2]]
            for index, (hx, hy) in enumerate(ends):
                if abs(hx - x) <= HANDLE_HIT and abs(hy - y) <= HANDLE_HIT:
                    return ("endpoint", index)
            for index, (hx, hy) in enumerate(ends):
                other = ends[1 - index]
                beyond = (x - hx) * (hx - other[0]) + (y - hy) * (hy - other[1]) > 0
                if math.hypot(hx - x, hy - y) <= ROTATE_ZONE + HANDLE_HIT / 2 and beyond:
                    return ("rotate", f"p{index}")
            return None
        corners = frame["corners"]
        box_w = math.dist(corners[0], corners[1])
        box_h = math.dist(corners[1], corners[2])
        if item is not None and item.kind == "rectangle" and box_w >= 40 and box_h >= 40:
            for index, (hx, hy) in enumerate(self._radius_handle_points(item, self.view)):
                if math.hypot(hx - x, hy - y) <= 6:
                    return ("radius", index)
        handles = self._handle_points(corners)
        for name in ("nw", "ne", "se", "sw"):
            hx, hy = handles[name]
            if abs(hx - x) <= HANDLE_HIT and abs(hy - y) <= HANDLE_HIT:
                return ("resize", name)
        # Edges resize along their whole length within a 6px band.
        edges = {"n": (corners[0], corners[1]), "e": (corners[1], corners[2]),
                 "s": (corners[2], corners[3]), "w": (corners[3], corners[0])}
        for name, (a, b) in edges.items():
            if geometry._distance_to_segment((x, y), a, b) <= 3.0:
                return ("resize", name)
        inside = geometry._winding((x, y), list(corners)) != 0
        if not inside:
            for name in ("nw", "ne", "se", "sw"):
                hx, hy = handles[name]
                if math.hypot(hx - x, hy - y) <= ROTATE_ZONE + HANDLE_HIT / 2:
                    return ("rotate", name)
        return None

    # ── cursors ──────────────────────────────────────────────────────────

    def _update_cursor(self, handle: tuple[str, Any] | None = None) -> None:
        if self._mode == "pan":
            self.set_cursor_from_name("grabbing")
            return
        if self._space_held or self.tool == "hand":
            self.set_cursor_from_name("grab")
            return
        if self._dropper:
            self.set_cursor_from_name("crosshair")
            return
        if handle is not None:
            kind, value = handle
            frame = self._selection_frame()
            rotation = frame["rotation"] if frame else 0.0
            if kind == "resize":
                base = {"n": 90, "ne": 45, "e": 0, "se": 135, "s": 90, "sw": 45, "w": 0, "nw": 135}[value]
                angle = (base + rotation) % 180
                name = ("ew-resize" if angle < 22.5 or angle >= 157.5 else
                        "nwse-resize" if angle >= 112.5 else
                        "ns-resize" if angle >= 67.5 else "nesw-resize")
                self.set_cursor_from_name(name)
                return
            if kind == "rotate":
                if value in {"p0", "p1"} and frame and frame["item"] is not None and frame["item"].path:
                    line = frame["item"]
                    a, b = line.path[0], line.path[1]
                    if value == "p0":
                        a, b = b, a
                    base = math.degrees(math.atan2(b["y"] - a["y"], b["x"] - a["x"])) + 45
                    self.set_cursor(self._rotate_cursor(int(round(base / 45.0)) % 8 * 45))
                    return
                base = {"ne": 0, "se": 90, "sw": 180, "nw": 270}.get(value, 0)
                self.set_cursor(self._rotate_cursor(int(round((base + rotation) / 45.0)) % 8 * 45))
                return
            self.set_cursor_from_name("pointer")
            return
        if self.tool in DRAW_TOOLS or self.tool == "path":
            self.set_cursor_from_name("text" if self.tool == "text" else "crosshair")
            return
        self.set_cursor_from_name(CURSORS.get(self.tool, "default"))

    def _rotate_cursor(self, degrees: int) -> Gdk.Cursor:
        key = f"rotate-{degrees}"
        cursor = self._cursor_cache.get(key)
        if cursor is not None:
            return cursor
        size = 24
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        cr.translate(size / 2, size / 2)
        cr.rotate(math.radians(degrees))
        # A quarter arc with an arrowhead at each end, curving around the
        # corner the pointer is outside of (drawn for the top-right corner).
        for width, color in ((3.5, (1, 1, 1, 1)), (1.5, (0.08, 0.08, 0.1, 1))):
            cr.set_line_width(width)
            cr.set_source_rgba(*color)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.arc(-4, 4, 9, -math.pi / 2, 0)
            cr.stroke()
            for tip_x, tip_y, a, b in ((-4, -5, (-1.5, -8.5), (-1.5, -1.5)), (5, 4, (1.5, 1.5), (8.5, 1.5))):
                cr.move_to(*a)
                cr.line_to(tip_x, tip_y)
                cr.line_to(*b)
                cr.stroke()
        surface.flush()
        texture = Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                        GLib.Bytes.new(bytes(surface.get_data())), surface.get_stride())
        cursor = Gdk.Cursor.new_from_texture(texture, size // 2, size // 2,
                                             Gdk.Cursor.new_from_name("default", None))
        self._cursor_cache[key] = cursor
        return cursor

    # ── pointer: press ───────────────────────────────────────────────────

    def _drag_begin(self, gesture: Any, x: float, y: float) -> None:
        self._stop_kinetic()
        self._stop_animation()
        self._focus_soon()
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        shift, ctrl, alt = _modifiers(state)
        now = time.monotonic()
        settings = Gtk.Settings.get_default()
        double_time = (settings.props.gtk_double_click_time if settings else 400) / 1000
        double_distance = settings.props.gtk_double_click_distance if settings else 5
        last_time, last_x, last_y, last_count = self._last_click
        repeat = now - last_time <= double_time and math.hypot(x - last_x, y - last_y) <= double_distance
        count = last_count + 1 if repeat and button == 1 else 1
        self._last_click = (now, x, y, count)
        self._pointer = (x, y)
        self._press = {"button": button, "x": x, "y": y, "count": count,
                       "world": self.view_to_world(x, y), "view": self.view}
        self._mode = None

        if button == 2 or (button == 1 and (self._space_held or self.tool == "hand")):
            self._mode = "pan"
            self._update_cursor()
            return
        if button == 3:
            self._press = None
            self._context_pressed(x, y)
            return
        if button != 1:
            self._press = None
            return
        ruler = self._ruler_at(x, y)
        if ruler is not None:
            if ruler in {"top", "left"}:
                self._press["pending"] = "guide_new"
                self._press["axis"] = "y" if ruler == "top" else "x"
            else:
                self._press = None
            return
        if self._text is not None:
            if self._text_press(x, y, count, shift):
                self._press["pending"] = "text_select"
                return
            self.end_text_edit()
        guide = self._guide_at(x, y) if self.tool == "select" and self._vector is None and not self._dropper else None
        if guide is not None:
            self._press["pending"] = "guide_move"
            self._press["guide"] = guide.id
            self._press["axis"] = rulers.guide_axis(guide)
            self._guide_selected = guide.id
            self._queue_view()
            return
        if self._guide_selected is not None:
            self._guide_selected = None
            self._queue_view()
        if self._dropper:
            self._press["dropper"] = True
            return
        if self.tool == "path":
            self._pen_pressed(x, y, count, shift)
            self._mode = "pen"
            return
        if self.tool == "comment":
            self._press = None
            self.emit("comment-requested", x, y)
            return
        if self.tool == "image" and not self.document.assets:
            self._press = None
            self.emit("image-needed", x, y)
            return
        if self.tool in DRAW_TOOLS:
            self._press["pending"] = "draw"
            return
        if self.tool == "zoom":
            self._press["pending"] = "zoom"
            return
        if count >= 2 and self.tool in {"select", "scale"}:
            label_item = self._label_at(x, y)
            if label_item is not None:
                self._press = None
                self.begin_rename(label_item)
                return

        if self._vector is not None:
            if self._vector_hit(x, y) is not None or self._press_on_vector_item(x, y):
                self._vector_press(x, y, count, shift)
                return
            # Anywhere else: a drag selects points, a click leaves edit mode.
            self._press["pending"] = "vector_marquee"
            self._press["leave_vector"] = True
            self._press["base_nodes"] = set(self._vector["selected"]) if shift else set()
            return
        # Move, scale and direct selection. Handles have priority over everything.
        if self.tool == "direct" and count == 1:
            tree, chain = self._hit_chain(x, y)
            leaf = chain[-1] if chain else None
            if self.can_edit_vector(leaf):
                self.begin_vector_edit(leaf)
                self._vector_press(x, y, count, shift)
                return
        handle = self._handle_at(x, y)
        if handle is not None:
            self._press["pending"] = handle[0]
            self._press["handle"] = handle[1]
            return
        label = self._label_at(x, y)
        tree, chain = self._hit_chain(x, y)
        target = label or geometry.click_target(tree, chain, self.document.selected_ids, deep=ctrl)
        if (count == 1 and not shift and not ctrl and label is None and self.document.selected_ids
                and self._inside_selection(x, y) and (target is None or target.id not in self.document.selected_ids)):
            # Figma: a drag that starts inside the selection moves the
            # selection; a click without dragging selects what is under it.
            self._press["pending"] = "move"
            self._press["click_select"] = target.id if target is not None else None
            return
        if count >= 2 and chain and not ctrl and label is None:
            leaf = chain[-1]
            if leaf.kind == "text" and not leaf.locked:
                # Figma: a double-click edits text that is already in scope; on a
                # group it first enters the group.
                if leaf.id in self.document.selected_ids or (target is not None and target.id == leaf.id):
                    self._press = None
                    self.begin_text_edit(leaf, x, y, select_word=True)
                    return
            entered = self._enter(tree, chain)
            if self._vector is not None:
                self._press = None
                return
            if entered is not None:
                target = entered
        selected = list(self.document.selected_ids)
        if target is None:
            if not shift:
                self._select([])
            self._press["pending"] = "marquee"
            self._press["base_selection"] = list(self.document.selected_ids) if shift else []
            return
        if shift:
            if target.id in selected:
                selected.remove(target.id)
                self._select(selected)
                return
            self._select([*selected, target.id])
        elif target.id not in selected:
            self._select([target.id])
        elif len(selected) > 1:
            # A click on part of a multiple selection narrows to it, unless it
            # turns into a drag of the whole selection.
            self._press["narrow_to"] = target.id
        self._press["pending"] = "move"

    def _inside_selection(self, x: float, y: float) -> bool:
        frame = self._selection_frame()
        if frame is None:
            return False
        return geometry._winding((x, y), list(frame["corners"])) != 0

    def _enter(self, tree: geometry.Tree, chain: list[LayoutObject]) -> LayoutObject | None:
        """Double-click: enter the selected container, or edit a path's nodes."""
        selection = self.document.selection
        if len(selection) != 1:
            return None
        current = selection[0]
        if current.kind in {"path", "line"} and len(current.path or []) > 2 or current.kind == "path":
            self.begin_vector_edit(current)
            return current
        return geometry.child_under(tree, current, chain)

    def _select(self, ids: list[str]) -> None:
        before = list(self.document.selected_ids)
        self.document.select(ids)
        if before != self.document.selected_ids:
            self.emit("selection-changed")
            self.emit("style-changed")
        self._queue_view()

    # ── pointer: drag ────────────────────────────────────────────────────

    def _drag_update(self, gesture: Any, offset_x: float, offset_y: float) -> None:
        if self._press is None:
            return
        x, y = self._press["x"] + offset_x, self._press["y"] + offset_y
        self._pointer = (x, y)
        self._route_drag(x, y, gesture.get_current_event_state())

    def _route_drag(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        press = self._press
        if press is None:
            return
        if self._mode == "pan":
            start: View = press["view"]
            self._set_view(start.panned(x - press["x"], y - press["y"]))
            return
        if self._mode == "pen":
            self._pen_motion(x, y, state)
            return
        if press.get("dropper"):
            self._dropper_motion(x, y)
            return
        pending = press.get("pending")
        if self._mode is None and pending:
            if math.hypot(x - press["x"], y - press["y"]) < DRAG_THRESHOLD:
                return
            self._activate(pending, state)
        if press.get("pending") == "text_select":
            self._text_drag(x, y)
            return
        handlers = {"move": self._move_to, "marquee": self._marquee_to, "resize": self._resize_to,
                    "rotate": self._rotate_to, "draw": self._draw_to, "radius": self._radius_to,
                    "endpoint": self._endpoint_to, "vector_nodes": self._vector_nodes_to,
                    "vector_handle": self._vector_handle_to, "vector_marquee": self._vector_marquee_to,
                    "guide_new": self._guide_to, "guide_move": self._guide_to,
                    "gradient": self._gradient_to, "zoom": self._zoom_marquee_to}
        handler = handlers.get(self._mode)
        if handler is not None:
            handler(x, y, state)
            if self._mode in self.AUTOSCROLL_MODES:
                self._autoscroll_follow(x, y, state)

    def _activate(self, pending: str, state: Gdk.ModifierType) -> None:
        press = self._press
        _, _, alt = _modifiers(state)
        self._mode = pending
        items = [item for item in self.document.selection if not item.locked]
        names = items[0].name if len(items) == 1 else f"{len(items)} layers"
        if pending in {"move", "resize", "rotate", "radius", "endpoint"} and not items:
            self._mode = None
            return
        if pending == "move":
            self.editor.begin_gesture(f"Move {names}")
            press["origins"] = self._origins(items)
            press["moving"] = [item.id for item in items]
            press["start_bounds"] = geometry.union_bounds(items)
            if alt:
                self._duplicate_for_drag()
        elif pending in {"marquee", "zoom"}:
            self._marquee = (*press["world"], *press["world"])
        elif pending == "resize":
            self.editor.begin_gesture(f"Resize {names}")
            press["origins"] = self._origins(items)
            press["roots"] = [item.id for item in items]
            press["union"] = geometry.union_bounds(items)
        elif pending == "rotate":
            self.editor.begin_gesture(f"Rotate {names}")
            press["origins"] = self._origins(items)
            press["roots"] = [item.id for item in items]
            bounds = geometry.union_bounds(items)
            press["center"] = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
        elif pending == "draw":
            self._begin_draw(state)
        elif pending == "radius":
            self.editor.begin_gesture(f"Change radius of {items[0].name}")
            press["item"] = items[0].id
        elif pending == "endpoint":
            item = items[0]
            self.editor.begin_gesture(f"Edit {item.name}")
            press["item"] = item.id
            press["path"] = [dict(entry) for entry in item.path]
            press["box"] = (item.x, item.y)
        elif pending in {"vector_nodes", "vector_handle"}:
            item = self._vector_item()
            self.editor.begin_gesture(f"Edit {item.name}" if item is not None else "Edit vector")
        elif pending == "vector_marquee":
            self._marquee = (*press["world"], *press["world"])
        elif pending == "guide_new":
            self.editor.begin_gesture("Add guide")
            wx, wy = press["world"]
            axis = press["axis"]
            guide = LayoutObject(new_id("guide"), "guide", "Guide", None, wx if axis == "x" else 0.0,
                                 wy if axis == "y" else 0.0, 0.0, 0.0, fill=None, metadata={"axis": axis})
            self.document.objects.append(guide)
            press["guide"] = guide.id
            self._guide_selected = guide.id
        elif pending == "guide_move":
            self.editor.begin_gesture("Move guide")
        elif pending == "gradient":
            begin = (self.gradient_editor or {}).get("begin")
            if begin:
                begin()
            else:
                self.editor.begin_gesture("Change fill")

    def _origins(self, items: list[LayoutObject]) -> dict[str, dict[str, Any]]:
        return {item.id: {"x": item.x, "y": item.y, "width": item.width, "height": item.height,
                          "rotation": item.rotation, "flip_h": item.flip_h, "flip_v": item.flip_v,
                          "path": [dict(entry) for entry in item.path]}
                for item in self.editor.with_descendants(items)}

    def _duplicate_for_drag(self) -> None:
        """Alt when a move starts or during it: the originals stay, copies move."""
        press = self._press
        if press is None or press.get("duplicated"):
            return
        for object_id, origin in press["origins"].items():
            item = self.document.object(object_id)
            if item is not None:
                item.x, item.y = origin["x"], origin["y"]
        try:
            self.editor.duplicate(press["moving"], offset=0.0, rename=False)
        except EditError:
            return
        roots = [self.document.object(object_id) for object_id in self.document.selected_ids]
        roots = [item for item in roots if item is not None]
        press["origins"] = self._origins(roots)
        press["moving"] = [item.id for item in roots]
        press["duplicated"] = True
        self.editor.set_gesture_label(f"Duplicate {roots[0].name}" if len(roots) == 1 else "Duplicate layers")
        self.emit("selection-changed")

    def _move_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        """Move from the drag-start positions by the pointer delta; the shape snaps."""
        press = self._press
        shift, ctrl, alt = _modifiers(state)
        if alt and not press.get("duplicated"):
            self._duplicate_for_drag()
        start_x, start_y = press["world"]
        wx, wy = self.view_to_world(x, y)
        dx, dy = wx - start_x, wy - start_y
        axes = "xy"
        if shift:
            dx, dy = geometry.constrain_move(dx, dy)
            axes = "x" if abs(dy) < 1e-9 else "y" if abs(dx) < 1e-9 else ""
        origins = press["origins"]
        for object_id, origin in origins.items():
            item = self.document.object(object_id)
            if item is not None:
                item.x, item.y = origin["x"], origin["y"]
        if self.snap and not ctrl and axes:
            engine = self._engine()
            result = engine.snap_move(set(press["moving"]), dx, dy, axes=axes)
            raw_dx, raw_dy = dx, dy
            dx, dy = result.dx, result.dy
            # Hysteresis: once a snap holds, it lets go only past 1.5 times the
            # threshold, so the shape does not flicker between nearby lines.
            release = engine.threshold * 1.5
            held = press.setdefault("held", {})
            for axis, raw, value in (("x", raw_dx, dx), ("y", raw_dy, dy)):
                pixel_only = abs(value - raw) < 0.5 and abs(value - round(value)) < 1e-9
                if abs(value - raw) > 1e-9 and not pixel_only:
                    held[axis] = value
                elif axis in held and abs(held[axis] - raw) <= release:
                    if axis == "x":
                        dx = held["x"]
                    else:
                        dy = held["y"]
                else:
                    held.pop(axis, None)
            if (dx, dy) != (result.dx, result.dy):
                bounds = press.get("start_bounds")
                from .snapping import Box
                if bounds is not None:
                    settled = Box(bounds[0] + dx, bounds[1] + dy, bounds[2] - bounds[0], bounds[3] - bounds[1])
                    peers = engine._peers(set(press["moving"]))
                    result.guides = engine.alignment_guides(settled, peers)
            self._snap_guides, self._snap_measurements = result.guides, result.measurements
        else:
            self._snap_guides, self._snap_measurements = [], []
        for object_id, origin in origins.items():
            item = self.document.object(object_id)
            if item is not None:
                item.x, item.y = origin["x"] + dx, origin["y"] + dy
        self._reparent_target = None
        if not ctrl:
            target = self._drop_frame(x, y, set(origins))
            # A layer's place is its nearest frame: moving it inside its group or
            # boolean keeps it there (Figma); only another frame takes it in.
            parents = {self._frame_of(object_id) for object_id in press["moving"]}
            if parents != {target}:
                self._reparent_target = target
        self._size_badge()
        self.queue_draw()
        self.emit("transform-preview")

    def _frame_of(self, object_id: str) -> str:
        for ancestor in self.document.ancestors(object_id):
            if ancestor.kind in geometry.FRAME_LIKE:
                return ancestor.id
        return "__page__"

    def _drop_frame(self, x: float, y: float, excluded: set[str]) -> str:
        """The deepest visible frame under a view point, or "__page__"."""
        wx, wy = self.view_to_world(x, y)
        found = "__page__"
        for item in self._tree().paint_order():
            if item.id in excluded or not item.visible or item.kind not in geometry.FRAME_LIKE:
                continue
            lx, ly = geometry.to_local(item, wx, wy)
            if 0 <= lx <= item.width and 0 <= ly <= item.height:
                found = item.id
        return found

    def _marquee_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        shift, ctrl, alt = _modifiers(state)
        start = self._press["world"]
        end = self.view_to_world(x, y)
        self._marquee = (start[0], start[1], end[0], end[1])
        found = geometry.marquee_selection(self._tree(), self._marquee, enclose=alt, deep=ctrl)
        base = self._press.get("base_selection", [])
        self._select([*base, *[value for value in found if value not in base]])

    def _zoom_marquee_to(self, x: float, y: float, _state: Gdk.ModifierType) -> None:
        self._marquee = (*self._press["world"], *self.view_to_world(x, y))
        self._queue_view()

    def _resize_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        press = self._press
        shift, ctrl, alt = _modifiers(state)
        handle = press["handle"]
        origins = press["origins"]
        roots = press["roots"]
        wx, wy = self.view_to_world(x, y)
        axes = ("x" if ("w" in handle or "e" in handle) else "") + ("y" if ("n" in handle or "s" in handle) else "")
        self._snap_guides = []
        for object_id, origin in origins.items():
            item = self.document.object(object_id)
            if item is not None:
                self._restore(item, origin)
        if len(roots) == 1 and not self.document.children(roots[0]):
            item = self.document.object(roots[0])
            origin = origins[item.id]
            keep = shift != bool(item.metadata.get("aspect_locked", False))
            if self.snap and not ctrl and not item.rotation:
                wx, wy, self._snap_guides = self._engine().snap_point({item.id}, wx, wy, axes=axes)
            if self.tool == "scale":
                keep = not shift
            box = geometry.resize_item_box(item, handle, wx, wy, keep_aspect=keep, from_center=alt)
            self._apply_box(item, origin, box)
            if self.tool == "scale":
                factor = math.sqrt(abs((box.width / (origin["width"] or 1)) * (box.height / (origin["height"] or 1))))
                self._scale_properties(item, origin, factor)
            elif item.kind == "text":
                self._text_box_resized(item, origin, handle)
        else:
            left, top, right, bottom = press["union"]
            width, height = max(right - left, 1e-6), max(bottom - top, 1e-6)
            if len(roots) == 1:
                root = self.document.object(roots[0])
                left, top, right, bottom = root.x, root.y, root.x + root.width, root.y + root.height
                width, height = max(root.width, 1e-6), max(root.height, 1e-6)
            if self.snap and not ctrl:
                wx, wy, self._snap_guides = self._engine().snap_point(set(roots), wx, wy, axes=axes)
            nl, nt, nr, nb, flip_x, flip_y = geometry.resize_local(
                width, height, handle, wx - left, wy - top,
                keep_aspect=(not shift) if self.tool == "scale" else shift, from_center=alt)
            scale_x, scale_y = (nr - nl) / width, (nb - nt) / height
            frame_resize = (len(roots) == 1 and self.document.object(roots[0]).kind in geometry.FRAME_LIKE
                            and self.tool != "scale")
            if frame_resize:
                self._resize_frame(roots[0], origins, (left + nl, top + nt, nr - nl, nb - nt), ignore=ctrl)
                self._size_badge()
                self.queue_draw()
                self.emit("transform-preview")
                return
            scaling = self.tool == "scale"
            for object_id, origin in origins.items():
                item = self.document.object(object_id)
                if item is None:
                    continue
                nx = left + nl + (origin["x"] - left) * scale_x
                ny = top + nt + (origin["y"] - top) * scale_y
                if flip_x:
                    nx = left + nl + (right - origin["x"] - origin["width"]) * scale_x
                if flip_y:
                    ny = top + nt + (bottom - origin["y"] - origin["height"]) * scale_y
                self._apply_box(item, origin, geometry.Box(
                    nx, ny, origin["width"] * scale_x, origin["height"] * scale_y,
                    origin["flip_h"] ^ flip_x, origin["flip_v"] ^ flip_y))
                if scaling:
                    self._scale_properties(item, origin, math.sqrt(abs(scale_x * scale_y)))
        self._size_badge()
        self.queue_draw()
        self.emit("transform-preview")

    def _resize_frame(self, frame_id: str, origins: dict[str, dict[str, Any]],
                      box: tuple[float, float, float, float], *, ignore: bool) -> None:
        """Resize a frame; its children follow their constraints (Ctrl ignores them)."""
        frame = self.document.object(frame_id)
        origin = origins[frame_id]
        frame.x, frame.y, frame.width, frame.height = box
        old_frame = (origin["x"], origin["y"], origin["width"], origin["height"])
        if ignore:
            return
        children = [child for child in self.document.children(frame_id) if child.id in origins]
        for child in children:
            start = origins[child.id]
            constraints = child.constraints or {}
            x, y, width, height = geometry.constrained_box(
                (start["x"], start["y"], start["width"], start["height"]), old_frame, box,
                str(constraints.get("horizontal", "left")), str(constraints.get("vertical", "top")))
            child.x, child.y = x, y
            if abs(width - start["width"]) > 1e-9 or abs(height - start["height"]) > 1e-9:
                child.width, child.height = width, height
                if start["path"]:
                    child.path = geometry.scale_path(start["path"], width / (start["width"] or 1),
                                                     height / (start["height"] or 1))
            dx, dy = x - start["x"], y - start["y"]
            for descendant in self.document.descendants(child.id):
                if descendant.id in origins:
                    descendant.x = origins[descendant.id]["x"] + dx
                    descendant.y = origins[descendant.id]["y"] + dy

    def _scale_properties(self, item: LayoutObject, origin: dict[str, Any], factor: float) -> None:
        """Scale tool (K): stroke, radii, text size and effects scale with the layer."""
        props = origin.setdefault("props", {
            "stroke_width": item.stroke_width, "radius": item.radius, "corner_radii": list(item.corner_radii),
            "font_size": item.metadata.get("font_size"), "effects": [dict(effect) for effect in item.effects],
            "dash": list(item.stroke_dash)})
        item.stroke_width = props["stroke_width"] * factor
        item.radius = props["radius"] * factor
        item.corner_radii = [value * factor for value in props["corner_radii"]]
        item.stroke_dash = [value * factor for value in props["dash"]]
        if props["font_size"] is not None:
            item.metadata = {**item.metadata, "font_size": round(float(props["font_size"]) * factor, 2)}
        effects = []
        for effect in props["effects"]:
            scaled = dict(effect)
            for key in ("x", "y", "blur", "spread", "radius"):
                if isinstance(scaled.get(key), (int, float)):
                    scaled[key] = scaled[key] * factor
            effects.append(scaled)
        item.effects = effects

    def _text_box_resized(self, item: LayoutObject, origin: dict[str, Any], handle: str) -> None:
        """Figma's text resizing: pulling the height makes the box fixed, pulling
        an auto width box's width makes it auto height; the font never scales."""
        sizing = origin.setdefault("sizing", type_settings(item.metadata)["sizing"])
        if "n" in handle or "s" in handle:
            new = "fixed"
        elif sizing == "auto_width":
            new = "auto_height"
        else:
            new = sizing
        if type_settings(item.metadata)["sizing"] != new:
            item.metadata = {**item.metadata, "sizing": new}
        if new == "auto_height":
            size = fitted_size(self.renderer._pango_context(), item)
            if size is not None:
                item.height = size[1]

    @staticmethod
    def _restore(item: LayoutObject, origin: dict[str, Any]) -> None:
        item.x, item.y, item.width, item.height = origin["x"], origin["y"], origin["width"], origin["height"]
        item.flip_h, item.flip_v, item.rotation = origin["flip_h"], origin["flip_v"], origin["rotation"]
        item.path = [dict(entry) for entry in origin["path"]]

    @staticmethod
    def _apply_box(item: LayoutObject, origin: dict[str, Any], box: geometry.Box) -> None:
        scale_x = box.width / origin["width"] if origin["width"] else 1.0
        scale_y = box.height / origin["height"] if origin["height"] else 1.0
        item.x, item.y, item.width, item.height = box.x, box.y, box.width, box.height
        item.flip_h, item.flip_v = box.flip_h, box.flip_v
        if origin["path"]:
            item.path = geometry.scale_path(origin["path"], scale_x, scale_y)

    def _rotate_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        press = self._press
        shift, _, _ = _modifiers(state)
        center = press["center"]
        delta = geometry.rotation_delta(center, press["world"], self.view_to_world(x, y))
        origins = press["origins"]
        roots = press["roots"]
        if len(roots) == 1:
            origin = origins[roots[0]]
            angle = origin["rotation"] + delta
            if shift:
                angle = snap_rotation(angle, constrain=True)
            delta = angle - origin["rotation"]
        elif shift:
            delta = snap_rotation(delta, constrain=True)
        if len(roots) == 1 and shift:
            only = self.document.object(roots[0])
            origin = origins[roots[0]]
            if only is not None and only.kind == "line" and len(origin["path"]) == 2 and not origin["rotation"]:
                a, b = origin["path"]
                start = math.degrees(math.atan2(b["y"] - a["y"], b["x"] - a["x"]))
                delta = snap_rotation(start + delta, constrain=True) - start
        for object_id, origin in origins.items():
            item = self.document.object(object_id)
            if item is None:
                continue
            if item.kind == "line" and len(origin["path"]) == 2 and not origin["rotation"]:
                # A line turns by its ends, so it stays a two-node line with
                # its box hugging it.
                ends = [geometry.rotate_point((origin["x"] + float(node_["x"]), origin["y"] + float(node_["y"])),
                                              center, delta) for node_ in origin["path"]]
                self._set_line(item, ends[0], ends[1])
                continue
            ocx, ocy = origin["x"] + origin["width"] / 2, origin["y"] + origin["height"] / 2
            ncx, ncy = geometry.rotate_point((ocx, ocy), center, delta)
            item.x, item.y = ncx - origin["width"] / 2, ncy - origin["height"] / 2
            item.rotation = geometry.normalize_angle(origin["rotation"] + delta)
        single = self.document.object(roots[0]) if len(roots) == 1 else None
        if single is not None and single.kind == "line" and len(single.path) == 2 and not single.rotation:
            a, b = single.path
            shown = -math.degrees(math.atan2(b["y"] - a["y"], b["x"] - a["x"]))
        else:
            shown = single.rotation if single is not None else geometry.normalize_angle(delta)
        self._badge = (f"{format_number(shown, 1)}°", x + 18, y + 18)
        self.queue_draw()
        self.emit("transform-preview")

    def _radius_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        _, _, alt = _modifiers(state)
        item = self.document.object(self._press["item"])
        if item is None:
            return
        corner = self._press["handle"]
        value = geometry.radius_from_handle(item, corner, *self.view_to_world(x, y))
        value = float(round(value)) if self.pixel_snap else round(value, 2)
        if alt:
            radii = list(geometry.corner_radii_of(item))
            radii[corner] = value
            item.corner_radii = radii
        else:
            item.radius = value
            item.corner_radii = []
        frame = self._selection_frame()
        bottom = max(corner_[1] for corner_ in frame["corners"])
        center_x = sum(corner_[0] for corner_ in frame["corners"]) / 4
        self._badge = (f"Radius {format_number(value)}", center_x, bottom + 8)
        self.queue_draw()
        self.emit("transform-preview")

    def _endpoint_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        press = self._press
        shift, ctrl, _ = _modifiers(state)
        item = self.document.object(press["item"])
        if item is None:
            return
        bx, by = press["box"]
        index = press["handle"]
        other = press["path"][1 - index]
        ox, oy = bx + float(other["x"]), by + float(other["y"])
        wx, wy = self.view_to_world(x, y)
        self._snap_guides = []
        if shift:
            dx, dy = geometry.constrain_angle(wx - ox, wy - oy, 45.0)
            wx, wy = ox + dx, oy + dy
            if self.snap and not ctrl:
                wx, wy, self._snap_guides = self._engine().snap_on_ray({item.id}, (ox, oy), wx, wy,
                                                                       parent_id=item.parent_id)
        elif self.snap and not ctrl:
            wx, wy, self._snap_guides = self._engine().snap_point({item.id}, wx, wy)
        self._endpoint_snap = self.world_to_view(wx, wy) if self._snap_guides else None
        start, end = ((ox, oy), (wx, wy)) if index == 1 else ((wx, wy), (ox, oy))
        self._set_line(item, start, end)
        self._size_badge()
        self.queue_draw()

    @staticmethod
    def _set_line(item: LayoutObject, start: tuple[float, float], end: tuple[float, float]) -> None:
        left, top = min(start[0], end[0]), min(start[1], end[1])
        item.x, item.y = left, top
        item.width, item.height = abs(end[0] - start[0]), abs(end[1] - start[1])
        item.rotation = 0.0
        item.flip_h = item.flip_v = False
        item.path = [{"x": start[0] - left, "y": start[1] - top},
                     {"x": end[0] - left, "y": end[1] - top}]

    def _gradient_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        editor = self.gradient_editor
        if not editor:
            return
        shift, _, alt = _modifiers(state)
        item_id, index = self._press["handle"]
        item = self.document.object(item_id)
        target = editor.get("target", "fill")
        edited = self._edited_gradient(item, target, editor.get("index")) if item is not None else None
        if item is None or not edited:
            return
        wx, wy = self.view_to_world(x, y)
        if not alt and not shift:
            wx, wy = geometry.snap_gradient_point(self.document.objects, wx, wy, self.document.zoom)
        lx, ly = geometry.to_local(item, wx, wy)
        paint = dict(edited)
        handles = [list(value) for value in paint.get("handles") or [[0.5, 0], [0.5, 1], [1, 0.5]]]
        width, height = max(item.width, 1e-6), max(item.height, 1e-6)
        hx, hy = lx / width, ly / height
        if shift and index == 1 and paint.get("type") == "linear":
            sx, sy = handles[0]
            dx, dy = geometry.constrain_angle((hx - sx) * width, (hy - sy) * height, 15.0)
            hx, hy = sx + dx / width, sy + dy / height
        while len(handles) <= index:
            handles.append([1.0, 0.5])
        handles[index] = [hx, hy]
        paint["handles"] = handles
        change = editor.get("change")
        if change:
            change(paint)
        else:
            setattr(item, f"{target}_gradient", paint)
        self.queue_draw()

    # ── drawing shapes ───────────────────────────────────────────────────

    def _parent_for_new(self, wx: float, wy: float) -> str | None:
        frame = self._drop_frame(*self.world_to_view(wx, wy), set())
        return None if frame == "__page__" else frame

    def _shape_fields(self, tool: str, kind: str) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if kind == "line":
            fields["path"] = [{"x": 0.0, "y": 0.0}, {"x": 0.0, "y": 0.0}]
            if tool == "arrow":
                fields["end_cap"] = "line_arrow"
        elif kind == "polygon":
            fields["point_count"] = 3
        elif kind == "star":
            fields["point_count"], fields["inner_ratio"] = 5, 0.382
        elif kind == "text":
            fields["text"] = ""
        elif kind == "image":
            fields["asset_id"] = self.document.assets[0].id
            fields["fill"] = None
            fields["radius"] = 16
        return fields

    def _begin_draw(self, state: Gdk.ModifierType) -> None:
        press = self._press
        tool = self.tool
        kind = TOOL_KIND.get(tool, tool)
        wx, wy = press["world"]
        _, ctrl, _ = _modifiers(state)
        if self.snap and not ctrl:
            wx, wy, _ = self._engine().snap_point(set(), wx, wy, parent_id=self._parent_for_new(wx, wy))
        press["anchor"] = (wx, wy)
        name = "Arrow" if tool == "arrow" else None
        self.editor.begin_gesture(f"Create {name or kind.title()}")
        try:
            item = self.editor.create(kind, wx, wy, 0.0, 0.0, parent_id=self._parent_for_new(wx, wy),
                                      name=name, **self._shape_fields(tool, kind))
        except EditError:
            self.editor.cancel_gesture()
            self._mode = None
            return
        press["item"] = item.id
        self.emit("selection-changed")

    def _draw_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        press = self._press
        item = self.document.object(press.get("item"))
        if item is None:
            return
        shift, ctrl, alt = _modifiers(state)
        ax, ay = press["anchor"]
        wx, wy = self.view_to_world(x, y)
        self._snap_guides = []
        if self.snap and not ctrl and not shift:
            wx, wy, self._snap_guides = self._engine().snap_point({item.id}, wx, wy)
        elif self.snap and not ctrl and shift and item.kind == "line" and not self._space_held:
            dx, dy = geometry.constrain_angle(wx - ax, wy - ay, 45.0)
            wx, wy, self._snap_guides = self._engine().snap_on_ray({item.id}, (ax, ay), ax + dx, ay + dy,
                                                                   parent_id=item.parent_id)
            shift = False     # already constrained
        self._endpoint_snap = self.world_to_view(wx, wy) if item.kind == "line" and self._snap_guides else None
        if self._space_held:
            # Space during the drag moves the shape being drawn.
            last = press.get("last_world", (wx, wy))
            press["anchor"] = (ax + wx - last[0], ay + wy - last[1])
            ax, ay = press["anchor"]
        press["last_world"] = (wx, wy)
        if item.kind == "line":
            if shift:
                dx, dy = geometry.constrain_angle(wx - ax, wy - ay, 45.0)
                wx, wy = ax + dx, ay + dy
            start = (ax - (wx - ax), ay - (wy - ay)) if alt else (ax, ay)
            self._set_line(item, start, (wx, wy))
        else:
            dx, dy = wx - ax, wy - ay
            if shift:
                side = max(abs(dx), abs(dy))
                dx, dy = math.copysign(side, dx or 1), math.copysign(side, dy or 1)
            if alt:
                left, top, width, height = ax - abs(dx), ay - abs(dy), abs(dx) * 2, abs(dy) * 2
            else:
                left, top, width, height = min(ax, ax + dx), min(ay, ay + dy), abs(dx), abs(dy)
            item.x, item.y, item.width, item.height = left, top, max(width, 0.01), max(height, 0.01)
        self._size_badge()
        self.queue_draw()

    def _finish_draw(self, press: dict[str, Any], clicked: bool) -> None:
        tool = self.tool
        if tool == "text":
            self._finish_text_draw(press, clicked)
            return
        kind = TOOL_KIND.get(tool, tool)
        name = "Arrow" if tool == "arrow" else None
        if clicked:
            wx, wy = press["world"]
            if self.snap:
                wx, wy, _ = self._engine().snap_point(set(), wx, wy, parent_id=self._parent_for_new(wx, wy))
            fields = self._shape_fields(tool, kind)
            size = {"text": (180.0, 44.0), "frame": (320.0, 240.0), "line": (100.0, 0.0),
                    "image": (200.0, 200.0)}.get(kind, (100.0, 100.0))
            if kind == "line":
                fields["path"] = [{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 0.0}]
            try:
                item = self.editor.create(kind, wx, wy, *size, parent_id=self._parent_for_new(wx, wy),
                                          name=name, **fields)
            except EditError as error:
                print(f"Canvas could not create {kind}: {error}")
                return
        else:
            item = self.document.object(press.get("item"))
            if item is not None and kind != "line" and self.pixel_snap:
                right, bottom = round(item.x + item.width), round(item.y + item.height)
                item.x, item.y = round(item.x), round(item.y)
                item.width, item.height = max(1.0, right - item.x), max(1.0, bottom - item.y)
            self.editor.end_gesture()
        self.emit("selection-changed")
        self.emit("document-changed", f"Create {item.name if item else kind.title()}")
        if self.tool_locked:
            return
        self.tool = "select"
        self._update_cursor()
        self.emit("tool-changed", "select")

    # ── pointer: release ─────────────────────────────────────────────────

    def _drag_end(self, gesture: Any, offset_x: float, offset_y: float) -> None:
        press = self._press
        if press is None:
            return
        x, y = press["x"] + offset_x, press["y"] + offset_y
        state = gesture.get_current_event_state()
        shift, ctrl, alt = _modifiers(state)
        mode = self._mode
        self._press = None
        self._mode = None
        self._badge = None
        self._stop_autoscroll()
        if mode in {"guide_new", "guide_move"}:
            self._finish_guide(press, x, y)
            return
        if mode is None and press.get("pending") in {"guide_new", "guide_move"}:
            self._queue_view()
            return

        if mode == "pan":
            self._update_cursor()
            return
        if press.get("dropper"):
            self._dropper_click(x, y, state)
            return
        if mode == "pen":
            self._pen_dragging = False
            self._queue_view()
            return
        pending = press.get("pending")
        if mode is None and press.get("leave_vector"):
            self.end_vector_edit()
            self._update_hover(x, y, state)
            return
        if mode in {"vector_nodes", "vector_handle", "vector_marquee"}:
            self._vector_finish_drag(mode)
            return
        if mode is None and pending in {"vector_nodes", "vector_handle", "vector_marquee"}:
            self._queue_view()
            return
        if mode is None:
            if pending == "draw":
                self._finish_draw(press, clicked=True)
            elif pending == "move" and "click_select" in press:
                self._select([press["click_select"]] if press["click_select"] else [])
            elif pending == "move" and press.get("narrow_to"):
                self._select([press["narrow_to"]])
            elif pending == "zoom":
                factor = 0.5 if alt else 2.0
                self._animate_to(self.view.zoomed_at(self.document.zoom * factor, x, y))
            self._update_hover(x, y, state)
            return
        if mode == "draw":
            self._finish_draw(press, clicked=False)
        elif mode == "move":
            label = self.editor._label or "Move"
            if not ctrl and self._reparent_target:
                self._reparent(press["moving"], self._reparent_target)
            if self.editor.end_gesture():
                self.emit("document-changed", label)
            self.emit("selection-changed")
        elif mode in {"resize", "rotate", "radius", "endpoint"}:
            label = self.editor._label
            if self.editor.end_gesture():
                self.emit("document-changed", label)
        elif mode == "gradient":
            end = (self.gradient_editor or {}).get("end")
            if end:
                end()
            elif self.editor.end_gesture():
                self.emit("document-changed", "Change fill")
        elif mode == "zoom" and self._marquee is not None:
            left, right = sorted((self._marquee[0], self._marquee[2]))
            top, bottom = sorted((self._marquee[1], self._marquee[3]))
            if right - left > 1 and bottom - top > 1:
                self._animate_to(fit_rect(left, top, right, bottom, self.get_width(), self.get_height(),
                                          margin=0, max_zoom=None))
        self._marquee = None
        self._reparent_target = None
        self._snap_guides, self._snap_measurements = [], []
        self._endpoint_snap = None
        self._update_hover(x, y, state)
        self.queue_draw()

    def _reparent(self, ids: list[str], target: str) -> None:
        """Dropping into a frame makes the layer its child, on top of its siblings."""
        parent_id = None if target == "__page__" else target
        for object_id in ids:
            item = self.document.object(object_id)
            if item is None or item.parent_id == parent_id:
                continue
            if parent_id is not None:
                inside = {object_id, *(child.id for child in self.document.descendants(object_id))}
                if parent_id in inside:
                    continue
            self.document.objects.remove(item)
            item.parent_id = parent_id
            self.document.objects.append(item)

    def _size_badge(self) -> None:
        frame = self._selection_frame()
        items = self._selection_items()
        if frame is None or not items:
            self._badge = None
            return
        if len(items) == 1:
            item = items[0]
            if item.kind == "line":
                text = format_number(math.hypot(item.width, item.height))
            else:
                text = f"{format_number(item.width)} × {format_number(item.height)}"
        else:
            bounds = geometry.union_bounds(items)
            text = f"{format_number(bounds[2] - bounds[0])} × {format_number(bounds[3] - bounds[1])}"
        corners = frame["corners"]
        self._badge = (text, sum(corner[0] for corner in corners) / 4, max(corner[1] for corner in corners) + 8)

    def _engine(self) -> SnapEngine:
        return SnapEngine(self.document, pixel_snap=self.pixel_snap, grid=self.grid_size,
                          threshold=SCREEN_THRESHOLD / max(self.document.zoom, 1e-6))

    # ── hover ────────────────────────────────────────────────────────────

    def _motion(self, controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._pointer = (x, y)
        if self._dropper:
            self._dropper_motion(x, y)
            return
        if self._press is not None:
            return
        self._update_hover(x, y, controller.get_current_event_state())
        if self._pen_path:
            self._queue_view()

    def _leave(self, *_args) -> None:
        if self._press is not None:
            return
        self._pointer = None
        if self._dropper:
            self._dropper["outside"] = True
            self._queue_view()
            return
        if self._hover_id or self._hover_measurements or self._hover_label_id:
            self._hover_id = self._hover_label_id = None
            self._hover_measurements = []
            self._queue_view()

    def _update_hover(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        if self._vector is not None:
            hit = self._vector_hit(x, y)
            if hit != self._vector.get("hover"):
                self._vector["hover"] = hit
                self._queue_view()
            self.set_cursor_from_name("pointer" if hit and hit[0] != "segment" else
                                      "crosshair" if hit else "default")
            return
        _, ctrl, alt = _modifiers(state)
        if self._ruler_at(x, y) is not None:
            if self._guide_hover is not None:
                self._guide_hover = None
                self._queue_view()
            self.set_cursor_from_name("default")
            return
        guide = self._guide_at(x, y) if self.tool == "select" else None
        hover_guide = guide.id if guide is not None else None
        if hover_guide != self._guide_hover:
            self._guide_hover = hover_guide
            self._queue_view()
        if guide is not None:
            self.set_cursor_from_name("col-resize" if rulers.guide_axis(guide) == "x" else "row-resize")
            return
        selecting = self.tool in {"select", "direct", "scale"}
        handle = self._handle_at(x, y) if selecting else None
        label = self._label_at(x, y) if selecting else None
        tree, chain = self._hit_chain(x, y)
        target = label or geometry.click_target(tree, chain, self.document.selected_ids, deep=ctrl)
        hover_id = target.id if target is not None and selecting else None
        if hover_id and not ctrl and self.document.selected_ids and self._inside_selection(x, y):
            # Inside the selection a drag moves the selection: no child highlight.
            hover_id = None
        measurements: list[Measurement] = []
        if alt and self.document.selected_ids and selecting:
            hovered = target
            if hovered is None or hovered.id in self.document.selected_ids:
                hovered = self.document.object(self.document.selection[0].parent_id)
            measurements = self._engine().distances_to(set(self.document.selected_ids), hovered)
        label_id = label.id if label is not None else None
        changed = (hover_id != self._hover_id or handle != self._hover_handle
                   or measurements != self._hover_measurements or label_id != self._hover_label_id)
        self._hover_id, self._hover_handle = hover_id, handle
        self._hover_label_id, self._hover_measurements = label_id, measurements
        self._update_cursor(handle)
        if changed:
            self._queue_view()

    def _modifiers_changed(self, _controller: Any, state: Gdk.ModifierType) -> bool:
        if self._press is not None and self._pointer is not None:
            self._route_drag(*self._pointer, state)
        elif self._pointer is not None:
            self._update_hover(*self._pointer, state)
        return False

    def _keyboard_modifier_state(self) -> Gdk.ModifierType:
        seat = self.get_display().get_default_seat()
        keyboard = seat.get_keyboard() if seat is not None else None
        return keyboard.get_modifier_state() if keyboard is not None else Gdk.ModifierType(0)

    def _focus_entered(self, *_args) -> None:
        # Keyboard focus is back: redraw hover affordances from the state the
        # keyboard reports now, not from whatever was held before leaving.
        if self._press is None and self._pointer is not None:
            self._update_hover(*self._pointer, self._keyboard_modifier_state())

    def _focus_left(self, *_args) -> None:
        # A key released after focus moved never reaches the canvas, so do not
        # keep panning with Space or drawing modifier affordances for it.
        if self._space_held:
            self._space_held = False
            if self._mode == "pan" and self._press is not None and self._press.get("button") == 1:
                self._press = None
                self._mode = None
        if self._press is None and self._pointer is not None:
            self._update_hover(*self._pointer, Gdk.ModifierType(0))
        else:
            self._update_cursor(self._hover_handle)

    # ── scroll, pinch and kinetic panning ────────────────────────────────

    def _scroll(self, controller: Any, dx: float, dy: float) -> bool:
        self._stop_animation()
        state = controller.get_current_event_state()
        shift, ctrl, _ = _modifiers(state)
        smooth = controller.get_unit() == Gdk.ScrollUnit.SURFACE
        action = scroll_action(dx, dy, smooth=smooth, ctrl=ctrl or self.tool == "zoom", shift=shift)
        if action.kind == "zoom":
            px, py = self._pointer or (self.get_width() / 2, self.get_height() / 2)
            self._set_view(self.view.zoomed_at(self.document.zoom * action.factor, px, py))
        elif action.kind == "pan":
            self._set_view(self.view.panned(action.dx, action.dy))
        else:
            return False
        if self._press is not None and self._pointer is not None and self._mode != "pan":
            # A marquee or drag extends in document space while scrolling.
            self._route_drag(*self._pointer, state)
        return True

    def _decelerate(self, controller: Gtk.EventControllerScroll, velocity_x: float,
                    velocity_y: float) -> None:
        shift, ctrl, _ = _modifiers(controller.get_current_event_state())
        if shift:
            velocity_x, velocity_y = velocity_y, velocity_x
        if ctrl or math.hypot(velocity_x, velocity_y) < 60:
            return
        self._stop_kinetic()
        self._kinetic = {"vx": -velocity_x, "vy": -velocity_y, "last": None}
        self._kinetic["id"] = self.add_tick_callback(self._kinetic_tick)

    def _kinetic_tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        state = self._kinetic
        if state is None:
            return GLib.SOURCE_REMOVE
        now = clock.get_frame_time() / 1_000_000
        if state["last"] is None:
            state["last"] = now
            return GLib.SOURCE_CONTINUE
        elapsed = min(0.05, now - state["last"])
        state["last"] = now
        decay = math.exp(-4.0 * elapsed)
        state["vx"] *= decay
        state["vy"] *= decay
        self._set_view(self.view.panned(state["vx"] * elapsed, state["vy"] * elapsed))
        if math.hypot(state["vx"], state["vy"]) < 15:
            self._kinetic = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _stop_kinetic(self) -> None:
        if self._kinetic is not None:
            if self._kinetic.get("id"):
                self.remove_tick_callback(self._kinetic["id"])
            self._kinetic = None

    def _pinch_begin(self, gesture: Gtk.GestureZoom, _sequence: Any) -> None:
        self._stop_kinetic()
        self._stop_animation()
        found, cx, cy = gesture.get_bounding_box_center()
        if not found:
            cx, cy = self._zoom_center()
        self._pinch = {"view": self.view, "center": (cx, cy)}

    def _pinch_scale(self, gesture: Gtk.GestureZoom, scale: float) -> None:
        if self._pinch is None:
            self._pinch_begin(gesture, None)
        start: View = self._pinch["view"]
        ox, oy = self._pinch["center"]
        found, cx, cy = gesture.get_bounding_box_center()
        if not found:
            cx, cy = ox, oy
        # Zoom about where the pinch began; two-finger movement also pans.
        self._set_view(start.zoomed_at(start.zoom * scale, ox, oy).panned(cx - ox, cy - oy))

    def pinch_to(self, scale: float, center_x: float, center_y: float) -> None:
        """A pinch from scale 1 to `scale` about a view point (test hook)."""
        start = self.view
        self._set_view(start.zoomed_at(start.zoom * scale, center_x, center_y))

    # ── zoom commands ────────────────────────────────────────────────────

    def zoom_at(self, factor: float, x: float, y: float) -> None:
        self._set_view(self.view.zoomed_at(self.document.zoom * factor, x, y))

    def _zoom_center(self) -> tuple[float, float]:
        if self._pointer is not None:
            return self._pointer
        return self.get_width() / 2, self.get_height() / 2

    def zoom_step(self, direction: int, *, at_pointer: bool = True) -> None:
        base = self._animation["to"] if self._animation else self.view
        cx, cy = self._zoom_center() if at_pointer else (self.get_width() / 2, self.get_height() / 2)
        self._animate_to(base.zoomed_at(ladder_step(base.zoom, direction), cx, cy), duration=150)

    def zoom_to_100(self) -> None:
        self._animate_to(self.view.zoomed_at(1.0, self.get_width() / 2, self.get_height() / 2))

    def zoom_to_fit(self, *, animate: bool = True) -> None:
        items = [item for item in self.document.objects
                 if item.visible and item.parent_id is None and item.kind not in {"guide", "comment"}]
        bounds = geometry.union_bounds(items)
        if bounds is None:
            return
        view = fit_rect(*bounds, self.get_width(), self.get_height())
        if animate:
            self._animate_to(view)
        else:
            self._set_view(view)

    def zoom_to_selection(self) -> None:
        items = self._selection_items()
        if not items:
            self.zoom_to_fit()
            return
        bounds = geometry.union_bounds(items)
        self._animate_to(fit_rect(*bounds, self.get_width(), self.get_height(), max_zoom=None))

    def fit_all(self) -> None:
        self.zoom_to_fit(animate=False)

    def fit_object(self, object_id: str, padding: float = 64) -> None:
        item = self.document.object(object_id)
        if item is None or item.width <= 0 or item.height <= 0:
            return
        self._set_view(fit_rect(*geometry.world_bounds(item), self.get_width(), self.get_height(),
                                margin=padding, max_zoom=4.0))

    def _animate_to(self, target: View, duration: int = ANIMATION_MS) -> None:
        target = View(clamp_zoom(target.zoom), target.x, target.y)
        settings = Gtk.Settings.get_default()
        animations = settings.props.gtk_enable_animations if settings else True
        self._stop_animation()
        if self.reduced_motion or not animations or not self.get_mapped():
            self._set_view(target)
            return
        self._animation = {"from": self.view, "to": target, "start": None, "duration": duration / 1000}
        self._animation["id"] = self.add_tick_callback(self._animation_tick)

    def _animation_tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        animation = self._animation
        if animation is None:
            return GLib.SOURCE_REMOVE
        now = clock.get_frame_time() / 1_000_000
        if animation["start"] is None:
            animation["start"] = now
        t = (now - animation["start"]) / animation["duration"]
        if t >= 1.0:
            self._animation = None
            self._set_view(animation["to"])
            return GLib.SOURCE_REMOVE
        self._set_view(interpolate_view(animation["from"], animation["to"], t,
                                        self.get_width(), self.get_height()))
        return GLib.SOURCE_CONTINUE

    def _stop_animation(self) -> None:
        if self._animation is not None:
            if self._animation.get("id"):
                self.remove_tick_callback(self._animation["id"])
            self._animation = None

    # ── keyboard ─────────────────────────────────────────────────────────

    def _base_keyval(self, keycode: int, keyval: int) -> int:
        """The unshifted key, so Shift+1 reads as "1" on any layout."""
        if keycode:
            try:
                found, base, *_ = self.get_display().translate_key(keycode, 0, 0)
                if found:
                    return base
            except (TypeError, AttributeError):
                pass
        return keyval

    def _key_pressed(self, _controller: Any, keyval: int, keycode: int,
                     state: Gdk.ModifierType) -> bool:
        shift, ctrl, alt = _modifiers(state)
        name = Gdk.keyval_name(keyval) or ""
        if self._rename is not None:
            if name == "Escape":
                self.end_rename(commit=False)
                return True
            return False
        if self._text is not None:
            return self._text_key(name, keyval, shift, ctrl, alt)
        if self._vector is not None and self._vector_key(name, shift, ctrl, alt):
            return True
        if self._guide_selected is not None and name in {"Delete", "BackSpace", "KP_Delete"}:
            return self.delete_guide(self._guide_selected)
        if shift and not ctrl and not alt and name.lower() == "r":
            self.toggle_rulers()
            return True
        base = Gdk.keyval_name(self._base_keyval(keycode, keyval)) or name
        if name == "space":
            if not self._space_held:
                self._space_held = True
                self._update_cursor()
            return True
        if name == "Escape":
            return self.escape()
        if self._dropper and name in {"Tab", "ISO_Left_Tab"}:
            formats = ["hex", "rgb", "hsl", "hsb"]
            self._dropper["format"] = formats[(formats.index(self._dropper.get("format", "hex")) + 1) % 4]
            self._queue_view()
            return True
        if self._pen_path:
            if name in {"Return", "KP_Enter"}:
                self._finish_pen(close=False)
                return True
            if name in {"BackSpace", "Delete"}:
                self._pen_path.pop()
                self._queue_view()
                return True
        if not alt and base == "0" and (shift != ctrl):
            self.zoom_to_100()
            return True
        if shift and not ctrl and not alt and base == "1":
            self.zoom_to_fit()
            return True
        if shift and not ctrl and not alt and base == "2":
            self.zoom_to_selection()
            return True
        if not alt and name in {"plus", "equal", "KP_Add"}:
            self.zoom_step(1)
            return True
        if not alt and name in {"minus", "KP_Subtract"}:
            self.zoom_step(-1)
            return True
        if ctrl or alt:
            lower = name.lower()
            if ctrl and not alt and lower == "a":
                self.select_all(inverse=shift)
                return True
            if ctrl and not alt and lower in {"c", "x"} and self.document.selected_ids:
                self.copy_selection(cut=lower == "x")
                return True
            if ctrl and not alt and lower == "v":
                self.paste(over_selection=shift)
                return True
            if ctrl and not alt and not shift and lower == "d" and self.document.selected_ids:
                return self.duplicate_selection()
            if ctrl and alt and lower == "m" and self.document.selected_ids:
                return self.toggle_mask_selection()
            if ctrl and not alt and not shift and lower == "e" and self.document.selected_ids:
                return self.flatten_selection()
            if alt and shift and not ctrl and lower in {"u", "s", "i", "e"} and self.document.selected_ids:
                return self.boolean_selection({"u": "union", "s": "subtract", "i": "intersect", "e": "exclude"}[lower])
            if ctrl and lower == "g" and self.document.selected_ids:
                if alt:
                    return self.frame_selection()
                return self.ungroup_selection() if shift else self.group_selection()
            return False
        lower = name.lower()
        if lower == "x":
            if shift:
                self.swap_style_colors()
            else:
                self.swap_style_front()
            return True
        if lower == "d" and not shift:
            self.reset_style()
            return True
        if name in {"slash", "KP_Divide"}:
            self.set_front_none()
            return True
        if lower == "o" and not shift:
            self._tool_from_key("ellipse")
            return True
        if lower == "k" and not shift:
            self._tool_from_key("scale")
            return True
        if lower == "l":
            self._tool_from_key("arrow" if shift else "line")
            return True
        if shift and lower in {"h", "v"}:
            return self.flip_selection(lower)
        directions = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}
        if name in directions:
            dx, dy = directions[name]
            if self.document.selected_ids:
                step = 10 if shift else 1
                return self.nudge(dx * step, dy * step)
            step = 320 if shift else 32
            self._set_view(self.view.panned(-dx * step, -dy * step))
            return True
        if name in {"Return", "KP_Enter"}:
            selection = self.document.selection
            if not shift and len(selection) == 1 and selection[0].kind == "text" and not selection[0].locked:
                self.begin_text_edit(selection[0], select_all=True)
                return True
            if not shift and len(selection) == 1 and self.can_edit_vector(selection[0]) \
                    and not self.document.children(selection[0].id):
                return self.begin_vector_edit(selection[0])
            return self.select_parent(deselect_at_top=False) if shift else self.select_children()
        if name in {"Tab", "ISO_Left_Tab"} and self.document.selected_ids:
            return self.select_sibling(-1 if shift or name == "ISO_Left_Tab" else 1)
        if not shift and len(base) == 1 and base.isdigit() and self.document.selected_ids:
            opacity = 1.0 if base == "0" else int(base) / 10
            self._set_selection_property("opacity", opacity, f"Change opacity to {round(opacity * 100)}%")
            return True
        return False

    def _tool_from_key(self, tool: str) -> None:
        self.set_tool(tool)
        self.emit("tool-changed", tool)

    def _key_released(self, _controller: Any, keyval: int, _keycode: int, _state: Any) -> None:
        if Gdk.keyval_name(keyval) == "space":
            self._space_held = False
            if self._mode == "pan" and self._press is not None and self._press.get("button") == 1:
                self._press = None
                self._mode = None
            self._update_cursor(self._hover_handle)

    def escape(self) -> bool:
        """Leave the innermost state first (spec 3.4)."""
        if self._text is not None:
            self.end_text_edit()
            return True
        if self._vector is not None and self._press is None:
            self.end_vector_edit()
            return True
        if self._press is not None and self._mode not in {None, "pan"}:
            self._stop_autoscroll()
            if self.editor.gesture_open:
                self.editor.cancel_gesture()
            creating = self._mode == "draw"
            self._press = None
            self._mode = None
            self._marquee = None
            self._badge = None
            self._reparent_target = None
            self._snap_guides, self._snap_measurements = [], []
            self.queue_draw()
            self.emit("selection-changed")
            if creating:
                self._tool_from_key("select")
            return True
        if self._dropper:
            self.cancel_eyedropper()
            return True
        if self._pen_path:
            self._finish_pen(close=False)
            return True
        if self.tool != "select":
            self.tool_locked = False
            self._tool_from_key("select")
            return True
        if self.document.selected_ids:
            return self.select_parent(deselect_at_top=True)
        return False

    # ── selection commands ───────────────────────────────────────────────

    def select_parent(self, *, deselect_at_top: bool = True) -> bool:
        selection = self.document.selection
        if not selection:
            return False
        parent = self.document.object(selection[0].parent_id)
        if parent is not None and parent.kind != "spread":
            self._select([parent.id])
        elif deselect_at_top:
            self._select([])
        return True

    def select_children(self) -> bool:
        children = [child.id for item in self.document.selection
                    for child in self.document.children(item.id) if child.visible and not child.locked]
        if not children:
            return False
        self._select(children)
        return True

    def select_sibling(self, direction: int) -> bool:
        selection = self.document.selection
        siblings = [item for item in self.document.children(selection[0].parent_id) if item.visible]
        if len(siblings) > 1:
            index = siblings.index(selection[-1]) if selection[-1] in siblings else 0
            self._select([siblings[(index + direction) % len(siblings)].id])
        return True

    def select_all(self, *, inverse: bool = False) -> None:
        selection = self.document.selection
        parent_id = selection[0].parent_id if selection else None
        siblings = [item.id for item in self.document.children(parent_id) if item.visible and not item.locked]
        if inverse:
            chosen = set(self.document.selected_ids)
            siblings = [value for value in siblings if value not in chosen]
        self._select(siblings)

    def nudge(self, dx: float, dy: float) -> bool:
        movable = [item.id for item in self.document.selection if not item.locked]
        if not movable:
            return False
        try:
            self.editor.move(movable, dx, dy, key="nudge:" + ",".join(sorted(movable)))
        except EditError:
            return False
        self.emit("document-changed", "Nudge")
        self.queue_draw()
        return True

    def flip_selection(self, axis: str) -> bool:
        items = [item for item in self.document.selection if not item.locked]
        if not items:
            return False
        key = "flip_h" if axis == "h" else "flip_v"
        label = "Flip horizontal" if axis == "h" else "Flip vertical"
        with self.editor.transaction(label):
            for item in items:
                setattr(item, key, not getattr(item, key))
            self.document.touch()
        self.emit("document-changed", label)
        self.queue_draw()
        return True

    def _set_selection_property(self, key: str, value: Any, label: str) -> None:
        try:
            self.editor.set_property(list(self.document.selected_ids), key, value)
        except EditError:
            return
        self.emit("document-changed", label)
        self.queue_draw()

    # ── current style commands (spec 6.2) ────────────────────────────────

    def current_style(self) -> dict[str, Any]:
        return normalized_style(self.document.current_style)

    def swap_style_front(self) -> None:
        style = self.current_style()
        style["front"] = "stroke" if style["front"] == "fill" else "fill"
        # Which swatch is in front is a view preference, not an undo step.
        self.document.current_style = style
        self.emit("style-changed")

    def swap_style_colors(self) -> None:
        """Shift+X: swap fill and stroke of the selection, or of the chip."""
        items = [item for item in self.document.selection if not item.locked]
        if items:
            from .paint import paint_stack, set_paint_stack
            with self.editor.transaction("Swap fill and stroke"):
                for item in items:
                    # Whole stacks swap, every paint with its opacity and blend.
                    fills, strokes = paint_stack(item, "fill"), paint_stack(item, "stroke")
                    set_paint_stack(item, "fill", strokes)
                    set_paint_stack(item, "stroke", fills)
                    if fills and not item.stroke_width:
                        item.stroke_width = 1.0
                self.document.touch()
            self.emit("document-changed", "Swap fill and stroke")
        else:
            style = self.current_style()
            shape, line = style["shape"], style["line"]
            shape["fill"], line["stroke"] = line.get("stroke"), shape.get("fill")
            shape["fill_gradient"], line["stroke_gradient"] = line.get("stroke_gradient"), shape.get("fill_gradient")
            self.editor.set_current_style(style, "Swap fill and stroke")
        self.queue_draw()
        self.emit("style-changed")

    def reset_style(self) -> None:
        style = default_current_style()
        style["front"] = self.current_style().get("front", "fill")
        self.editor.set_current_style(style, "Reset current style")
        self.emit("style-changed")

    def set_front_none(self) -> None:
        front = self.current_style().get("front", "fill")
        items = [item for item in self.document.selection if not item.locked]
        if items:
            try:
                self.editor.set_paint([item.id for item in items], front, None,
                                      label="Remove fill" if front == "fill" else "Remove stroke")
            except EditError:
                return
            self.emit("document-changed", "Remove paint")
        else:
            style = self.current_style()
            part = style["shape"] if front == "fill" else style["line"]
            part[front] = None
            part[f"{front}_gradient"] = None
            self.editor.set_current_style(style, "Set no fill" if front == "fill" else "Set no stroke")
        self.queue_draw()
        self.emit("style-changed")

    # ── eyedropper (spec 6.6) ────────────────────────────────────────────

    def begin_eyedropper(self, callback: Callable[[Color, dict[str, bool]], None], *,
                         return_tool: str | None = None,
                         on_cancel: Callable[[], None] | None = None) -> None:
        """Sample the rendered canvas; `callback(color, modifiers)` on click."""
        self.cancel_eyedropper(notify=False)
        self._dropper = {"callback": callback, "return_tool": return_tool, "on_cancel": on_cancel,
                         "format": "hex", "outside": False, "pixels": None}
        self._capture_scene_pixels()
        root = self.get_root()
        if isinstance(root, Gtk.Widget):
            outside = Gtk.GestureClick(button=1)
            outside.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            outside.connect("pressed", self._dropper_outside_click)
            root.add_controller(outside)
            self._dropper["outside_controller"] = (root, outside)
        self._update_cursor()
        self._queue_view()

    def cancel_eyedropper(self, notify: bool = True) -> None:
        state = self._dropper
        if state is None:
            return
        self._dropper = None
        holder = state.get("outside_controller")
        if holder:
            holder[0].remove_controller(holder[1])
        if notify and state.get("on_cancel"):
            state["on_cancel"]()
        if state.get("return_tool") and self.tool == "dropper":
            self.tool = state["return_tool"]
            self.emit("tool-changed", self.tool)
        self._update_cursor()
        self._queue_view()

    def _ground_color(self) -> Color:
        content = self._token("luma_content", "#fbfbfa")
        ink = self._token("luma_primary_ink", "#ffffff")
        mix = 0.039   # matches `.canvas-canvas-frame` in content.css
        return Color(content.red + (ink.red - content.red) * mix,
                     content.green + (ink.green - content.green) * mix,
                     content.blue + (ink.blue - content.blue) * mix)

    def _capture_scene_pixels(self) -> None:
        width, height = self.get_width(), self.get_height()
        native = self.get_native()
        renderer = native.get_renderer() if native is not None else None
        if width <= 0 or height <= 0 or renderer is None:
            return
        scale = self.get_scale_factor()
        snapshot = Gtk.Snapshot()
        snapshot.scale(scale, scale)
        snapshot.append_color(rgba(self._ground_color()), rect(0, 0, width, height))
        self._snapshot_scene(snapshot, width, height, self.view)
        node = snapshot.to_node()
        if node is None:
            return
        texture = renderer.render_texture(node, rect(0, 0, width * scale, height * scale))
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = downloader.download_bytes()
        self._dropper["pixels"] = (data.get_data(), stride, texture.get_width(), texture.get_height(), scale)

    def sample(self, x: float, y: float) -> Color | None:
        """The rendered canvas pixel under a view point while sampling."""
        pixels = self._dropper.get("pixels") if self._dropper else None
        if not pixels:
            return None
        data, stride, width, height, scale = pixels
        px, py = int(x * scale), int(y * scale)
        if not (0 <= px < width and 0 <= py < height):
            return None
        offset = py * stride + px * 4
        return Color(data[offset] / 255, data[offset + 1] / 255, data[offset + 2] / 255, 1.0)

    def _dropper_motion(self, x: float, y: float) -> None:
        if self._dropper is not None:
            self._dropper["outside"] = False
            self._pointer = (x, y)
            self._queue_view()

    def _dropper_click(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        dropper = self._dropper
        if dropper is None:
            return
        color = self.sample(x, y)
        if color is None:
            return
        shift, ctrl, alt = _modifiers(state)
        callback = dropper["callback"]
        if not alt:
            self.cancel_eyedropper(notify=False)
        callback(color, {"shift": shift, "ctrl": ctrl, "alt": alt})

    def _dropper_outside_click(self, gesture: Gtk.GestureClick, _count: int, x: float, y: float) -> None:
        dropper = self._dropper
        if dropper is None:
            return
        found, bounds = self.compute_bounds(self.get_root())
        if found and bounds.contains_point(point(x, y)):
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        callback = dropper["callback"]
        self.cancel_eyedropper(notify=False)
        from .color_picker import pick_screen_color

        def picked(color: Color | None) -> None:
            if color is not None:
                callback(color, {"shift": False, "ctrl": False, "alt": False})
        pick_screen_color(self, picked)

    def _apply_dropper_to_selection(self, color: Color, modifiers: dict[str, bool]) -> None:
        """`I` on the canvas: the selection's fill (Shift: stroke), or the current style."""
        target = "stroke" if modifiers.get("shift") else "fill"
        RecentColors().add(color)
        items = [item for item in self.document.selection if not item.locked]
        if items:
            try:
                self.editor.set_paint([item.id for item in items], target, solid(color))
            except EditError:
                return
            self.emit("document-changed", f"Change {target}")
        else:
            style = self.current_style()
            if target == "fill":
                style["shape"]["fill"], style["shape"]["fill_gradient"] = to_hex(color), None
            else:
                style["line"]["stroke"], style["line"]["stroke_gradient"] = to_hex(color), None
            self.editor.set_current_style(style, "Pick color")
        self.queue_draw()
        self.emit("style-changed")

    def _draw_loupe(self, snapshot: Gtk.Snapshot, width: float, height: float) -> None:
        dropper = self._dropper
        if dropper is None:
            return
        if dropper.get("outside") or self._pointer is None:
            self._draw_badge(snapshot, "Click to pick from the screen", width / 2, height - 28, BADGE_DARK)
            return
        pixels = dropper.get("pixels")
        if not pixels:
            return
        x, y = self._pointer
        data, stride, pw, ph, scale = pixels
        diameter, cells = 96.0, 11
        cell = diameter / cells
        cx = x
        cy = y - diameter / 2 - 24 if y > diameter + 48 else y + diameter / 2 + 24
        left, top = cx - diameter / 2, cy - diameter / 2
        circle = Gsk.PathBuilder.new()
        circle.add_circle(point(cx, cy), diameter / 2)
        circle_path = circle.to_path()
        shadow = Gsk.Shadow()
        shadow.color = _black(0.28)
        shadow.dx, shadow.dy, shadow.radius = 0, 4, 12
        snapshot.push_shadow([shadow])
        snapshot.append_fill(circle_path, Gsk.FillRule.WINDING, _white())
        snapshot.pop()
        snapshot.push_fill(circle_path, Gsk.FillRule.WINDING)
        center_x, center_y = int(x * scale), int(y * scale)
        for row in range(cells):
            for column in range(cells):
                sx, sy = center_x + column - cells // 2, center_y + row - cells // 2
                if 0 <= sx < pw and 0 <= sy < ph:
                    offset = sy * stride + sx * 4
                    color = Gdk.RGBA(red=data[offset] / 255, green=data[offset + 1] / 255,
                                     blue=data[offset + 2] / 255, alpha=1.0)
                else:
                    color = Gdk.RGBA(red=0.5, green=0.5, blue=0.5, alpha=1.0)
                snapshot.append_color(color, rect(left + column * cell, top + row * cell, cell + 0.5, cell + 0.5))
        ring = Gsk.PathBuilder.new()
        ring.add_rect(rect(left + (cells // 2) * cell, top + (cells // 2) * cell, cell, cell))
        snapshot.append_stroke(ring.to_path(), Gsk.Stroke.new(2.0), _black(0.55))
        snapshot.append_stroke(ring.to_path(), Gsk.Stroke.new(1.0), _white())
        snapshot.pop()
        snapshot.append_stroke(circle_path, Gsk.Stroke.new(3.0), _white())
        snapshot.append_stroke(circle_path, Gsk.Stroke.new(1.0), _black(0.18))
        sampled = self.sample(x, y)
        if sampled is None:
            return
        fmt = dropper.get("format", "hex")
        r, g, b = sampled.bytes()
        if fmt == "rgb":
            label = f"{r}  {g}  {b}"
        elif fmt == "hsl":
            h, s, lightness = rgb_to_hsl(sampled)
            label = f"{round(h)}°  {round(s * 100)}%  {round(lightness * 100)}%"
        elif fmt == "hsb":
            h, s, v = rgb_to_hsv(sampled)
            label = f"{round(h)}°  {round(s * 100)}%  {round(v * 100)}%"
        else:
            label = to_hex(sampled, prefix=False).upper()
        self._draw_badge(snapshot, label, cx, cy + diameter / 2 + 6, BADGE_DARK, anchor="top")

    # ── context menu ─────────────────────────────────────────────────────

    def _context_pressed(self, x: float, y: float) -> None:
        if self._dropper:
            self.cancel_eyedropper()
            return
        if self._text is not None:
            self._text_context_menu(x, y)
            return
        item = self._hit(x, y)
        if item and item.id not in self.document.selected_ids:
            self._select([item.id])
        self.emit("context-requested", x, y, item)

    # ── pen ──────────────────────────────────────────────────────────────

    def _pen_pressed(self, x: float, y: float, count: int, shift: bool) -> None:
        """Click for a corner, drag for a curve, click the start to close."""
        wx, wy = self.view_to_world(x, y)
        if self._pen_path and count >= 2:
            self._finish_pen(close=False)
            return
        if len(self._pen_path) >= 2:
            first = self._pen_path[0]
            sx, sy = self.world_to_view(float(first["x"]), float(first["y"]))
            if math.hypot(sx - x, sy - y) < 8:
                self._finish_pen(close=True)
                return
        if self._pen_path and shift:
            last = self._pen_path[-1]
            dx, dy = geometry.constrain_angle(wx - float(last["x"]), wy - float(last["y"]), 45.0)
            wx, wy = float(last["x"]) + dx, float(last["y"]) + dy
        elif self.snap:
            wx, wy, _ = self._engine().snap_point(set(), wx, wy)
        self._pen_path.append(node(wx, wy))
        self._pen_dragging = True
        self._queue_view()

    def _pen_motion(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        """Dragging out of a new node pulls its handles; Alt breaks the mirror."""
        if not self._pen_dragging or not self._pen_path:
            return
        press = self._press
        if press and math.hypot(x - press["x"], y - press["y"]) < DRAG_THRESHOLD:
            return
        shift, _, alt = _modifiers(state)
        wx, wy = self.view_to_world(x, y)
        index = len(self._pen_path) - 1
        entry = self._pen_path[index]
        if shift:
            dx, dy = geometry.constrain_angle(wx - entry["x"], wy - entry["y"], 45.0)
            wx, wy = entry["x"] + dx, entry["y"] + dy
        self._pen_path[index] = {**entry, "smooth": not alt}
        set_handle(self._pen_path, index, "out", wx, wy, mirror=not alt)
        self._queue_view()

    def _finish_pen(self, *, close: bool) -> None:
        path = [dict(entry) for entry in self._pen_path]
        self._pen_path = []
        self._pen_dragging = False
        if len(path) < 2:
            self._queue_view()
            return
        if close:
            close_path(path)
        left, top, right, bottom = geometry.subpath_bounds([geometry.nodes_outline(path)])
        relative = []
        for entry in path:
            moved = dict(entry)
            for key in ("x", "in_x", "out_x"):
                if key in moved:
                    moved[key] = float(moved[key]) - left
            for key in ("y", "in_y", "out_y"):
                if key in moved:
                    moved[key] = float(moved[key]) - top
            relative.append(moved)
        try:
            item = self.editor.create("path", left, top, max(0.0, right - left), max(0.0, bottom - top),
                                      parent_id=self._parent_for_new(float(path[0]["x"]), float(path[0]["y"])),
                                      name="Vector", path=relative)
        except EditError as error:
            print(f"Canvas could not draw the path: {error}")
            self._queue_view()
            return
        self.document.select([item.id])
        self.emit("selection-changed")
        self.emit("document-changed", "Draw path")
        if self.tool == "path" and not self.tool_locked:
            self._tool_from_key("select")
        self.queue_draw()

    def cancel_pen(self) -> None:
        self._pen_path = []
        self._pen_dragging = False
        self._queue_view()

    # ── rulers and guides ────────────────────────────────────────────────

    def toggle_rulers(self, value: bool | None = None) -> None:
        """Shift+R."""
        self.show_rulers = (not self.show_rulers) if value is None else bool(value)
        self.emit("rulers-changed", self.show_rulers)
        self._queue_view()

    def _ruler_at(self, x: float, y: float) -> str | None:
        if not self.show_rulers:
            return None
        size = rulers.RULER_SIZE
        if x < size and y < size:
            return "corner"
        if y < size:
            return "top"
        if x < size:
            return "left"
        return None

    def guides(self) -> list[LayoutObject]:
        return [item for item in self.document.objects if item.kind == "guide" and item.visible]

    def _guide_at(self, x: float, y: float) -> LayoutObject | None:
        view = self.view
        for guide in reversed(self.guides()):
            if rulers.guide_axis(guide) == "x":
                if abs(view.to_view(guide.x, 0)[0] - x) <= 4:
                    return guide
            elif abs(view.to_view(0, guide.y)[1] - y) <= 4:
                return guide
        return None

    def _guide_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        press = self._press
        guide = self.document.object(press.get("guide")) if press else None
        if guide is None:
            return
        _, ctrl, _ = _modifiers(state)
        axis = press["axis"]
        wx, wy = self.view_to_world(x, y)
        if not ctrl:
            engine = self._engine()
            peers = [item for item in self.document.objects if item.visible and item.kind not in {"guide", "comment", "slice"}]
            value = wx if axis == "x" else wy
            best = None
            for peer in peers:
                left, top, right, bottom = geometry.world_bounds(peer)
                for option in ((left, (left + right) / 2, right) if axis == "x" else (top, (top + bottom) / 2, bottom)):
                    if abs(option - value) <= engine.threshold and (best is None or abs(option - value) < abs(best - value)):
                        best = option
            value = best if best is not None else (round(value) if self.pixel_snap else value)
            wx, wy = (value, wy) if axis == "x" else (wx, value)
        if axis == "x":
            guide.x = wx
        else:
            guide.y = wy
        removing = self._ruler_at(x, y) in {"top" if axis == "y" else "left", "corner"}
        press["removing"] = removing
        self._queue_view()

    def _finish_guide(self, press: dict[str, Any], x: float, y: float) -> None:
        guide_id = press.get("guide")
        axis = press.get("axis")
        back_on_ruler = self._ruler_at(x, y) in {"top" if axis == "y" else "left", "corner"}
        if press.get("pending") == "guide_new" and back_on_ruler:
            self.editor.cancel_gesture()
            self._guide_selected = None
            self._queue_view()
            return
        if back_on_ruler:
            self.document.objects = [item for item in self.document.objects if item.id != guide_id]
            self._guide_selected = None
            self.editor.set_gesture_label("Delete guide")
        label = self.editor._label or "Move guide"
        if self.editor.end_gesture():
            self.emit("document-changed", label)
        self._queue_view()

    def delete_guide(self, guide_id: str) -> bool:
        if self.document.object(guide_id) is None:
            return False
        with self.editor.transaction("Delete guide"):
            self.document.objects = [item for item in self.document.objects if item.id != guide_id]
            self.document.touch()
        self._guide_selected = None
        self.emit("document-changed", "Delete guide")
        self._queue_view()
        return True

    def _draw_guides(self, snapshot: Gtk.Snapshot, width: float, height: float, view: View) -> None:
        guides = self.guides()
        if not guides:
            return
        scale = self.device_scale()
        red = rgba(GUIDE_RED)
        for guide in guides:
            active = guide.id in {self._guide_selected, self._guide_hover}
            thickness = (2.0 if active else 1.0) / scale
            if rulers.guide_axis(guide) == "x":
                vx = rulers.crisp(view.to_view(guide.x, 0)[0], scale)
                snapshot.append_color(red, rect(vx - thickness / 2, 0, thickness, height))
            else:
                vy = rulers.crisp(view.to_view(0, guide.y)[1], scale)
                snapshot.append_color(red, rect(0, vy - thickness / 2, width, thickness))

    def _draw_rulers(self, snapshot: Gtk.Snapshot, width: float, height: float, view: View,
                     accent: Gdk.RGBA) -> None:
        size = rulers.RULER_SIZE
        scale = self.device_scale()
        surface = self._token("luma_content", "#fbfbfa")
        line = self._token("luma_line_faint", "#e3e3e0")
        muted = self._token("luma_muted", "#757e87")
        hairline = 1.0 / scale
        snapshot.append_color(surface, rect(0, 0, width, size))
        snapshot.append_color(surface, rect(0, size, size, height - size))
        snapshot.append_color(line, rect(0, size - hairline, width, hairline))
        snapshot.append_color(line, rect(size - hairline, size, hairline, height - size))
        # The selection's extent, in accent, like Figma.
        items = [item for item in self._selection_items() if item.kind != "guide"]
        bounds = geometry.union_bounds(items) if items else None
        extents: list[tuple[str, float, float]] = []
        band = Gdk.RGBA(red=accent.red, green=accent.green, blue=accent.blue, alpha=0.16)
        if bounds is not None:
            left, top = view.to_view(bounds[0], bounds[1])
            right, bottom = view.to_view(bounds[2], bounds[3])
            snapshot.append_color(band, rect(max(left, size), 0, max(0.0, min(right, width) - max(left, size)), size - hairline))
            snapshot.append_color(band, rect(0, max(top, size), size - hairline, max(0.0, min(bottom, height) - max(top, size))))
            extents = [("x", left, bounds[0]), ("x", right, bounds[2]), ("y", top, bounds[1]), ("y", bottom, bounds[3])]
        step = rulers.nice_step(view.zoom)
        ticks_path = Gsk.PathBuilder.new()
        # Labels in priority order: guides being handled, the selection's edges,
        # then ticks. Each is placed after its line, or before it (for a start
        # edge), and a label that would touch one already placed is dropped.
        labels: list[tuple[str, float, str, Gdk.RGBA, str]] = []
        for guide in self.guides():
            if guide.id in {self._guide_selected, self._guide_hover}:
                axis = rulers.guide_axis(guide)
                position = view.to_view(guide.x, 0)[0] if axis == "x" else view.to_view(0, guide.y)[1]
                labels.append((axis, position, rulers.label(round(rulers.guide_position(guide), 2), 0.01),
                               rgba(GUIDE_RED), "after"))
        for index, (kind, position, value) in enumerate(extents):
            labels.append((kind, position, rulers.label(round(value, 2), 0.01), accent,
                           "before" if index % 2 == 0 else "after"))
        for axis, start, end, offset in (("x", size, width, view.x), ("y", size, height, view.y)):
            major, minor = rulers.ticks(start, end, view.zoom, offset)
            for _value, position in minor:
                crisp_position = rulers.crisp(position, scale)
                if axis == "x":
                    ticks_path.move_to(crisp_position, size - 4)
                    ticks_path.line_to(crisp_position, size)
                else:
                    ticks_path.move_to(size - 4, crisp_position)
                    ticks_path.line_to(size, crisp_position)
            for value, position in major:
                crisp_position = rulers.crisp(position, scale)
                if axis == "x":
                    ticks_path.move_to(crisp_position, size - 8)
                    ticks_path.line_to(crisp_position, size)
                else:
                    ticks_path.move_to(size - 8, crisp_position)
                    ticks_path.line_to(size, crisp_position)
                labels.append((axis, position, rulers.label(value, step), muted, "after"))
        snapshot.append_stroke(ticks_path.to_path(), Gsk.Stroke.new(hairline), muted)
        placed: dict[str, list[tuple[float, float]]] = {"x": [], "y": []}
        for axis in ("x", "y"):
            clip = rect(size, 0, max(0.0, width - size), size) if axis == "x" else rect(0, size, size, max(0.0, height - size))
            snapshot.push_clip(clip)
            for label_axis, position, text, color, side in labels:
                if label_axis != axis:
                    continue
                layout = self._layout(text, 10, 500)
                _, logical = layout.get_pixel_extents()
                length = logical.width
                first = position + 3 if side == "after" else position - 3 - length
                span = (first - 2, first + length + 2)
                if any(span[0] < other[1] and other[0] < span[1] for other in placed[axis]):
                    continue
                placed[axis].append(span)
                snapshot.save()
                if axis == "x":
                    snapshot.translate(point(round(first), round((size - 8 - logical.height) / 2)))
                else:
                    # Vertical labels read bottom to top; "after" is further down the ruler.
                    snapshot.translate(point(round((size - 8 - logical.height) / 2), round(first + length)))
                    snapshot.rotate(-90)
                snapshot.append_layout(layout, color)
                snapshot.restore()
            snapshot.pop()
        snapshot.append_color(surface, rect(0, 0, size, size))
        snapshot.append_color(line, rect(size - hairline, 0, hairline, size))
        snapshot.append_color(line, rect(0, size - hairline, size, hairline))

    # ── auto-scroll (a drag at the canvas's edge moves the view) ─────────

    AUTOSCROLL_MODES = {"move", "resize", "marquee", "draw", "endpoint", "vector_nodes", "vector_handle",
                        "vector_marquee", "guide_new", "guide_move", "zoom", "radius"}

    def _autoscroll_follow(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        inset = rulers.RULER_SIZE if self.show_rulers else 0.0
        vx, vy = rulers.autoscroll_velocity(x, y, self.get_width(), self.get_height(),
                                            inset_left=inset, inset_top=inset)
        if vx == 0 and vy == 0:
            self._stop_autoscroll()
            return
        if self._autoscroll is None:
            self._autoscroll = {"last": None}
            self._autoscroll["id"] = self.add_tick_callback(self._autoscroll_tick)
        self._autoscroll.update({"vx": vx, "vy": vy, "pointer": (x, y), "state": state})

    def _autoscroll_tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        scroll = self._autoscroll
        if scroll is None or self._press is None:
            self._autoscroll = None
            return GLib.SOURCE_REMOVE
        now = clock.get_frame_time() / 1_000_000
        if scroll["last"] is None:
            scroll["last"] = now
            return GLib.SOURCE_CONTINUE
        elapsed = min(0.05, now - scroll["last"])
        scroll["last"] = now
        self.autoscroll_step(elapsed)
        return GLib.SOURCE_CONTINUE if self._autoscroll is not None else GLib.SOURCE_REMOVE

    def autoscroll_step(self, elapsed: float) -> None:
        """Pan by the current auto-scroll speed for `elapsed` seconds and carry the drag along."""
        scroll = self._autoscroll
        if scroll is None:
            return
        self._set_view(self.view.panned(scroll["vx"] * elapsed, scroll["vy"] * elapsed))
        x, y = scroll["pointer"]
        mode = self._mode
        handler = {"move": self._move_to, "marquee": self._marquee_to, "resize": self._resize_to,
                   "draw": self._draw_to, "endpoint": self._endpoint_to, "radius": self._radius_to,
                   "vector_nodes": self._vector_nodes_to, "vector_handle": self._vector_handle_to,
                   "vector_marquee": self._vector_marquee_to, "guide_new": self._guide_to,
                   "guide_move": self._guide_to, "zoom": self._zoom_marquee_to}.get(mode)
        if handler is not None:
            handler(x, y, scroll["state"])

    def _stop_autoscroll(self) -> None:
        if self._autoscroll is not None:
            if self._autoscroll.get("id"):
                self.remove_tick_callback(self._autoscroll["id"])
            self._autoscroll = None

    # ── vector edit mode (spec 8.2) ──────────────────────────────────────
    #
    # Enter or a double-click on a vector edits its nodes: drag nodes and
    # handles (Alt breaks the mirror, Shift keeps 45 degrees), click a segment
    # to add a node, Delete removes nodes, double-click a node to switch it
    # between corner and smooth, drag on empty canvas to select nodes. Shapes
    # become vectors at their first edit. Escape or Enter leaves.

    VECTOR_KINDS = {"path", "line", "rectangle", "ellipse", "polygon", "star"}

    @property
    def vector_editing(self) -> str | None:
        return self._vector["id"] if self._vector else None

    @property
    def vector_selection(self) -> list[int]:
        return sorted(self._vector["selected"]) if self._vector else []

    def can_edit_vector(self, item: LayoutObject | None) -> bool:
        return item is not None and item.kind in self.VECTOR_KINDS and not item.locked and item.visible

    def begin_vector_edit(self, item: LayoutObject) -> bool:
        if not self.can_edit_vector(item):
            return False
        self.end_text_edit()
        self._vector = {"id": item.id, "selected": set(), "hover": None}
        self.document.select([item.id])
        if self.tool not in {"select", "direct"}:
            self.tool = "select"
            self.emit("tool-changed", "select")
        self._update_cursor()
        self.emit("vector-editing-changed", True)
        self.emit("selection-changed")
        self._queue_view()
        return True

    def end_vector_edit(self) -> None:
        if self._vector is None:
            return
        self._vector = None
        self.emit("vector-editing-changed", False)
        self.emit("selection-changed")
        self._queue_view()

    def _vector_item(self) -> LayoutObject | None:
        if self._vector is None:
            return None
        item = self.document.object(self._vector["id"])
        if item is None:
            self._vector = None
        return item

    def vector_nodes(self, item: LayoutObject | None = None) -> list[dict]:
        """The nodes being edited: a vector's own, or a shape's outline as nodes."""
        item = item or self._vector_item()
        if item is None:
            return []
        if item.kind in {"path", "line"} and item.path:
            path = nodes.detect_mirrors(item.path)
        else:
            from .booleans import to_nodes
            path = nodes.detect_mirrors(to_nodes(geometry.outline(item)))
        if self._vector is not None and self._vector["id"] == item.id:
            # An undo can take points away: forget selections past the end.
            self._vector["selected"].intersection_update(range(len(path)))
        return path

    def _press_on_vector_item(self, x: float, y: float) -> bool:
        item = self._vector_item()
        return item is not None and geometry.contains(item, *self.view_to_world(x, y),
                                                      tolerance=self._tolerance())

    def _vector_commit_path(self, item: LayoutObject, path: list[dict]) -> None:
        """Write edited nodes; a shape becomes a vector at its first edit."""
        if item.kind not in {"path", "line"} or (item.kind == "line" and len(path) != 2):
            if item.kind != "path":
                if item.name in KIND_NAMES.values():
                    item.name = "Vector"
                item.kind = "path"
                item.radius, item.corner_radii, item.point_count = 0.0, [], 0
        item.path = path

    def _vector_rebase(self, item: LayoutObject) -> None:
        """The box hugs the path again, without moving anything on screen."""
        found = nodes.rebased(item.path)
        if found is None:
            return
        path, (left, top, width, height) = found
        width, height = max(width, 0.0), max(height, 0.0)
        cx, cy = geometry.to_world(item, left + width / 2, top + height / 2)
        item.x, item.y, item.width, item.height = cx - width / 2, cy - height / 2, width, height
        item.path = path

    def _vector_view_point(self, item: LayoutObject, local: tuple[float, float]) -> tuple[float, float]:
        return self.world_to_view(*geometry.to_world(item, *local))

    def _vector_visible_handles(self, path: list[dict]) -> list[int]:
        """Handles show on selected nodes and on the nodes next to them (Figma)."""
        if self._vector is None:
            return []
        shown = set(self._vector["selected"])
        for a, b in nodes.segments(path):
            if a in self._vector["selected"] or b in self._vector["selected"]:
                shown.update((a, b))
        return sorted(index for index in shown if index < len(path))

    def _vector_hit(self, x: float, y: float) -> tuple[str, Any] | None:
        """What is under a view point in edit mode: a handle, a node or a segment."""
        item = self._vector_item()
        if item is None:
            return None
        path = self.vector_nodes(item)
        for index in self._vector_visible_handles(path):
            for which in ("out", "in"):
                value = nodes.handle(path[index], which)
                if value is not None and value != nodes.point(path[index]):
                    hx, hy = self._vector_view_point(item, value)
                    if math.hypot(hx - x, hy - y) <= 6:
                        return ("handle", (index, which))
        best = None
        for index, entry in enumerate(path):
            nx, ny = self._vector_view_point(item, nodes.point(entry))
            distance = math.hypot(nx - x, ny - y)
            if distance <= 7 and (best is None or distance < best[0]):
                best = (distance, index)
        if best is not None:
            return ("node", best[1])
        local = geometry.to_local(item, *self.view_to_world(x, y))
        reach = 6 / max(self.document.zoom, 1e-6)
        found = nodes.nearest_segment(path, local, reach)
        if found is not None:
            return ("segment", found)
        return None

    def _vector_press(self, x: float, y: float, count: int, shift: bool) -> bool:
        item = self._vector_item()
        if item is None:
            return False
        hit = self._vector_hit(x, y)
        press = self._press
        selected = self._vector["selected"]
        if hit is None:
            press["pending"] = "vector_marquee"
            press["base_nodes"] = set(selected) if shift else set()
            if not shift:
                selected.clear()
            self._queue_view()
            return True
        kind, value = hit
        path = self.vector_nodes(item)
        press["path"] = path
        press["box"] = (item.x, item.y, item.width, item.height, item.rotation, item.flip_h, item.flip_v)
        if kind == "handle":
            press["pending"] = "vector_handle"
            press["handle"] = value
            return True
        if kind == "node":
            if count >= 2:
                mode = "corner" if nodes.mirror_of(path[value]) != "none" or nodes.handle(path[value], "in") \
                    else "angle"
                self.set_vector_mirror(mode, [value])
                press["pending"] = None
                return True
            if shift:
                selected.symmetric_difference_update({value})
            elif value not in selected:
                selected.clear()
                selected.add(value)
            press["pending"] = "vector_nodes"
            press["anchor"] = value
            self.emit("selection-changed")
            self._queue_view()
            return True
        # A click on a segment adds a node there; dragging then moves it.
        segment, t = value
        new_path, index = nodes.split_segment(path, segment, t)
        with self.editor.transaction("Add point"):
            self._vector_commit_path(item, new_path)
            self._vector_rebase(item)
            self.document.touch()
        selected.clear()
        selected.add(index)
        press["path"] = self.vector_nodes(item)
        press["box"] = (item.x, item.y, item.width, item.height, item.rotation, item.flip_h, item.flip_v)
        press["pending"] = "vector_nodes"
        press["anchor"] = index
        self.emit("selection-changed")
        self.emit("document-changed", "Add point")
        self.queue_draw()
        return True

    def _vector_restore(self, item: LayoutObject, press: dict[str, Any]) -> None:
        x, y, width, height, rotation, flip_h, flip_v = press["box"]
        item.x, item.y, item.width, item.height = x, y, width, height
        item.rotation, item.flip_h, item.flip_v = rotation, flip_h, flip_v

    def _vector_nodes_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        item = self._vector_item()
        press = self._press
        if item is None or press is None:
            return
        shift, ctrl, _ = _modifiers(state)
        self._vector_restore(item, press)
        path = press["path"]
        anchor = press["anchor"]
        start_local = nodes.point(path[anchor])
        start_world = geometry.to_world(item, *start_local)
        wx, wy = self.view_to_world(x, y)
        pressed = press["world"]
        target = (start_world[0] + wx - pressed[0], start_world[1] + wy - pressed[1])
        if shift:
            dx, dy = geometry.constrain_angle(target[0] - start_world[0], target[1] - start_world[1], 45.0)
            target = (start_world[0] + dx, start_world[1] + dy)
        self._snap_guides = []
        if self.snap and not ctrl:
            target, self._snap_guides = self._vector_snap(item, path, target, shift)
        local = geometry.to_local(item, *target)
        moved = nodes.move_nodes(path, self._vector["selected"], local[0] - start_local[0], local[1] - start_local[1])
        self._vector_commit_path(item, moved)
        self._badge = (f"{format_number(target[0])}, {format_number(target[1])}", x + 16, y + 16)
        self.queue_draw()
        self.emit("transform-preview")

    def _vector_snap(self, item: LayoutObject, path: list[dict], target: tuple[float, float],
                     constrained: bool) -> tuple[tuple[float, float], list]:
        """Snap a dragged node to the other nodes' x and y, then to the scene."""
        from .snapping import Guide
        threshold = SCREEN_THRESHOLD / max(self.document.zoom, 1e-6)
        others = [geometry.to_world(item, *nodes.point(entry)) for index, entry in enumerate(path)
                  if index not in self._vector["selected"]]
        x, y = target
        guides = []
        best_x = min(others, key=lambda value: abs(value[0] - x), default=None)
        best_y = min(others, key=lambda value: abs(value[1] - y), default=None)
        snapped_x = best_x is not None and abs(best_x[0] - x) <= threshold
        snapped_y = best_y is not None and abs(best_y[1] - y) <= threshold
        if not constrained:
            if not snapped_x or not snapped_y:
                sx, sy, scene_guides = self._engine().snap_point({item.id}, x, y, parent_id=item.parent_id)
                if not snapped_x:
                    x = sx
                if not snapped_y:
                    y = sy
                guides.extend(scene_guides)
            if snapped_x:
                x = best_x[0]
                guides.append(Guide("vertical", x, min(y, best_x[1]), max(y, best_x[1]), "edge", (y, best_x[1])))
            if snapped_y:
                y = best_y[1]
                guides.append(Guide("horizontal", y, min(x, best_y[0]), max(x, best_y[0]), "edge", (x, best_y[0])))
        elif self.pixel_snap:
            x, y = round(x), round(y)
        return (x, y), guides

    def _vector_handle_to(self, x: float, y: float, state: Gdk.ModifierType) -> None:
        item = self._vector_item()
        press = self._press
        if item is None or press is None:
            return
        shift, _, alt = _modifiers(state)
        self._vector_restore(item, press)
        index, which = press["handle"]
        path = press["path"]
        wx, wy = self.view_to_world(x, y)
        lx, ly = geometry.to_local(item, wx, wy)
        px, py = nodes.point(path[index])
        if shift:
            dx, dy = geometry.constrain_angle(lx - px, ly - py, 45.0)
            lx, ly = px + dx, py + dy
        moved = nodes.move_handle(path, index, which, lx, ly, break_mirror=alt or press.get("broken", False))
        if alt:
            press["broken"] = True
        self._vector_commit_path(item, moved)
        self.queue_draw()
        self.emit("transform-preview")

    def _vector_marquee_to(self, x: float, y: float, _state: Gdk.ModifierType) -> None:
        item = self._vector_item()
        press = self._press
        if item is None or press is None:
            return
        self._marquee = (*press["world"], *self.view_to_world(x, y))
        view_rect = (press["x"], press["y"], x, y)
        path = self.vector_nodes(item)
        found = nodes.nodes_in_rect(path, lambda lx, ly: self._vector_view_point(item, (lx, ly)), view_rect)
        self._vector["selected"].clear()
        self._vector["selected"].update(set(press.get("base_nodes", set())) | set(found))
        self._queue_view()

    def _vector_finish_drag(self, mode: str) -> None:
        item = self._vector_item()
        if mode in {"vector_nodes", "vector_handle"}:
            label = self.editor._label or "Edit vector"
            if item is not None:
                self._vector_rebase(item)
            if self.editor.end_gesture():
                self.emit("document-changed", label)
        self._marquee = None
        self._badge = None
        self._snap_guides = []
        self.emit("selection-changed")
        self.queue_draw()

    def _vector_key(self, name: str, shift: bool, ctrl: bool, alt: bool) -> bool:
        item = self._vector_item()
        if item is None:
            return False
        selected = self._vector["selected"]
        if name in {"Escape", "Return", "KP_Enter"} and not ctrl:
            self.end_vector_edit()
            return True
        if name in {"Delete", "BackSpace", "KP_Delete"} and selected:
            return self.delete_vector_nodes()
        if ctrl and name.lower() == "a":
            selected.update(range(len(self.vector_nodes(item))))
            self.emit("selection-changed")
            self._queue_view()
            return True
        directions = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}
        if name in directions and selected and not ctrl:
            dx, dy = directions[name]
            step = 10 if shift else 1
            path = nodes.move_nodes(self.vector_nodes(item), selected, 0, 0)
            # Nudges move in world space; convert the step into the layer's box.
            origin = geometry.to_local(item, 0, 0)
            moved = geometry.to_local(item, dx * step, dy * step)
            path = nodes.move_nodes(path, selected, moved[0] - origin[0], moved[1] - origin[1])
            with self.editor.transaction("Nudge points", key=f"nudge-nodes:{item.id}"):
                self._vector_commit_path(item, path)
                self._vector_rebase(item)
                self.document.touch()
            self.emit("document-changed", "Nudge points")
            self.queue_draw()
            return True
        return False

    def delete_vector_nodes(self) -> bool:
        item = self._vector_item()
        if item is None or not self._vector["selected"]:
            return False
        path = nodes.delete_nodes(self.vector_nodes(item), self._vector["selected"])
        self._vector["selected"].clear()
        try:
            if len(path) < 2:
                object_id = item.id
                self.end_vector_edit()
                self.editor.delete([object_id])
                label = "Delete"
            else:
                with self.editor.transaction("Delete points"):
                    self._vector_commit_path(item, path)
                    self._vector_rebase(item)
                    self.document.touch()
                label = "Delete points"
        except EditError as error:
            print(f"Canvas could not delete the points: {error}")
            return False
        self.emit("selection-changed")
        self.emit("document-changed", label)
        self.queue_draw()
        return True

    def set_vector_mirror(self, mode: str, indices: list[int] | None = None) -> bool:
        """Corner, or smooth with no mirroring, mirrored angle, or angle and length."""
        item = self._vector_item()
        if item is None:
            return False
        targets = indices if indices is not None else sorted(self._vector["selected"])
        if not targets:
            return False
        path = nodes.set_mirror(self.vector_nodes(item), targets, mode)
        with self.editor.transaction("Change point type"):
            self._vector_commit_path(item, path)
            self._vector_rebase(item)
            self.document.touch()
        self.emit("document-changed", "Change point type")
        self.queue_draw()
        return True

    def set_vector_closed(self, closed: bool) -> bool:
        item = self._vector_item()
        if item is None:
            return False
        path = self.vector_nodes(item)
        indices = sorted(self._vector["selected"]) or [0]
        for index in indices:
            path = nodes.set_closed(path, index, closed)
        label = "Close path" if closed else "Open path"
        with self.editor.transaction(label):
            self._vector_commit_path(item, path)
            self._vector_rebase(item)
            self.document.touch()
        self.emit("document-changed", label)
        self.queue_draw()
        return True

    def move_vector_nodes_to(self, x: float | None = None, y: float | None = None) -> bool:
        """The inspector's X and Y for selected points (world coordinates of the first)."""
        item = self._vector_item()
        if item is None or not self._vector["selected"]:
            return False
        path = self.vector_nodes(item)
        first = min(self._vector["selected"])
        wx, wy = geometry.to_world(item, *nodes.point(path[first]))
        target = geometry.to_local(item, wx if x is None else x, wy if y is None else y)
        start = nodes.point(path[first])
        moved = nodes.move_nodes(path, self._vector["selected"], target[0] - start[0], target[1] - start[1])
        with self.editor.transaction("Move points", key=f"move-nodes:{item.id}"):
            self._vector_commit_path(item, moved)
            self._vector_rebase(item)
            self.document.touch()
        self.emit("document-changed", "Move points")
        self.queue_draw()
        return True

    def _draw_vector_edit(self, snapshot: Gtk.Snapshot, view: View, accent: Gdk.RGBA) -> None:
        item = self._vector_item()
        if item is None:
            return
        path = self.vector_nodes(item)
        to_view = lambda lx, ly: view.to_view(*geometry.to_world(item, lx, ly))  # noqa: E731
        outline = gsk_path(geometry.nodes_outlines(path), to_view)
        if outline is not None:
            snapshot.append_stroke(outline, Gsk.Stroke.new(1.0), accent)
        hover = self._vector.get("hover")
        if hover and hover[0] == "segment":
            (segment, t) = hover[1]
            curve = nodes.segment_curve(path, segment)
            builder = Gsk.PathBuilder.new()
            builder.move_to(*to_view(*curve[0]))
            builder.cubic_to(*to_view(*curve[1]), *to_view(*curve[2]), *to_view(*curve[3]))
            snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(2.0), accent)
            mid = nodes._cubic(curve, t)
            px, py = to_view(*mid)
            builder = Gsk.PathBuilder.new()
            builder.add_circle(point(round(px) + 0.5, round(py) + 0.5), 3.5)
            snapshot.append_fill(builder.to_path(), Gsk.FillRule.WINDING, accent)
        selected = self._vector["selected"]
        lines = Gsk.PathBuilder.new()
        dots = []
        for index in self._vector_visible_handles(path):
            nx, ny = to_view(*nodes.point(path[index]))
            for which in ("in", "out"):
                value = nodes.handle(path[index], which)
                if value is None or value == nodes.point(path[index]):
                    continue
                hx, hy = to_view(*value)
                lines.move_to(nx, ny)
                lines.line_to(hx, hy)
                dots.append((hx, hy))
        snapshot.append_stroke(lines.to_path(), Gsk.Stroke.new(1.0), accent)
        for hx, hy in dots:
            self._handle_square(snapshot, hx, hy, accent, round_handle=True, size=6)
        for index, entry in enumerate(path):
            nx, ny = to_view(*nodes.point(entry))
            if index in selected:
                x0, y0 = round(nx) + 0.5, round(ny) + 0.5
                area = Gsk.RoundedRect()
                area.init_from_rect(rect(x0 - 3.5, y0 - 3.5, 7, 7), 1)
                builder = Gsk.PathBuilder.new()
                builder.add_rounded_rect(area)
                shape = builder.to_path()
                snapshot.append_fill(shape, Gsk.FillRule.WINDING, accent)
                snapshot.append_stroke(shape, Gsk.Stroke.new(1.0), _white())
            else:
                self._handle_square(snapshot, nx, ny, accent, size=7)

    # ── renaming a frame from its label ──────────────────────────────────

    def begin_rename(self, item: LayoutObject) -> None:
        """Double-click a frame's label: rename it in place (Enter commits, Escape cancels)."""
        self.end_rename()
        self._select([item.id])
        entry = Gtk.Entry()
        entry.add_css_class("canvas-frame-rename")
        entry.set_text(item.name)
        entry.set_has_frame(True)
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Frame name"])
        entry.rename_id = item.id
        entry.connect("activate", lambda *_: self.end_rename())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: GLib.idle_add(lambda: self.end_rename() or False))
        entry.add_controller(focus)
        entry.set_parent(self)
        self._rename = entry
        root = self.get_root()
        if root is not None:
            outside = Gtk.GestureClick(button=0)
            outside.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            def clicked(_gesture, _count, x, y):
                picked = root.pick(x, y, Gtk.PickFlags.DEFAULT)
                while picked is not None:
                    if picked is entry:
                        return
                    picked = picked.get_parent()
                GLib.idle_add(lambda: (self.end_rename(restore_focus=False) if self._rename is entry else None) or False)
            outside.connect("pressed", clicked)
            root.add_controller(outside)
            self._rename_outside = (root, outside)
        self.queue_allocate()
        entry.grab_focus()
        entry.select_region(0, -1)
        self._queue_view()

    def end_rename(self, *, commit: bool = True, restore_focus: bool = True) -> None:
        entry = self._rename
        if entry is None:
            return
        self._rename = None
        if outside := getattr(self, "_rename_outside", None):
            outside[0].remove_controller(outside[1])
            self._rename_outside = None
        name = entry.get_text().strip()
        object_id = entry.rename_id
        entry.unparent()
        if restore_focus:
            self.grab_focus()
        item = self.document.object(object_id)
        if commit and item is not None and name and name != item.name:
            try:
                self.editor.set_property([object_id], "name", name)
            except EditError:
                return
            self.emit("document-changed", f"Rename {name}")
        self._queue_view()

    def _allocate_rename(self) -> None:
        entry = self._rename
        if entry is None:
            return
        item = self.document.object(entry.rename_id)
        labels = {value.id: area for value, area, _ in self.frame_labels()} if item is not None else {}
        area = labels.get(entry.rename_id)
        if area is None:
            x, y = (self.world_to_view(item.x, item.y) if item is not None else (8, 8))
            area = rect(x, y - LABEL_GAP - LABEL_HEIGHT, 160, LABEL_HEIGHT)
        _minimum, natural = entry.get_preferred_size()
        height = max(natural.height, 24)
        width = max(160, int(area.get_width()) + 24)
        transform = Gsk.Transform.new().translate(point(round(area.get_x()) - 6,
                                                        round(area.get_y() + area.get_height() - height + 4)))
        entry.allocate(width, height, -1, transform)

    # ── clipboard (spec 12) ──────────────────────────────────────────────

    def copy_selection(self, *, cut: bool = False) -> bool:
        """Put the selection on the clipboard as Canvas layers, SVG and PNG."""
        ids = list(self.document.selected_ids)
        payload = clipboard.serialize(self.document, ids)
        if payload is None:
            return False
        providers = [Gdk.ContentProvider.new_for_bytes(clipboard.MIME, GLib.Bytes.new(clipboard.encode(payload)))]
        svg = clipboard.svg_text(self.document, ids)
        if svg:
            providers.append(Gdk.ContentProvider.new_for_bytes("image/svg+xml", GLib.Bytes.new(svg.encode("utf-8"))))
        texture = self.render_texture(ids, scale=2.0)
        if texture is not None:
            providers.append(Gdk.ContentProvider.new_for_value(texture))
        self._local_clipboard = payload
        self.get_clipboard().set_content(Gdk.ContentProvider.new_union(providers))
        if cut:
            try:
                self.editor.delete([root.id for root in clipboard.roots_of(self.document, ids)])
            except EditError:
                return True
            self.emit("selection-changed")
            self.emit("document-changed", "Cut")
            self.queue_draw()
        return True

    def render_texture(self, ids: list[str], *, scale: float = 1.0) -> Gdk.Texture | None:
        """The given layers (with their descendants) rendered on transparency."""
        native = self.get_native()
        renderer = native.get_renderer() if native is not None else None
        roots = clipboard.roots_of(self.document, ids)
        if renderer is None or not roots:
            return None
        family = []
        for root in roots:
            family.append(root)
            family.extend(self.document.descendants(root.id))
        order = {item.id: index for index, item in enumerate(self.document.objects)}
        family.sort(key=lambda item: order[item.id])
        bounds = geometry.union_bounds(roots)
        width = max(1.0, bounds[2] - bounds[0])
        height = max(1.0, bounds[3] - bounds[1])
        subset = type("Subset", (), {})()
        subset.objects = family
        subset.assets = self.document.assets
        subset.embedded_assets = self.document.embedded_assets
        node = SceneRenderer(self).scene(subset)
        if node is None:
            return None
        snapshot = Gtk.Snapshot()
        snapshot.scale(scale, scale)
        snapshot.translate(point(-bounds[0], -bounds[1]))
        snapshot.append_node(node)
        composed = snapshot.to_node()
        if composed is None:
            return None
        return renderer.render_texture(composed, rect(0, 0, width * scale, height * scale))

    def _view_rect(self) -> tuple[float, float, float, float]:
        return self._visible_world(self.get_width(), self.get_height(), self.view, 0.0)

    def paste(self, *, over_selection: bool = False) -> None:
        """Paste Canvas layers, SVG, an image or text from the clipboard."""
        board = self.get_clipboard()
        formats = board.get_formats()
        local = getattr(self, "_local_clipboard", None)
        if board.is_local() and local is not None:
            self._paste_payload(local, over_selection)
            return
        if formats.contain_mime_type(clipboard.MIME):
            self._read_bytes(board, clipboard.MIME, lambda data: self._paste_payload(clipboard.decode(data), over_selection))
        elif formats.contain_mime_type("image/svg+xml"):
            self._read_bytes(board, "image/svg+xml", lambda data: self._paste_svg(data.decode("utf-8", "replace"), over_selection))
        elif formats.contain_gtype(Gdk.Texture) or any(formats.contain_mime_type(mime) for mime in ("image/png", "image/jpeg")):
            board.read_texture_async(None, self._texture_read)
        elif formats.contain_gtype(GObject.TYPE_STRING) or formats.contain_mime_type("text/plain"):
            board.read_text_async(None, lambda source, result: self._paste_text(source, result, over_selection))

    def _read_bytes(self, board: Gdk.Clipboard, mime: str, done: Callable[[bytes], None]) -> None:
        def opened(source: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
            try:
                stream, _mime = source.read_finish(result)
            except GLib.Error:
                return
            output = Gio.MemoryOutputStream.new_resizable()

            def spliced(target: Gio.MemoryOutputStream, splice_result: Gio.AsyncResult) -> None:
                try:
                    target.splice_finish(splice_result)
                except GLib.Error:
                    return
                done(bytes(target.steal_as_bytes().get_data() or b""))
            output.splice_async(stream, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET,
                                GLib.PRIORITY_DEFAULT, None, spliced)
        board.read_async([mime], GLib.PRIORITY_DEFAULT, None, opened)

    def _paste_payload(self, payload: dict[str, Any] | None, over_selection: bool) -> None:
        if not payload:
            return
        self.end_text_edit()
        try:
            roots = clipboard.paste(self.editor, payload, over_selection=over_selection, view_rect=self._view_rect())
        except (EditError, ValueError, TypeError) as error:
            print(f"Canvas could not paste: {error}")
            return
        count = len(roots)
        self.emit("selection-changed")
        self.emit("document-changed", "Paste" if count == 1 else f"Paste {count} layers")
        self.queue_draw()

    def _paste_svg(self, text: str, over_selection: bool) -> None:
        payload = clipboard.svg_payload(text)
        if payload is None:
            return
        # An SVG has no meaningful position of its own: center it in the view.
        view = self._view_rect()
        left, top, right, bottom = payload["bounds"]
        shift_x = (view[0] + view[2]) / 2 - (left + right) / 2
        shift_y = (view[1] + view[3]) / 2 - (top + bottom) / 2
        for value in payload["objects"]:
            value["x"] += shift_x
            value["y"] += shift_y
        payload["bounds"] = [left + shift_x, top + shift_y, right + shift_x, bottom + shift_y]
        self._paste_payload(payload, over_selection)

    def _texture_read(self, board: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        try:
            texture = board.read_texture_finish(result)
        except GLib.Error:
            return
        if texture is None:
            return
        view = self._view_rect()
        data = bytes(texture.save_to_png_bytes().get_data())
        clipboard.image_layer(self.editor, data, "image/png", texture.get_width(), texture.get_height(),
                              center=((view[0] + view[2]) / 2, (view[1] + view[3]) / 2),
                              max_size=((view[2] - view[0]) * 0.8, (view[3] - view[1]) * 0.8))
        self.emit("selection-changed")
        self.emit("document-changed", "Paste image")
        self.queue_draw()

    def _paste_text(self, board: Gdk.Clipboard, result: Gio.AsyncResult, over_selection: bool) -> None:
        try:
            text = board.read_text_finish(result)
        except GLib.Error:
            return
        if not text:
            return
        stripped = text.lstrip()
        if stripped.startswith("<svg") or (stripped.startswith("<?xml") and "<svg" in stripped[:400]):
            self._paste_svg(text, over_selection)
            return
        view = self._view_rect()
        center = ((view[0] + view[2]) / 2, (view[1] + view[3]) / 2)
        item = self.editor.create("text", center[0], center[1], 1.0, 1.0, name="Text", text=text)
        item.metadata = {**item.metadata, "sizing": "auto_width"}
        self._fit_text(item)
        item.x -= item.width / 2
        item.y -= item.height / 2
        self.emit("selection-changed")
        self.emit("document-changed", "Paste text")
        self.queue_draw()

    def group_selection(self) -> bool:
        """Ctrl+G."""
        try:
            self.editor.group(list(self.document.selected_ids))
        except EditError:
            return False
        self.emit("selection-changed")
        self.emit("document-changed", "Group selection")
        self.queue_draw()
        return True

    def ungroup_selection(self) -> bool:
        """Ctrl+Shift+G."""
        try:
            self.editor.ungroup(list(self.document.selected_ids))
        except EditError:
            return False
        self.emit("selection-changed")
        self.emit("document-changed", "Ungroup")
        self.queue_draw()
        return True

    def frame_selection(self) -> bool:
        """Ctrl+Alt+G: wrap the selection in a frame."""
        try:
            self.editor.group(list(self.document.selected_ids), kind="frame")
        except EditError:
            return False
        self.emit("selection-changed")
        self.emit("document-changed", "Frame selection")
        self.queue_draw()
        return True

    def boolean_selection(self, operation: str) -> bool:
        """Union, subtract, intersect or exclude the selection (a live boolean)."""
        ids = [item.id for item in self.document.selection if not item.locked]
        try:
            item = self.editor.boolean(ids, operation)
        except EditError as error:
            print(f"Canvas could not combine the selection: {error}")
            return False
        self.emit("selection-changed")
        self.emit("document-changed", item.name)
        self.queue_draw()
        return True

    def flatten_selection(self) -> bool:
        """Ctrl+E: the selection as one vector."""
        ids = [item.id for item in self.document.selection if not item.locked]
        if not ids:
            return False
        try:
            self.editor.flatten(ids)
        except EditError as error:
            print(f"Canvas could not flatten the selection: {error}")
            return False
        self.emit("selection-changed")
        self.emit("document-changed", "Flatten")
        self.queue_draw()
        return True

    def toggle_mask_selection(self) -> bool:
        """Ctrl+Alt+M: use the selection's bottom layer as a mask, or release a mask."""
        selection = [item for item in self.document.selection if not item.locked]
        if not selection:
            return False
        releasing = all(geometry.mask_mode(item) or (
            item.metadata.get("mask_group") and any(geometry.mask_mode(child) for child in self.document.children(item.id)))
            for item in selection)
        try:
            if releasing:
                self.editor.release_mask([item.id for item in selection])
                label = "Release mask"
            else:
                self.editor.use_as_mask([item.id for item in selection])
                label = "Use as mask"
        except EditError as error:
            print(f"Canvas could not change the mask: {error}")
            return False
        self.emit("selection-changed")
        self.emit("document-changed", label)
        self.queue_draw()
        return True

    def duplicate_selection(self) -> bool:
        ids = [root.id for root in clipboard.roots_of(self.document, self.document.selected_ids)]
        try:
            self.editor.duplicate(ids, offset=10.0, rename=False)
        except EditError:
            return False
        self.emit("selection-changed")
        self.emit("document-changed", "Duplicate")
        self.queue_draw()
        return True

    # ── text editing on the canvas ───────────────────────────────────────

    def _text_item(self) -> LayoutObject | None:
        return self.document.object(self._text.item_id) if self._text is not None else None

    def _text_layout(self, item: LayoutObject):
        session = self._text
        text = item.text
        if session is not None and session.preedit:
            text = session.text[:session.cursor] + session.preedit + session.text[session.cursor:]
        return self.renderer.text_layout(item, text)

    def begin_text_edit(self, item: LayoutObject, x: float | None = None, y: float | None = None, *,
                        created: bool = False, select_all: bool = False, select_word: bool = False) -> None:
        """Edit a text layer in place: caret at the point, typing through the input method."""
        if self._text is not None:
            self.end_text_edit()
        if not created:
            self.editor.begin_gesture(f"Edit {item.name}")
        self._text = text_edit.TextSession.begin(item, created=created, select_all=select_all)
        self.document.select([item.id])
        if x is not None and y is not None and not select_all:
            layout = self._text_layout(item)
            lx, ly = geometry.to_local(item, *self.view_to_world(x, y))
            index = text_edit.index_at(layout, lx, ly)
            if select_word and self._text.text:
                self._text.select_word(index)
            else:
                self._text.set_cursor(index)
        self.tool = "select"
        self._suspend_accels()
        self._keys.set_im_context(self._im)
        self._im.set_client_widget(self)
        self._im.focus_in()
        self.grab_focus()
        self._restart_caret()
        self._update_im_location()
        self.set_cursor_from_name("text")
        self.emit("selection-changed")
        self.emit("text-editing-changed", True)
        self.queue_draw()

    def end_text_edit(self, *, commit: bool = True) -> None:
        session = self._text
        if session is None:
            return
        self._text = None
        self._restore_accels()
        self._keys.set_im_context(None)
        self._im.focus_out()
        self._im.reset()
        self._im.set_client_widget(None)
        if self._caret_source:
            GLib.source_remove(self._caret_source)
            self._caret_source = 0
        item = self.document.object(session.item_id)
        if item is not None:
            item.text = session.text
            self._fit_text(item)
        empty = item is None or not session.text.strip()
        if session.created and empty:
            self.editor.cancel_gesture()
            self.document.select([])
            label = None
        elif not commit:
            self.editor.cancel_gesture()
            label = None
        else:
            label = self.editor._label or "Edit text"
            if not self.editor.end_gesture():
                label = None
        self._update_cursor()
        self.emit("text-editing-changed", False)
        self.emit("selection-changed")
        if label:
            self.emit("document-changed", label)
        self.queue_draw()

    def fit_text(self, item: LayoutObject) -> None:
        """Resize an auto width or auto height text layer to its words."""
        self._fit_text(item)

    # Application accelerators run before the focused widget sees a key, so
    # while typing they are lifted (Ctrl+Z must undo typing, not the document).
    KEEP_ACCELS = ("<Control>s", "<Control>q", "<Control>w", "<Control>n", "<Control>o")

    def _suspend_accels(self) -> None:
        root = self.get_root()
        application = root.get_application() if isinstance(root, Gtk.Window) else None
        if application is None or getattr(self, "_saved_accels", None):
            return
        saved = {}
        for name in application.list_action_descriptions():
            accels = application.get_accels_for_action(name)
            if accels and not all(accel in self.KEEP_ACCELS for accel in accels):
                saved[name] = list(accels)
                application.set_accels_for_action(name, [accel for accel in accels if accel in self.KEEP_ACCELS])
        self._saved_accels = (application, saved)

    def _restore_accels(self) -> None:
        saved = getattr(self, "_saved_accels", None)
        if not saved:
            return
        application, accels = saved
        for name, values in accels.items():
            application.set_accels_for_action(name, values)
        self._saved_accels = None

    def _fit_text(self, item: LayoutObject) -> None:
        size = fitted_size(self.renderer._pango_context(), item)
        if size is not None:
            item.width, item.height = size

    def _apply_text(self) -> None:
        item = self._text_item()
        if item is None:
            self.end_text_edit()
            return
        session = self._text
        # The composing (preedit) text shows and sizes the box until committed.
        item.text = session.text[:session.cursor] + session.preedit + session.text[session.cursor:]
        self._fit_text(item)
        self._restart_caret()
        self._update_im_location()
        self.queue_draw()
        self.emit("transform-preview")

    def _restart_caret(self) -> None:
        self._caret_on = True
        if self._caret_source:
            GLib.source_remove(self._caret_source)

        def blink() -> bool:
            self._caret_on = not self._caret_on
            self._queue_view()
            return GLib.SOURCE_CONTINUE
        settings = Gtk.Settings.get_default()
        blinking = settings.props.gtk_cursor_blink if settings else True
        self._caret_source = GLib.timeout_add(530, blink) if blinking else 0

    def _update_im_location(self) -> None:
        item = self._text_item()
        if item is None:
            return
        cx, cy, _cw, ch = text_edit.caret_rect(self._text_layout(item), self._text.cursor)
        x0, y0 = self.world_to_view(*geometry.to_world(item, cx, cy))
        area = Gdk.Rectangle()
        area.x, area.y = int(x0), int(y0)
        area.width, area.height = 1, max(1, int(ch * self.document.zoom))
        self._im.set_cursor_location(area)

    def _text_press(self, x: float, y: float, count: int, shift: bool) -> bool:
        item = self._text_item()
        if item is None:
            return False
        lx, ly = geometry.to_local(item, *self.view_to_world(x, y))
        margin = 4 / max(self.document.zoom, 1e-6)
        if not (-margin <= lx <= item.width + margin and -margin <= ly <= item.height + margin):
            return False
        session = self._text
        index = text_edit.index_at(self._text_layout(item), lx, ly)
        if count == 2:
            session.select_word(index)
        elif count >= 3:
            session.select_paragraph(index)
        else:
            session.set_cursor(index, extend=shift)
        self._restart_caret()
        self._queue_view()
        return True

    def _text_drag(self, x: float, y: float) -> None:
        item = self._text_item()
        if item is None or self._press is None or self._press.get("count", 1) > 1:
            return
        lx, ly = geometry.to_local(item, *self.view_to_world(x, y))
        self._text.set_cursor(text_edit.index_at(self._text_layout(item), lx, ly), extend=True)
        self._restart_caret()
        self._queue_view()

    def _text_key(self, name: str, keyval: int, shift: bool, ctrl: bool, alt: bool) -> bool:
        session = self._text
        item = self._text_item()
        if session is None or item is None:
            return False
        layout = self._text_layout(item)
        lower = name.lower()
        if name == "Escape":
            self.end_text_edit()
            return True
        if ctrl and not alt:
            if lower == "a":
                session.select_all()
            elif lower in {"c", "x"}:
                if session.has_selection:
                    self.get_clipboard().set(session.selected_text())
                    if lower == "x":
                        session.delete(-1)
                        self._apply_text()
            elif lower == "v":
                self.get_clipboard().read_text_async(None, self._text_pasted)
            elif lower == "z":
                changed = session.redo() if shift else session.undo()
                if changed:
                    self._apply_text()
            elif lower == "y":
                if session.redo():
                    self._apply_text()
            elif lower in {"b", "i", "u"}:
                self._toggle_text_style(item, lower)
            elif name in {"Left", "Right"}:
                session.move("word_left" if name == "Left" else "word_right", extend=shift)
            elif name in {"Home", "End"}:
                session.move("doc_start" if name == "Home" else "doc_end", extend=shift)
            elif name in {"BackSpace", "Delete"}:
                session.delete(-1 if name == "BackSpace" else 1, word=True)
                self._apply_text()
            elif name in {"Return", "KP_Enter"}:
                self.end_text_edit()
            else:
                # Ctrl+S and other commands still reach the window.
                return lower not in {"s", "q", "w", "k", "n", "o"}
            self._restart_caret()
            self._queue_view()
            return True
        moves = {"Left": "left", "Right": "right", "Up": "up", "Down": "down", "Home": "home", "End": "end",
                 "KP_Left": "left", "KP_Right": "right", "KP_Up": "up", "KP_Down": "down",
                 "Page_Up": "doc_start", "Page_Down": "doc_end"}
        if name in moves:
            session.move(moves[name], extend=shift, layout=layout)
        elif name in {"BackSpace", "Delete", "KP_Delete"}:
            session.delete(-1 if name == "BackSpace" else 1)
            self._apply_text()
        elif name in {"Return", "KP_Enter"}:
            session.insert("\n")
            self._apply_text()
        elif name in {"Tab", "ISO_Left_Tab"}:
            session.insert("\t")
            self._apply_text()
        else:
            character = chr(Gdk.keyval_to_unicode(keyval)) if Gdk.keyval_to_unicode(keyval) else ""
            if character and character.isprintable() and not alt:
                # Only when no input method is attached (tests); the IM commits otherwise.
                session.insert(character)
                self._apply_text()
            return True
        self._restart_caret()
        self._update_im_location()
        self._queue_view()
        return True

    def _text_context_menu(self, x: float, y: float) -> None:
        """Right-click while editing text: Cut, Copy, Paste, Select All."""
        group = Gio.SimpleActionGroup()

        def action(name: str, callback: Callable[[], None], enabled: bool = True) -> None:
            value = Gio.SimpleAction.new(name, None)
            value.set_enabled(enabled)
            value.connect("activate", lambda *_: (callback(), self.grab_focus()))
            group.add_action(value)
        session = self._text
        action("cut", lambda: self._text_key("x", 0, False, True, False), session.has_selection)
        action("copy", lambda: self._text_key("c", 0, False, True, False), session.has_selection)
        action("paste", lambda: self._text_key("v", 0, False, True, False))
        action("select-all", lambda: self._text_key("a", 0, False, True, False))
        self.insert_action_group("text", group)
        menu = Gio.Menu()
        for label, name, accel in (("Cut", "cut", "<Control>x"), ("Copy", "copy", "<Control>c"),
                                   ("Paste", "paste", "<Control>v"), ("Select All", "select-all", "<Control>a")):
            item = Gio.MenuItem.new(label, f"text.{name}")
            item.set_attribute_value("accel", GLib.Variant.new_string(accel))
            menu.append_item(item)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_has_arrow(False)
        popover.set_parent(self)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
        popover.set_pointing_to(area)
        popover.connect("closed", lambda widget: GLib.idle_add(lambda: widget.unparent() or False))
        popover.popup()

    def _text_pasted(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        try:
            value = clipboard.read_text_finish(result)
        except GLib.Error:
            return
        if value and self._text is not None:
            self._text.insert(value.replace("\r\n", "\n"))
            self._apply_text()

    def _toggle_text_style(self, item: LayoutObject, key: str) -> None:
        metadata = dict(item.metadata)
        if key == "b":
            metadata["font_weight"] = 400 if float(metadata.get("font_weight", 400)) >= 600 else 700
        elif key == "i":
            metadata["italic"] = not metadata.get("italic", False)
        else:
            metadata["decoration"] = "none" if metadata.get("decoration") == "underline" else "underline"
        item.metadata = metadata
        self._apply_text()

    def _im_commit(self, _context: Gtk.IMContext, value: str) -> None:
        if self._text is not None:
            self._text.preedit = ""
            self._text.insert(value)
            self._apply_text()

    def _im_preedit(self, context: Gtk.IMContext) -> None:
        if self._text is not None:
            preedit, _attributes, _cursor = context.get_preedit_string()
            self._text.preedit = preedit or ""
            self._apply_text()

    def _im_surrounding(self, context: Gtk.IMContext) -> bool:
        if self._text is None:
            return False
        session = self._text
        encoded = session.text.encode("utf-8")
        # PyGObject takes the byte length explicitly; indices are UTF-8 byte offsets.
        context.set_surrounding_with_selection(session.text, len(encoded),
                                               len(session.text[:session.cursor].encode("utf-8")),
                                               len(session.text[:session.anchor].encode("utf-8")))
        return True

    def _im_delete_surrounding(self, _context: Gtk.IMContext, offset: int, count: int) -> bool:
        # Offsets and counts here are characters from the caret (GTK documents
        # delete-surrounding in chars).
        if self._text is None:
            return False
        session = self._text
        start = max(0, min(len(session.text), session.cursor + offset))
        session.text = session.text[:start] + session.text[start + count:]
        session.cursor = session.anchor = start
        self._apply_text()
        return True

    def _finish_text_draw(self, press: dict[str, Any], clicked: bool) -> None:
        """Text tool: click for auto width text, drag for a fixed box; then type."""
        if not clicked:
            item = self.document.object(press.get("item"))
            if item is None:
                self.editor.cancel_gesture()
                return
            item.text = ""
            item.metadata = {**item.metadata, "sizing": "auto_height"}
            self._fit_text(item)
            item.height = max(item.height, abs(press.get("last_world", press["world"])[1] - press["anchor"][1]))
            self.editor.set_gesture_label("Create Text")
        else:
            wx, wy = press["world"]
            self.editor.begin_gesture("Create Text")
            try:
                item = self.editor.create("text", wx, wy, 1.0, 1.0, parent_id=self._parent_for_new(wx, wy),
                                          name="Text", text="")
            except EditError:
                self.editor.cancel_gesture()
                return
            item.metadata = {**item.metadata, "sizing": "auto_width"}
            font_size = float(item.metadata.get("font_size", 16))
            item.y -= font_size * 0.6
            self._fit_text(item)
        self.tool = "select"
        self.emit("tool-changed", "select")
        self.begin_text_edit(item, created=True)

    def _draw_text_editing(self, snapshot: Gtk.Snapshot, view: View, accent: Gdk.RGBA) -> None:
        item = self._text_item()
        session = self._text
        if item is None or session is None:
            return
        self._stroke_outline(snapshot, item, view, accent, 1.0)
        layout = self._text_layout(item)
        snapshot.save()
        snapshot.translate(point(view.x, view.y))
        snapshot.scale(view.zoom, view.zoom)
        SceneRenderer._apply_transform(snapshot, item)
        start, end = session.selection
        highlight = Gdk.RGBA(red=accent.red, green=accent.green, blue=accent.blue, alpha=0.28)
        for left, top, width, height in text_edit.selection_rects(layout, start, end):
            snapshot.append_color(highlight, rect(left, top, width, height))
        if self._caret_on and not session.has_selection:
            cx, cy, _cw, ch = text_edit.caret_rect(layout, session.cursor + len(session.preedit))
            caret_width = 1.0 / max(view.zoom * self.device_scale(), 1e-6)
            ink = color_of(item.fill)
            caret = rgba(ink.with_alpha(1.0)) if ink is not None and ink.a > 0.2 else accent
            snapshot.append_color(caret, rect(cx - caret_width / 2, cy, caret_width, ch))
        snapshot.restore()

    # ── test hook (spec 18.2) ────────────────────────────────────────────

    def test_inject(self, event: dict[str, Any]) -> None:
        """Drive the input router with synthetic events.

        press, motion and release take `x`, `y`, `button` and `state`;
        scroll takes `dx`, `dy`, `smooth` and `state`; zoom takes `scale`,
        `x`, `y`; key takes `name`, `state`, and `release`; focus takes
        `entered`.
        """
        kind = event["type"]
        state = Gdk.ModifierType(event.get("state", 0))
        if kind == "press":
            self._fake_gesture = _FakeGesture(event.get("button", 1), state, (event["x"], event["y"]))
            self._drag_begin(self._fake_gesture, event["x"], event["y"])
        elif kind == "motion":
            fake = self._fake_gesture
            if fake is None or self._press is None:
                self._pointer = (event["x"], event["y"])
                self._update_hover(event["x"], event["y"], state)
                return
            fake.state = state
            self._drag_update(fake, event["x"] - fake.origin[0], event["y"] - fake.origin[1])
        elif kind == "release":
            fake = self._fake_gesture
            if fake is None:
                return
            fake.state = state
            self._fake_gesture = None
            self._drag_end(fake, event["x"] - fake.origin[0], event["y"] - fake.origin[1])
        elif kind == "scroll":
            self._scroll(_FakeScroll(state, event.get("smooth", False)), event.get("dx", 0.0), event.get("dy", 0.0))
        elif kind == "zoom":
            self.pinch_to(event["scale"], event["x"], event["y"])
        elif kind == "focus":
            if event.get("entered", False):
                self._focus_entered()
            else:
                self._focus_left()
        elif kind == "type":
            if self._text is not None:
                self._text.insert(event["text"])
                self._apply_text()
        elif kind == "key":
            keyval = Gdk.keyval_from_name(event["name"])
            if event.get("release"):
                self._key_released(None, keyval, 0, state)
            else:
                self._key_pressed(None, keyval, 0, state)


class _FakeGesture:
    def __init__(self, button: int, state: Gdk.ModifierType, origin: tuple[float, float]) -> None:
        self.button, self.state, self.origin = button, state, origin

    def get_current_button(self) -> int:
        return self.button

    def get_current_event_state(self) -> Gdk.ModifierType:
        return self.state


class _FakeScroll:
    def __init__(self, state: Gdk.ModifierType, smooth: bool) -> None:
        self.state, self.smooth = state, smooth

    def get_current_event_state(self) -> Gdk.ModifierType:
        return self.state

    def get_unit(self) -> Gdk.ScrollUnit:
        return Gdk.ScrollUnit.SURFACE if self.smooth else Gdk.ScrollUnit.WHEEL
