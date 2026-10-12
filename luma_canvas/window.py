# SPDX-License-Identifier: Apache-2.0
"""Canvas document window and its editing callbacks.

The v70 composition is a CreativeWorkspace holding a floating Layers panel,
selection inspector, bottom ToolBar and ZoomControl. Until the kit publishes
those APIs, the prior island composition below remains the runnable shell.
TODO(kit-request canvas-01-creative-workspace): compose those shared parts and
remove the superseded visual rules from content.css.
"""

from __future__ import annotations

import inspect
import math
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Callable

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, LumaSemantics, LumaUI, Pango
from luma_appkit import (
    ActionCenter, BarAction, BarWidget, CornerPill, TitleIsland, PanelHeading,
    AppWindow,
    Command,
    CommandGroup,
    CommandRegistry,
    Collaborator,
    ConnectedButtonGroup,
    DocumentHeader,
    IconButton,
    InputMode,
    Island,
    PresentationMode,
    Person,
    SaveStatus,
    ShareSheet,
    ShareSubject,
    Toast,
    ToastHost,
    ZoomControl,
    add_style_sheet,
    command_popover,
    install_appkit,
)

from .canvas import LayoutCanvas
from .design_controls import auto_layout_selection, component_selection
from .creative_assets import apply_color, apply_text_style, choose_photo, insert_component_preset
from .creative_data import CREATIVE_ICONS, kit_layers, layer_move_target
from .documents import DocumentSession, SaveState, new_document
from .editing import EditError, Editor
from .export import export
from .gutter import ResizeGutter
from .inspector import Inspector
from .layers import LayersPanel, drop_target
from . import model as _model
from .model import Asset, Document, LayoutObject, new_id
from .persistence import APP_ID, DOCUMENT_SUFFIX, LEGACY_DOCUMENT_SUFFIXES
from .viewport import fit_rect_with_insets

APP_NAME = "Canvas HOTFIX" if os.environ.get("LUMA_CANVAS_HOTFIX") == "1" else "Canvas"

# Panel widths: minimum, maximum, default (CANVAS-SPEC 14.1).
NAVIGATION_WIDTHS = (180, 360, 200)
INSPECTOR_WIDTHS = (240, 320, 248)
RAIL_WIDTH = 40
RAIL_TO_PANEL = 9  # the kit's gutter between the rail and the Layers island

# v70 tools while the shared ToolBar lands: (tool, label, key, Lucide icon).
# The shape button offers v70's rectangle, ellipse and line flyout.
RAIL_TOOLS = (
    ("select", "Move", "V", "lumaui-mouse-pointer-2-symbolic"),
    ("frame", "Frame", "F", "lumaui-hash-symbolic"),
    ("shape", "Shapes", "R", "lumaui-square-symbolic"),
    ("path", "Pen", "P", "lumaui-pen-tool-symbolic"),
    ("text", "Text", "T", "lumaui-type-symbolic"),
    ("image", "Image", "I", "lumaui-image-symbolic"),
    ("hand", "Hand", "H", "lumaui-hand-symbolic"),
    ("comment", "Comment", "C", "lumaui-message-circle-symbolic"),
)
SHAPE_TOOLS = {
    "rectangle": ("Rectangle", "R", "lumaui-square-symbolic"),
    "ellipse": ("Ellipse", "O", "lumaui-circle-symbolic"),
    "line": ("Line", "L", "lumaui-minus-symbolic"),
    "arrow": ("Arrow", "A", "lumaui-arrow-up-right-symbolic"),
}
TOOL_KEYS = {"v": "select", "k": "scale", "a": "arrow", "f": "frame", "p": "path", "r": "rectangle",
             "o": "ellipse", "l": "line", "t": "text", "i": "image", "h": "hand", "c": "comment"}
ZOOM_LADDER = (0.01, 0.02, 0.03, 0.06, 0.125, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256)
FIT_MARGIN = 48.0
# The model's own zoom range (1% to 25600% once the interaction stream lands).
ZOOM_LIMITS = (getattr(_model, "ZOOM_MIN", 0.05), getattr(_model, "ZOOM_MAX", 64.0))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class CanvasWindow(AppWindow):
    _content_styles: dict = {}

    def __init__(self, application: Adw.Application, path: Path | None = None, *,
                 show_welcome: bool = True, document: Document | None = None,
                 recovered: Any = None, snapshot: dict | None = None,
                 fixture_mode: bool = False) -> None:
        LumaUI.init()
        install_appkit()
        display = Gdk.Display.get_default()
        if display not in self._content_styles:
            self._content_styles[display] = add_style_sheet(str(Path(__file__).with_name("content.css")))
        self._install_icon_path()
        if document is None and path is not None:
            # Callers open files through CanvasApplication.open_path, which
            # shows an alert for a file that cannot be read. Direct
            # construction (tests, scripts) raises the same errors.
            from .importers import import_svg
            from .persistence import DocumentStore
            if path.suffix.lower() == ".svg":
                document, path = import_svg(path), None
            else:
                document, snapshot = DocumentStore.load_with_snapshot(path)
        welcome_active = (path is None and document is None and recovered is None and show_welcome
                          and LumaUI.WelcomeView.should_show(APP_ID))
        commands = self._command_registry(application)
        super().__init__(
            application=application, app_id=APP_ID, title=APP_NAME, icon_name=APP_ID, commands=commands,
            default_width=900 if welcome_active else 1440, default_height=560 if welcome_active else 900,
            geometry_scope="welcome" if welcome_active else "", minimum_width=320, minimum_height=294,
        )
        self._install_app_menu(application)
        if self.context.presentation == PresentationMode.FULLSCREEN:
            self.fullscreen()
        self.add_css_class("canvas-window")
        if self.context.input_mode == InputMode.TOUCH:
            self.add_css_class("canvas-touch")
        if recovered is not None:
            document, path = recovered.document, recovered.path
        self._fixture_mode = fixture_mode
        self._header_status = "Canvas · Saved"
        self.editor = Editor(document if document is not None else new_document())
        self.path = path
        if fixture_mode:
            from .fixture import FixtureSession
            self.session = FixtureSession(self.editor.document)
        else:
            self.session = DocumentSession(lambda: self.editor.document, path=path,
                                           on_status=self._save_status_changed,
                                           snapshot=snapshot if recovered is None else None)
        self._tool_buttons: dict[str, Gtk.ToggleButton] = {}
        self._shape_tool = "rectangle"
        self._layout_mode = "wide"
        self._panels_hidden = {"navigation": False, "inspector": False}
        self._history_stamps: dict[int, float] = {}
        self._versions_sheet = None
        self._welcome_active = welcome_active
        self._initial_fit_done = False
        self._creative_active = False
        self._closing_confirmed = False
        self.large_nudge = 10.0
        self._build_actions()
        self._build_ui()
        self._build_title_actions()
        if welcome_active:
            self.welcome = LumaUI.WelcomeView.new(
                "Canvas", APP_ID,
                "Draw, lay out and export. Vectors, type and images in one place, sharp at any size.",
                "design", APP_ID)
            self.welcome.get_new_button().connect("clicked", lambda *_: self._new())
            self.welcome.get_open_button().connect("clicked", lambda *_: self._open())
            self.pages.add_named(self.welcome, "welcome")
            self.pages.set_visible_child_name("welcome")
        self._build_semantics()
        self._refresh_all()
        if fixture_mode:
            self._apply_fixture_state(os.environ.get("LUMA_CANVAS_FIXTURE_STATE", "initial"))
        if path is not None and recovered is None:
            self.session.opened()
        if recovered is not None:
            self.session.adopt_recovered(recovered.pre_recovery)
            self._show_banner("Canvas recovered changes from when it last quit unexpectedly.",
                              "Show versions", self._browse_versions)
        self.connect("close-request", self._close_requested)
        self.connect("notify::is-active", self._activity_changed)
        try:
            from .font_picker import watch_font_changes
            watch_font_changes(lambda: self.inspector.refresh(rebuild=True))
        except (ImportError, ValueError):
            pass
        if path is not None or recovered is not None:
            GLib.idle_add(lambda: (self._warn_missing_fonts(), False)[1])
        self.connect("map", self._initial_map)

    # ── basics ──────────────────────────────────────────────────────────

    @property
    def document(self) -> Document:
        return self.editor.document

    def supports(self, capability: str) -> bool:
        """Capabilities the interaction stream's canvas may add."""
        if capability == "text-editing-on-canvas":
            return bool(getattr(self.canvas, "supports_text_editing", False))
        return bool(getattr(self.canvas, f"supports_{capability.replace('-', '_')}", False))

    def _install_icon_path(self) -> None:
        """Canvas ships its glyphs; no installed theme carries them."""
        display = Gdk.Display.get_default()
        if display is None:
            return
        for candidate in (
            os.environ.get("LUMA_CANVAS_ICON_PATH", ""),
            str(Path(__file__).resolve().parent.parent / "data" / "icons"),
            "/app/share/luma-canvas/icons",
            "/usr/share/luma-canvas/icons",
        ):
            if candidate and Path(candidate).is_dir():
                Gtk.IconTheme.get_for_display(display).add_search_path(candidate)
                return

    def _command_registry(self, application: Adw.Application) -> CommandRegistry:
        def has_selection() -> bool:
            return bool(getattr(self, "editor", None) and self.document.selected_ids)

        def selected(count: int) -> Callable[[], bool]:
            return lambda: bool(getattr(self, "editor", None)) and len(self.document.selected_ids) >= count

        def has_container() -> bool:
            return has_selection() and any(item.kind in {"group", "frame"} for item in self.document.selection)

        def has_stroke() -> bool:
            return has_selection() and any(item.kind == "path" and item.stroke and item.stroke_width
                                           for item in self.document.selection)

        def combinable() -> bool:
            if not has_selection():
                return False
            selection = self.document.selection
            return len(selection) >= 2 or (len(selection) == 1 and selection[0].kind == "boolean")

        def canvas_can(method: str) -> Callable[[], bool]:
            return lambda: hasattr(getattr(self, "canvas", None), method)

        return CommandRegistry((
            CommandGroup(None, (
                Command("document.new", "New document", self._new, "document-new-symbolic", shortcut=("Ctrl", "N")),
                Command("document.open", "Open…", self._open, "document-open-symbolic", shortcut=("Ctrl", "O")),
                Command("document.browse-files", "Browse files…", self._browse_files, "folder-open-symbolic"),
                Command("document.export", "Export…", self._export_dialog, "document-save-as-symbolic",
                        shortcut=("Shift", "Ctrl", "E")),
            ), quick_actions=True),
            CommandGroup("Document", (
                Command("document.save-version", "Save a version", self._save_version, shortcut=("Ctrl", "S")),
                Command("document.duplicate", "Duplicate", self._duplicate_document, shortcut=("Shift", "Ctrl", "S")),
                Command("document.save-copy", "Save a copy…", self._save_copy),
                Command("document.import", "Import…", self._import, shortcut=("Shift", "Ctrl", "I")),
                Command("document.rename", "Rename…", self._focus_rename),
                Command("document.move", "Move…", self._move_document),
                Command("document.revert-last-opened", "Revert to last opened", lambda: self._revert("opened")),
                Command("document.revert-last-saved", "Revert to last saved version", lambda: self._revert("saved"),
                        enabled=lambda: bool(getattr(getattr(self, "session", None), "has_saved", False))),
                Command("document.browse-versions", "Browse versions…", self._browse_versions),
                Command("document.reveal", "Show in Filer", self._reveal_in_files),
                Command("document.trash", "Move to Trash", self._trash_document, destructive=True),
            )),
            CommandGroup("Edit", (
                Command("edit.undo", "Undo", self._undo, "edit-undo-symbolic", shortcut=("Ctrl", "Z")),
                Command("edit.redo", "Redo", self._redo, "edit-redo-symbolic", shortcut=("Shift", "Ctrl", "Z")),
                Command("edit.cut", "Cut", lambda: self.canvas.copy_selection(cut=True),
                        enabled=lambda: has_selection() and canvas_can("copy_selection")()),
                Command("edit.copy", "Copy", lambda: self.canvas.copy_selection(),
                        enabled=lambda: has_selection() and canvas_can("copy_selection")()),
                Command("edit.paste", "Paste", lambda: self.canvas.paste(), enabled=canvas_can("paste")),
                Command("edit.paste-over", "Paste over selection", lambda: self.canvas.paste(over_selection=True),
                        enabled=lambda: has_selection() and canvas_can("paste")()),
                Command("edit.select-all", "Select all", lambda: self.canvas.select_all(), enabled=canvas_can("select_all")),
                Command("edit.duplicate", "Duplicate selection", self._duplicate, shortcut=("Ctrl", "D"), enabled=has_selection),
                Command("edit.rename-layer", "Rename layer", lambda: self.layers.begin_rename(), shortcut=("Ctrl", "R"), enabled=has_selection),
                Command("edit.delete", "Delete", self._delete_selection, "edit-delete-symbolic", destructive=True, enabled=has_selection),
            )),
            CommandGroup("Design", (
                Command("design.auto-layout", "Add auto layout", lambda: auto_layout_selection(self),
                        "layout-template", shortcut=("Shift", "A"), enabled=has_selection),
                Command("design.component", "Create component", lambda: component_selection(self),
                        "component", shortcut=("Ctrl", "Alt", "K"), enabled=has_selection),
            )),
            CommandGroup("Arrange", (
                Command("arrange.align-left", "Align left", lambda: self.align("left"), shortcut=("Alt", "A"), enabled=has_selection),
                Command("arrange.align-center-x", "Align horizontal centers", lambda: self.align("center-x"), shortcut=("Alt", "H"), enabled=has_selection),
                Command("arrange.align-right", "Align right", lambda: self.align("right"), shortcut=("Alt", "D"), enabled=has_selection),
                Command("arrange.align-top", "Align top", lambda: self.align("top"), shortcut=("Alt", "W"), enabled=has_selection),
                Command("arrange.align-center-y", "Align vertical centers", lambda: self.align("center-y"), shortcut=("Alt", "V"), enabled=has_selection),
                Command("arrange.align-bottom", "Align bottom", lambda: self.align("bottom"), shortcut=("Alt", "S"), enabled=has_selection),
                Command("arrange.distribute-x", "Distribute horizontal spacing", lambda: self.align("distribute-x"), shortcut=("Alt", "Shift", "H"), enabled=selected(3)),
                Command("arrange.distribute-y", "Distribute vertical spacing", lambda: self.align("distribute-y"), shortcut=("Alt", "Shift", "V"), enabled=selected(3)),
                Command("arrange.group", "Group selection", lambda: self._group("group"), shortcut=("Ctrl", "G"), enabled=has_selection),
                Command("arrange.ungroup", "Ungroup", lambda: self._group("ungroup"), shortcut=("Ctrl", "Shift", "G"), enabled=has_container),
                Command("arrange.frame", "Frame selection", lambda: self._group("frame"), shortcut=("Ctrl", "Alt", "G"), enabled=has_selection),
                Command("arrange.mask", "Use as mask", lambda: self.canvas.toggle_mask_selection(),
                        shortcut=("Ctrl", "Alt", "M"), enabled=has_selection),
                Command("arrange.forward", "Bring forward", lambda: self._restack("forward"), shortcut=("Ctrl", "bracketright"), enabled=has_selection),
                Command("arrange.backward", "Send backward", lambda: self._restack("backward"), shortcut=("Ctrl", "bracketleft"), enabled=has_selection),
                Command("arrange.front", "Bring to front", lambda: self._restack("front"), shortcut=("Ctrl", "Shift", "bracketright"), enabled=has_selection),
                Command("arrange.back", "Send to back", lambda: self._restack("back"), shortcut=("Ctrl", "Shift", "bracketleft"), enabled=has_selection),
            )),
            CommandGroup("Path", (
                Command("path.union", "Union", lambda: self._boolean("union"), "luma-canvas-boolean-union-symbolic",
                        shortcut=("Alt", "Shift", "U"), enabled=combinable),
                Command("path.subtract", "Subtract", lambda: self._boolean("subtract"),
                        "luma-canvas-boolean-subtract-symbolic", shortcut=("Alt", "Shift", "S"), enabled=combinable),
                Command("path.intersect", "Intersect", lambda: self._boolean("intersect"),
                        "luma-canvas-boolean-intersect-symbolic", shortcut=("Alt", "Shift", "I"), enabled=combinable),
                Command("path.exclude", "Exclude", lambda: self._boolean("exclude"),
                        "luma-canvas-boolean-exclude-symbolic", shortcut=("Alt", "Shift", "E"), enabled=combinable),
                Command("path.flatten", "Flatten", lambda: self.canvas.flatten_selection(),
                        "luma-canvas-flatten-symbolic", shortcut=("Ctrl", "E"), enabled=has_selection),
            )),
            CommandGroup("Shape", (
                Command("path.outline-stroke", "Outline stroke", self._outline_stroke, shortcut=("Ctrl", "Shift", "O"),
                        enabled=has_stroke),
            )),
            CommandGroup("View", (
                Command("view.zoom-in", "Zoom in", lambda: self._zoom_step(1), shortcut=("Ctrl", "plus")),
                Command("view.zoom-out", "Zoom out", lambda: self._zoom_step(-1), shortcut=("Ctrl", "minus")),
                Command("view.zoom-fit", "Zoom to fit", self._zoom_to_fit),
                Command("view.zoom-selection", "Zoom to selection", self._zoom_to_selection, enabled=has_selection),
                Command("view.zoom-100", "Zoom to 100%", lambda: self._zoom_to(1.0), shortcut=("Ctrl", "0")),
                Command("view.grid", "Show grid", self._toggle_grid),
                Command("view.outline", "Outline view", self._toggle_outline, shortcut=("Ctrl", "Y")),
                Command("view.toggle-panels", "Show or hide panels", self._toggle_all_panels, shortcut=("Ctrl", "backslash")),
                Command("view.toggle-layers", "Show or hide layers", lambda: self._toggle_panel("navigation"), shortcut=("Alt", "1")),
                Command("view.toggle-inspector", "Show or hide inspector", lambda: self._toggle_panel("inspector"), shortcut=("Alt", "8")),
                Command("view.find-layer", "Find layers", lambda: self.layers.show_search(), shortcut=("Ctrl", "F")),
                Command("view.commands", "Search commands", lambda: self._show_command_search(), "system-search-symbolic", shortcut=("Ctrl", "K")),
            )),
            CommandGroup(None, (
                Command("application.shortcuts", "Keyboard shortcuts", self._shortcuts),
                Command("application.about", "About Canvas", lambda: application.activate_action("about", None)),
                Command("application.quit", "Quit Canvas", lambda: application.activate_action("quit", None)),
            )),
        ))

    def _build_semantics(self) -> None:
        self.semantic_root = LumaSemantics.SemanticObject.new("canvas", "creative-document-editor", "Canvas")
        self.semantic_document = LumaSemantics.SemanticObject.new("active-document", "design-document", self.document.name)
        self.semantic_document.set_privacy(LumaSemantics.Privacy.PRIVATE)
        for command_id, label, risk in (
            ("document.save-version", "Save a version", LumaSemantics.ActionRisk.LOW),
            ("document.export", "Export document", LumaSemantics.ActionRisk.CONSEQUENTIAL),
            ("edit.delete", "Delete selection", LumaSemantics.ActionRisk.DESTRUCTIVE),
        ):
            action = LumaSemantics.Action.new(command_id, label)
            action.set_risk(risk)
            self.semantic_document.add_action(action)
        self.semantic_root.add_child(self.semantic_document)

    def _build_actions(self) -> None:
        """Window actions the toolbar menus point at."""
        simple = {
            "zoom-in": lambda: self._zoom_step(1), "zoom-out": lambda: self._zoom_step(-1),
            "zoom-fit": self._zoom_to_fit, "zoom-selection": self._zoom_to_selection,
            "zoom-50": lambda: self._zoom_to(0.5), "zoom-100": lambda: self._zoom_to(1.0),
            "zoom-200": lambda: self._zoom_to(2.0),
        }
        for name, callback in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, fn=callback: fn())
            self.add_action(action)
        for name, callback in (("creative-duplicate", self._duplicate),
                               ("creative-front", lambda: self._restack("front")),
                               ("creative-back", lambda: self._restack("back")),
                               ("creative-delete", self._delete_selection)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, fn=callback: fn())
            self.add_action(action)
        self._toggle_actions: dict[str, Gio.SimpleAction] = {}
        for name, setter in (("show-grid", self._set_grid),
                             ("pixel-snap", lambda value: setattr(self.canvas, "pixel_snap", value)),
                             ("outline", self._set_outline),
                             ("pixel-grid", self._set_pixel_grid),
                             ("show-rulers", lambda value: self.canvas.toggle_rulers(value))):
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(False))
            action.connect("change-state", lambda act, value, fn=setter: (fn(value.get_boolean()), act.set_state(value)))
            self.add_action(action)
            self._toggle_actions[name] = action
        page = Gio.SimpleAction.new_stateful("page", GLib.VariantType.new("s"), GLib.Variant.new_string("page-1"))
        page.connect("change-state", self._page_action)
        self.add_action(page)
        self._page_action_ref = page
        for name, callback in (("page-new", self._new_page), ("page-rename", self._rename_page),
                               ("page-delete", self._delete_page)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, fn=callback: fn())
            self.add_action(action)
        for name, callback in (("creative-stroke", self._toggle_creative_stroke),
                               ("creative-shadow", self._toggle_creative_shadow)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, fn=callback: fn())
            self.add_action(action)

    # ── layout ──────────────────────────────────────────────────────────

    def _segments(self, values, active: str, callback: Callable[[str], None]):
        group = ConnectedButtonGroup(compact=True, context=self.context)
        group.add_css_class("canvas-tabs")
        group.set_hexpand(True)
        group.set_homogeneous(True)
        buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for value, label in values:
            button = Gtk.ToggleButton(label=label, hexpand=True)
            button.set_name(f"cv-tab-{value}")
            if first is None:
                first = button
            else:
                button.set_group(first)
            group.append(button)
            buttons[value] = button
        buttons[active].set_active(True)
        for value, button in buttons.items():
            button.connect("toggled", lambda widget, choice=value: callback(choice) if widget.get_active() else None)
        return group, buttons

    def _build_ui(self) -> None:
        # TODO(kit-request canvas-01-creative-workspace): replace the old
        # islands with CreativeWorkspace(left=FloatingPanel(...), right=...,
        # tools=ToolBar(...), zoom=ZoomControl(...)).
        self.toast_overlay = ToastHost()
        self.toast_overlay.set_name("cv-toast-host")
        self.workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.workspace.set_name("cv-workspace")
        self.workspace.add_css_class("canvas-workspace")
        self.pages = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.pages.add_named(self.workspace, "editor")
        self.toast_overlay.set_child(self.pages)
        self.set_body(self.toast_overlay)

        # One row: left column, gutter, canvas island, gutter, inspector island.
        self.islands = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, vexpand=True)
        self.islands.set_name("cv-islands")
        self.workspace.append(self.islands)

        self.left_column = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=RAIL_TO_PANEL)
        self.left_column.set_name("cv-left-column")
        self.left_column.add_css_class("canvas-left-column")
        self.left_column.set_hexpand(False)
        self.tool_pane = self._build_rail()
        self.left_column.append(self.tool_pane)
        self.layers = LayersPanel(
            on_select=self._layers_selected, on_rename=self.rename_layer, on_toggle=self._layers_toggled,
            on_reorder=self._layers_reordered, on_hover=self._layer_hovered, on_context=self._open_context_menu,
            on_asset_color=self._asset_color, on_asset_text=self._asset_text,
            segments=self._segments, context=self.context)
        self.navigation_pane = self.layers
        self.nav_segment_buttons = self.layers.tab_buttons
        # The rail sits on the window ground; Layers and Assets are an island.
        self.layers_slot = Island()
        self.layers_slot.set_name("cv-left-panel")
        self.layers_slot.add_css_class("canvas-layers-island")
        self.layers_slot.append(self.layers)
        self.left_column.append(self.layers_slot)

        self.center_pane = Island()
        self.center_pane.add_css_class("workspace")
        self.center_pane.set_name("cv-work")
        self.center_pane.add_css_class("canvas-center-island")
        self.center_pane.set_hexpand(True)
        self.center_pane.append(self._build_center())

        self.inspector_pane = Island()
        self.inspector_pane.set_name("cv-right-panel")
        self.inspector_pane.add_css_class("canvas-inspector-island")
        self.inspector_pane.set_hexpand(False)
        self.inspector = Inspector(self, self._segments)
        self.inspector.set_name("cv-inspector")
        self.layers.set_name("cv-layers")
        self.inspector_segment_buttons = self.inspector.tab_buttons
        self.inspector_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.inspector_slot.append(self.inspector)
        self.inspector_pane.append(self.inspector_slot)

        self.navigation_gutter = ResizeGutter(
            get_width=lambda: self.layers_slot.get_size_request()[0],
            set_width=lambda width: self.layers_slot.set_size_request(width, -1),
            minimum=NAVIGATION_WIDTHS[0], maximum=NAVIGATION_WIDTHS[1], default=NAVIGATION_WIDTHS[2],
            grows_with_pointer=True, on_finished=lambda width: self.remember("navigation-width", int(width)),
            label="Resize layers")
        self.inspector_gutter = ResizeGutter(
            get_width=lambda: self.inspector_pane.get_size_request()[0],
            set_width=lambda width: self.inspector_pane.set_size_request(width, -1),
            minimum=INSPECTOR_WIDTHS[0], maximum=INSPECTOR_WIDTHS[1], default=INSPECTOR_WIDTHS[2],
            grows_with_pointer=False, on_finished=lambda width: self.remember("inspector-width", int(width)),
            label="Resize inspector")
        for widget in (self.left_column, self.navigation_gutter, self.center_pane,
                       self.inspector_gutter, self.inspector_pane):
            self.islands.append(widget)
        self._restore_panel_sizes()

        # Medium widths: Layers in a popover from the toolbar.
        self.layers_popover = Gtk.Popover(position=Gtk.PositionType.BOTTOM, has_arrow=False)
        self.layers_popover.add_css_class("canvas-layers-flyout")
        self.layers_popover_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.layers_popover_slot.set_size_request(NAVIGATION_WIDTHS[2] + 40, 480)
        self.layers_popover.set_child(self.layers_popover_slot)
        self.layers_toggle.set_popover(self.layers_popover)

        medium = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1099px"))
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 719px"))
        for breakpoint in (medium, compact):
            breakpoint.connect("apply", lambda *_: GLib.idle_add(self._sync_layout_mode))
            breakpoint.connect("unapply", lambda *_: GLib.idle_add(self._sync_layout_mode))
            self.add_breakpoint(breakpoint)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        modifiers = Gtk.EventControllerKey()
        modifiers.connect("modifiers", self._modifiers_changed)
        self.add_controller(modifiers)
        if hasattr(Gdk, "FileList"):
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("drop", self._files_dropped)
            self.add_controller(drop)
        if hasattr(LumaUI, "CreativeWorkspace"):
            self._mount_creative_workspace()

    def _mount_creative_workspace(self) -> None:
        """Use the shared suite shell while retaining Canvas's editor model."""
        from luma_appkit import (
            CreativeWorkspace, FloatingPanel, Flyout, LayerTree, Tool, ToolBar,
            TOOL_SEPARATOR,
        )

        self.stage_overlay.set_child(None)
        self.stage_overlay.remove_overlay(self.zoom_pill)
        self.stage_overlay.remove_overlay(self.compact_dock)
        self.zoom_pill.set_margin_end(16)
        self.zoom_pill.set_margin_bottom(20)
        self.layers_slot.remove(self.layers)
        self.inspector_slot.remove(self.inspector)
        self.inspector.scroll.set_child(None)
        self.canvas.remove_css_class("canvas-canvas-frame")

        asset_scroll = self.layers.stack.get_child_by_name("assets")
        asset_scroll.set_child(None)
        self.creative_tree = LayerTree(
            kit_layers(self.document), selected=self.document.selected_ids,
            on_select=self._layers_selected, on_visibility=self._creative_visibility,
            on_rename=self._creative_rename, on_reorder=self._creative_reorder,
            on_context=self._creative_layer_context,
        )
        self.creative_tree.set_name("cv-layers")
        from .design_controls import ResourcePanel
        self.resource_panel = ResourcePanel(self)
        self.assets_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.assets_content.append(self.resource_panel)
        self.assets_content.append(self.layers.assets)
        page = self.document.current_page() or self.document.ensure_pages()
        self.creative_left = FloatingPanel(
            page.name, "layers", title_menu=self._page_menu,
            pages=(("layers", "Layers", self.creative_tree),
                   ("assets", "Assets", self.assets_content)),
            summary=f"{len(self.document.objects)} layers",
            on_page=self._creative_left_page,
            on_folded=lambda folded: self._creative_panel_folded("navigation", folded),
        )
        self.creative_left.set_name("cv-left-panel")
        self.creative_left.set_holds("layers")

        self.creative_design = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.creative_export = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        from .creative_inspector import CreativeInspector

        self.creative_inspector = CreativeInspector(self)
        self.creative_design.append(self.creative_inspector)
        from .design_controls import PrototypePanel, action
        self.prototype_panel = PrototypePanel(self)
        self.prototype_play = action("play", "Preview selected frame", self.prototype_panel.preview)
        self.prototype_play.set_visible(False)
        self.creative_export.append(self.inspector.column)
        selection = self.document.selection
        subject = selection[0] if selection else None
        self.creative_right = FloatingPanel(
            subject.name if subject else "Properties",
            CREATIVE_ICONS.get(subject.kind, "layers") if subject else "layers",
            pages=(("design", "Design", self.creative_design),
                   ("prototype", "Prototype", self.prototype_panel),
                   ("export", "Export", self.creative_export)),
            more=self._creative_more_menu(),
            closable=False, editable=bool(subject), header_actions=self.prototype_play, on_page=self._creative_inspector_page,
            on_rename=self._creative_subject_renamed,
            on_folded=lambda folded: self._creative_panel_folded("inspector", folded),
        )
        self.creative_right.set_name("cv-right-panel")
        self.creative_right.set_holds("inspector")

        tools = [
            Tool("move", "mouse-pointer-2", "Move", "V"),
            Tool("frame", "hash", "Frame", "F"),
            Flyout("shape", "Shapes", (
                Tool("rectangle", "square", "Rectangle", "R"),
                Tool("ellipse", "circle", "Ellipse", "O"),
                Tool("line", "minus", "Line", "L"),
                Tool("arrow", "arrow-up-right", "Arrow", "A"),
            )),
            Tool("path", "pen-tool", "Pen", "P"),
            Tool("text", "type", "Text", "T"),
            Tool("image", "image", "Image", "I"),
            TOOL_SEPARATOR,
            Tool("hand", "hand", "Hand", "H"),
            Tool("comment", "message-circle", "Comment", "C"),
        ]
        self.creative_toolbar = ToolBar(tools, current="move", on_tool=self._creative_tool)
        self.creative_toolbar.set_name("cv-tools")
        self.toast_overlay.track_bar(self.creative_toolbar)
        self.creative_workspace = CreativeWorkspace(
            self.canvas, left=self.creative_left, right=self.creative_right,
            tools=self.creative_toolbar, zoom=self.zoom_pill,
            camera=(self.document.zoom, self.document.canvas_x, self.document.canvas_y),
        )
        self.creative_workspace.set_name("cv-work")
        self.creative_workspace.set_overflow(Gtk.Overflow.HIDDEN)
        self.action_bar.get_parent().remove(self.action_bar)
        self.pages.remove(self.workspace)
        self.workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.workspace.append(self.creative_workspace)
        self.pages.add_named(self.workspace, "editor")
        self._creative_active = True
        self._phone_panel = None
        self._phone_layers_tab = "layers"
        self._phone_content = None
        self._phone_expanded = False
        self._phone_color = None
        self._phone_scroller = None
        self._phone_side = None
        self._phone_landscape = False
        self._phone_short = False
        self.phone_actions = ActionCenter().attach(self.toast_overlay)
        self.creative_workspace.add_tick_callback(self._phone_geometry)
        from luma_appkit.action_center import make_control
        self.phone_project = TitleIsland(self.session.display_name, lead='back',
            on_lead=self._show_documents, variant='creative', phone_only=False)
        self.phone_project.float_over(self)
        self.phone_export = CornerPill(share=self._open_share_sheet,
            actions=[('share','Export',self.export_frames)],labelled=True,primary='Export')
        from luma_appkit.lumaui_tokens import PHONE_FRAME
        self.phone_export.set_margin_top(
            PHONE_FRAME["status"] - 12 if self.has_css_class("lumaui-phone-device") else 0)
        from luma_appkit.structure_layers import LayerHost
        LayerHost.window_host(self).add_overlay(self.phone_export)

        from luma_appkit.structure_adapt import WidthWatch
        self._phone_watch = WidthWatch(self.creative_workspace, self._phone_layout, threshold=599)

    def _phone_layout(self, width):
        landscape=width>=500 and width>self.creative_workspace.get_height() and self.creative_workspace.get_height()<420
        if landscape!=self._phone_landscape and self._phone_color is not None:self._phone_color.popdown()
        self._phone_landscape=landscape
        self._phone_short=self.creative_workspace.get_height()<500
        self._phone_ui = 0 < width < 600 or (0 < width < 900 and 0 < self.creative_workspace.get_height() < 420)
        for widget in (self.creative_inspector,self.prototype_panel,self.resource_panel):
            (widget.add_css_class if self._phone_ui else widget.remove_css_class)("lumaui-creative-properties-phone")
        self.phone_project.set_visible(getattr(self, "_phone_ui", False) and not self._welcome_active)
        self.phone_export.set_visible(False)
        self._refresh_phone_actions()
        self._place_panels()

    def _restore_phone_content(self):
        if self._phone_side is not None:
            from luma_appkit.structure_layers import LayerHost
            LayerHost.window_host(self).remove_overlay(self._phone_side)
            self._phone_side=None
        if self._phone_content is None:
            return
        child, parent, name, was_visible = self._phone_content
        current = child.get_parent()
        if isinstance(current, Gtk.Box): current.remove(child)
        elif isinstance(current, Gtk.Viewport): current.set_child(None)
        if isinstance(parent, Gtk.Stack): parent.add_named(child, name)
        else: parent.append(child)
        child.set_visible(was_visible)
        self._phone_content = None

    def _phone_deselect(self):
        self._phone_panel = None
        self.document.select([])
        self.canvas.emit("selection-changed")

    def _phone_layers_page(self, key):
        self._phone_layers_tab=key
        self._refresh_phone_actions()

    def _phone_grow(self, key):
        if self._phone_color is not None:
            self._phone_color.popdown()
        self._phone_panel = None if self._phone_panel == key else key
        self._phone_expanded = False
        self._refresh_phone_actions()

    def _show_documents(self):
        if not self._welcome_active and not self.flush():
            Toast.show(self.toast_overlay, 'Could not save the design. Please try again before returning to Documents.')
            return
        if self.pages.get_child_by_name('documents') is None:
            from .document_library import DocumentLibrary
            self.document_library = DocumentLibrary(self)
            self.pages.add_named(self.document_library, 'documents')
        self.document_library.refresh()
        self._welcome_active = True
        self.pages.set_visible_child_name('documents')
        self.phone_project.set_visible(False)
        self.phone_export.set_visible(False)
        self.phone_actions.hide_bar()

    def _phone_close_panel(self):
        if self._phone_color is not None:
            self._phone_color.popdown()
            return
        self._phone_panel = None
        self._phone_expanded = False
        self._refresh_phone_actions()

    def _phone_expand_panel(self):
        self._phone_expanded = not self._phone_expanded
        self._phone_expand.set_tooltip_text('Reduce panel' if self._phone_expanded else 'Expand panel')
        from luma_appkit import icons
        self._phone_expand.set_child(icons.image('chevron-down' if self._phone_expanded else 'chevron-up'))

    def _phone_geometry(self, _widget, _clock):
        width,height=self.creative_workspace.get_width(),self.creative_workspace.get_height()
        compact=0<width<600 or (0<width<900 and 0<height<420)
        landscape=width>=500 and width>height and height<420
        if compact!=getattr(self,'_phone_ui',False) or landscape!=self._phone_landscape or (height<500)!=self._phone_short:self._phone_layout(width)
        phone = getattr(self, '_phone_ui', False) and not self._welcome_active
        if not phone:
            if self.canvas.get_margin_bottom() or self.canvas.get_margin_top():
                self.canvas.set_margin_top(0);self.canvas.set_margin_bottom(0);self.canvas.set_margin_end(0)
            return True
        height = self.creative_workspace.get_height()
        if height < 100:return True
        if self._phone_scroller is not None:
            fraction=.48 if self._phone_expanded else .40
            if self._phone_panel=='color':fraction=.42
            elif self._phone_panel=='tools':fraction=.38
            fixed=max(0,self.phone_actions.bar.get_height()-self._phone_scroller.get_height())
            minimum=48
            if self._phone_panel=='color':minimum=80 if self._phone_short else 160
            elif self._phone_panel in {'design','prototype'}:minimum=88 if self._phone_short else 100
            cap=max(minimum,int(height*fraction)-fixed)
            if self._phone_side is not None:cap=max(88 if self._phone_panel=='color' else 100,height-250)
            if self._phone_scroller.get_max_content_height()!=cap:
                self._phone_scroller.set_max_content_height(cap)
        # The shared bar owns its glass; drawing has a separate viewport above
        # it. No frame label, gradient handle or selection can hide behind it.
        found,bar_bounds=self.phone_actions.bar.compute_bounds(self.creative_workspace)
        bottom=max(0,int(height-bar_bounds.get_y())+4) if found and bar_bounds.get_y()>0 else self.phone_actions.bar.get_height()+48
        bottom=min(bottom,max(0,height-136))
        if self.canvas.get_margin_bottom()!=bottom:
            self.canvas.set_margin_bottom(bottom)
        self.canvas.set_margin_top(56)
        end=0
        if self._phone_side is not None:
            found,side_bounds=self._phone_side.compute_bounds(self.creative_workspace)
            end=max(0,int(width-side_bounds.get_x())+4) if found and side_bounds.get_x()>0 else 300
        self.canvas.set_margin_end(end)
        signature=(self.canvas.get_width(),self.canvas.get_height(),self._phone_panel,
                   self._phone_expanded,tuple(self.document.selected_ids))
        if signature!=getattr(self,'_phone_geometry_signature',None):
            self._phone_geometry_signature=signature
            if getattr(self,'_phone_reveal_timer',0):GLib.source_remove(self._phone_reveal_timer)
            self._phone_reveal_timer=GLib.timeout_add(160,self._phone_reveal_selection)
        return True

    def _phone_reveal_selection(self):
        if not self.canvas.get_mapped() or not getattr(self,'_phone_ui',False) or self._welcome_active:
            self._phone_reveal_timer=0
            return False
        if getattr(self.canvas,'_animation',None) is not None:return True
        self._phone_reveal_timer=0
        if getattr(self.canvas,'_press',None) is not None:return False
        if self.document.selection and self.canvas.get_height()>80:
            from . import geometry
            from .viewport import fit_rect
            bounds=geometry.union_bounds(self.canvas._selection_items())
            if bounds:
                left_edge,top_edge,right_edge,bottom_edge=bounds;view=self.canvas.view
                left,top=view.to_view(left_edge,top_edge);right,bottom=view.to_view(right_edge,bottom_edge)
                if left<16 or top<16 or right>self.canvas.get_width()-16 or bottom>self.canvas.get_height()-16:
                    self.canvas._set_view(fit_rect(*bounds,self.canvas.get_width(),self.canvas.get_height(),margin=20,max_zoom=view.zoom))
        return False

    def _phone_more_tools(self):
        from luma_appkit.action_bubble import MenuItem
        menu=[MenuItem(name,icon=glyph,on_activate=lambda key=key:self._creative_tool(key))
              for key,glyph,name in [('frame','hash','Frame'),('text','type','Text'),('path','pen-tool','Pen'),
                                    ('image','image','Image'),('hand','hand','Hand'),('comment','message-circle','Comment')]]
        if self.document.selection:
            menu += [MenuItem('Duplicate',icon='copy',on_activate=self._duplicate),
                     MenuItem('Delete',icon='trash-2',on_activate=self._delete_selection),
                     MenuItem('Bring to front',icon='bring-to-front',on_activate=lambda:self._restack('front')),
                     MenuItem('Fit selection',icon='zoom-in',on_activate=self._zoom_to_selection),
                     MenuItem('Clear selection',icon='x',on_activate=self._phone_deselect)]
        menu += [MenuItem('Zoom in',icon='zoom-in',on_activate=lambda:self._zoom_step(1)),
                 MenuItem('Zoom out',icon='zoom-out',on_activate=lambda:self._zoom_step(-1)),
                 MenuItem('Fit artboards',icon='zoom-in',on_activate=self._zoom_to_fit),
                 MenuItem('Share',icon='share',on_activate=self._open_share_sheet),
                 MenuItem('Export',icon='upload',on_activate=self.export_frames)]
        return menu

    def _refresh_phone_actions(self):
        if not hasattr(self, 'phone_actions'):return
        if hasattr(self,'_phone_subject') and self.document.selection and self._phone_panel in {'design','prototype'}:
            self._phone_subject.set_label(self.document.selection[0].name)
            if self._phone_side is not None:self._phone_side.set_title(self.document.selection[0].name)
        if self._phone_color is not None:
            if getattr(self,'_phone_ui',False) and not self._welcome_active:
                return  # Keep the live picker stationary while selection changes.
            self._phone_color.popdown()
        signature=(getattr(self,'_phone_ui',False),self._welcome_active,id(self.document),
                   tuple(self.document.selected_ids),self._phone_panel,self._phone_layers_tab,self.canvas.tool,self._phone_landscape,self._phone_short)
        if signature==getattr(self,'_phone_action_signature',None):return
        self._phone_action_signature=signature
        self._restore_phone_content();self.phone_actions.hide_bar();self._phone_scroller=None
        self.phone_project.set_visible(getattr(self,'_phone_ui',False) and not self._welcome_active)
        self.phone_export.set_visible(False)
        if self._welcome_active:return
        if not getattr(self,'_phone_ui',False):
            self.creative_toolbar.set_visible(True);self.action_bar.set_visible(not self._creative_active)
            return
        self.creative_toolbar.set_visible(False);self.action_bar.set_visible(False)
        from luma_appkit.action_center import make_control
        from luma_appkit import ModeSwitch
        self._phone_tool_buttons={}
        items=[]
        for key,glyph,name in ([('move','mouse-pointer-2','Move'),('rectangle','square','Shapes')] + ([] if self.document.selection else [('text','type','Text')])):
            button=make_control(BarAction(glyph,tooltip=name,
                active=self.canvas.tool==('select' if key=='move' else key),
                on_activate=lambda key=key:self._creative_tool(getattr(self,'_last_shape_tool','rectangle') if key=='rectangle' else key)))
            if key=='rectangle':
                hold=Gtk.GestureLongPress();hold.connect('pressed',lambda *_args,b=button:self._open_shape_flyout(b));button.add_controller(hold)
                button.set_tooltip_text('Shapes · hold to choose')
            self._phone_tool_buttons[key]=button;items.append(BarWidget(button))
        items += [BarAction('layers',tooltip='Layers and assets',on_activate=lambda:self._phone_grow('layers'))]
        if self.document.selection:
            items.append(BarAction('sliders-horizontal',tooltip='Properties',on_activate=lambda:self._phone_grow('design')))
        items.append(BarAction('ellipsis',tooltip='Tools and actions',on_activate=lambda:self._phone_grow('tools')))
        self.phone_actions.show_bar(items,fill=True)
        key=self._phone_panel
        child=(self.creative_inspector if key=='design' else self.prototype_panel) if key in {'design','prototype'} and self.document.selection else (self.assets_content if self._phone_layers_tab=='assets' else self.creative_tree) if key=='layers' else None
        if key=='tools':
            from luma_appkit.bar_panel import panel_list
            import copy
            def choose(callback):
                self._phone_close_panel()
                if callback:callback()
            rows=[]
            for item in self._phone_more_tools():
                row=copy.copy(item);row.on_activate=lambda callback=item.on_activate:choose(callback);rows.append(row)
            child=panel_list(rows,label='Tools and actions')
        if child is None:return
        if key!='tools':
            parent=child.get_parent();name=parent.get_page(child).get_name() if isinstance(parent,Gtk.Stack) else None
        if key!='tools':
            was_visible=child.get_visible();parent.remove(child);child.set_visible(True)
            self._phone_content=(child,parent,name,was_visible)
        panel=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
        heading=Gtk.Box(spacing=4)
        selected=self.document.selection
        title=Gtk.Label(label='Tools and actions' if key=='tools' else selected[0].name if selected and key!='layers' else 'Layers',
                        xalign=0,hexpand=True,ellipsize=Pango.EllipsizeMode.END)
        self._phone_subject=title
        title.add_css_class('heading');heading.append(title)
        from .design_controls import action
        if self._phone_short and key in {'design','prototype'}:
            from luma_appkit.action_bubble import MenuItem
            heading.append(make_control(BarAction('sliders-horizontal',tooltip='Panel mode',menu=lambda:[MenuItem(label,on_activate=lambda mode=mode:self._phone_grow(mode)) for mode,label in [('design','Design'),('prototype','Prototype')]])))
        if self._phone_short and key=='layers':
            from luma_appkit.action_bubble import MenuItem
            heading.append(make_control(BarAction('layers',tooltip='Layers or assets',menu=lambda:[MenuItem(label,on_activate=lambda mode=mode:self._phone_layers_page(mode)) for mode,label in [('layers','Layers'),('assets','Assets')]])))
        if key=='prototype':heading.append(action('play','Preview selected frame',self.prototype_panel.preview))
        self._phone_expand=action('chevron-up','Expand panel',self._phone_expand_panel)
        if not self._phone_short:heading.append(self._phone_expand)
        heading.append(action('x','Close panel',self._phone_close_panel))
        control=heading.get_first_child()
        while control:
            if isinstance(control,Gtk.Button):control.set_size_request(44,44)
            control=control.get_next_sibling()
        panel.append(heading)
        if key in {'design','prototype'} and not self._phone_short:
            panel.append(ModeSwitch([('design','Design'),('prototype','Prototype')],current=key,on_change=self._phone_grow,fill=True))
        elif key=='layers' and not self._phone_short:
            panel.append(ModeSwitch([('layers','Layers'),('assets','Assets')],current=self._phone_layers_tab,on_change=self._phone_layers_page,fill=True))
        self._phone_scroller=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,max_content_height=100,child=child)
        panel.append(self._phone_scroller)
        if self._phone_landscape:
            from luma_appkit.creative_workspace import FloatingPanel
            from luma_appkit.structure_layers import LayerHost
            from luma_appkit.action_bubble import MenuItem
            panel.remove(heading)
            switch=panel.get_first_child()
            if isinstance(switch,ModeSwitch):panel.remove(switch)
            controls=Gtk.Box(spacing=4)
            choices=([MenuItem(label,on_activate=lambda mode=mode:self._phone_grow(mode)) for mode,label in [('design','Design'),('prototype','Prototype')]] if key in {'design','prototype'} else [MenuItem(label,on_activate=lambda mode=mode:self._phone_layers_page(mode)) for mode,label in [('layers','Layers'),('assets','Assets')]])
            if key!='tools':controls.append(make_control(BarAction('sliders-horizontal',tooltip='Panel mode',menu=choices)))
            if key=='prototype':controls.append(action('play','Preview selected frame',self.prototype_panel.preview))
            controls.append(action('x','Close panel',self._phone_close_panel))
            control=controls.get_first_child()
            while control:
                control.set_size_request(44,44);control=control.get_next_sibling()
            self._phone_side=FloatingPanel(title.get_label(),'layers',child=panel,closable=False,header_actions=controls)
            self._phone_side.set_halign(Gtk.Align.END);self._phone_side.set_valign(Gtk.Align.START)
            self._phone_side.set_margin_top(12);self._phone_side.set_margin_end(12)
            LayerHost.window_host(self).add_overlay(self._phone_side)
        else:self.phone_actions.grow(key,panel)

    @staticmethod
    def _creative_more_menu() -> Gio.Menu:
        menu = Gio.Menu()
        actions = Gio.Menu()
        for label, action, shortcut in (
            ("Duplicate", "win.creative-duplicate", "<Control>d"),
            ("Bring to front", "win.creative-front", "bracketright"),
            ("Send to back", "win.creative-back", "bracketleft"),
            ("Zoom to selection", "win.zoom-selection", "<Shift>2"),
        ):
            item = Gio.MenuItem.new(label, action)
            item.set_attribute_value("accel", GLib.Variant.new_string(shortcut))
            actions.append_item(item)
        menu.append_section(None, actions)
        destructive = Gio.Menu()
        item = Gio.MenuItem.new("Delete", "win.creative-delete")
        item.set_attribute_value("accel", GLib.Variant.new_string("Delete"))
        destructive.append_item(item)
        menu.append_section(None, destructive)
        return menu

    def _creative_left_page(self, page: str) -> None:
        if page == "assets":
            self.layers._sync_assets()

    def _creative_panel_folded(self, panel: str, folded: bool) -> None:
        # The kit folds panels automatically at phone width. Keep that
        # responsive state separate from the user's desktop fold choices.
        if (not hasattr(self, "creative_workspace") or self.creative_workspace.get_width() <= 0
                or self.creative_workspace.get_is_phone()):
            return
        if self._panels_hidden[panel] != folded:
            self._panels_hidden[panel] = folded
            if self._creative_active:
                self._place_panels()

    def _creative_inspector_page(self, page: str) -> None:
        self.prototype_play.set_visible(page == "prototype")
        if page == "export":
            self.inspector.tab_buttons["export"].set_active(True)
        else:
            self.creative_inspector.refresh()

    def _creative_subject_renamed(self, name: str) -> None:
        ids = self.document.selected_ids
        if len(ids) == 1:
            self.rename_layer(ids[0], name)

    def _creative_visibility(self, layer) -> None:
        self._layers_toggled("visible", [layer.get_id()], layer.get_visible())

    def _creative_rename(self, layer) -> None:
        self.rename_layer(layer.get_id(), layer.get_name())

    def _creative_reorder(self, layer, parent, index: int) -> None:
        try:
            parent_id, before_id = layer_move_target(
                self.document, layer.get_id(), parent.get_id() if parent else None, index)
        except ValueError as error:
            self._toast(str(error), warning=True)
            return
        self.edit(f"Reorder {layer.get_name()}",
                  lambda: self.editor.reorder(layer.get_id(), before_id, parent_id))

    def _creative_layer_context(self, layer, row: Gtk.Widget, x: float, y: float) -> None:
        # Selecting the layer rebuilds its row. Anchor to the stable tree so
        # the menu still has a native parent after that rebuild.
        found, bounds = row.compute_bounds(self.creative_tree)
        self._open_context_menu(self.creative_tree,
                                bounds.get_x() + x if found else x,
                                bounds.get_y() + y if found else y, layer.get_id())

    def _creative_tool(self, tool: str) -> None:
        if tool in {"rectangle","ellipse","line","arrow"}:self._last_shape_tool=tool
        if tool == "image":
            self._choose_image_at(self.canvas.get_width() / 2, self.canvas.get_height() / 2)
            return
        self._set_tool("select" if tool == "move" else tool)
        for key,button in getattr(self,'_phone_tool_buttons',{}).items():
            on=key==tool
            (button.add_css_class if on else button.remove_css_class)('on')
            button.update_state([Gtk.AccessibleState.PRESSED],[Gtk.AccessibleTristate.TRUE if on else Gtk.AccessibleTristate.FALSE])

    def _toggle_creative_stroke(self) -> None:
        selection = self.document.selection
        if len(selection) != 1:
            return
        item = selection[0]
        def change() -> None:
            adding = not item.stroke
            self.editor.set_property([item.id], "stroke", "#16161a" if adding else None)
            self.editor.set_property([item.id], "stroke_width", max(item.stroke_width, 1) if adding else 0)
        self.edit("Remove stroke" if item.stroke else "Add stroke", change)

    def _toggle_creative_shadow(self) -> None:
        selection = self.document.selection
        if len(selection) != 1:
            return
        item = selection[0]
        from luma_appkit.action_bubble import MenuItem
        from luma_appkit.menus import bar_menu
        def add(kind):
            effect = {"type": kind, "x": 0, "y": 4, "blur": 12,
                      "spread": 0, "color": "#00000040", "visible": True}
            self.edit("Add effect", lambda: self.editor.set_property(
                [item.id], "effects", [*item.effects, effect]))
        anchor = self.creative_inspector.effects_add_button
        self.effect_menu = bar_menu(anchor, [MenuItem(title, on_activate=lambda kind=kind: add(kind)) for kind, title in (
            ("drop_shadow", "Drop shadow"), ("inner_shadow", "Inner shadow"),
            ("layer_blur", "Layer blur"), ("background_blur", "Background blur"))], label="Add effect", where="anchor", width_px=240)

    def _build_title_actions(self) -> None:
        """Document actions live with the document, below window chrome."""
        people = self._fixture_people() if self._fixture_mode else ()
        self._share_people = people
        self.document_header = DocumentHeader(
            self.session.display_name, icon=APP_ID, subtitle=self._header_status,
            people=people, share=self._open_share_sheet,
            primary="Export", on_primary=self.export_frames, more=self.commands,
        )
        self.document_header.set_name("cv-document-header")
        self.action_bar.append(self.document_header)
        self.action_bar.set_visible(not self._welcome_active and not self._creative_active)
        if self._creative_active:
            from luma_appkit.action_center import make_control
            self.creative_toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
            self.bottom_share = make_control(BarAction("share-2", tooltip="Share",
                on_activate=lambda: self._open_share_sheet(self.bottom_share)))
            self.bottom_export = make_control(BarAction("share", "Export", on_activate=self.export_frames))
            self.bottom_more = make_control(BarAction("ellipsis", tooltip="Document actions",
                on_activate=lambda: self._open_bottom_more()))
            for control in (self.bottom_share, self.bottom_export, self.bottom_more):
                self.creative_toolbar.append(control)

    def _open_share_sheet(self, anchor: Gtk.Widget) -> None:
        people = self._share_people
        fixture = self._fixture_mode
        ShareSheet.present(
            anchor,
            document=ShareSubject(self.session.display_name,
                                  "Canvas · you and 2 others" if fixture else "Canvas document",
                                  kind="doc", icon=APP_ID),
            people=people,
            choices=("work-together", "send-copy") if fixture else ("send-copy",),
            collaborators=(Collaborator(people[0], "edit"), Collaborator(people[1], "comment"))
            if fixture else (),
            owner=Person("You", username="nick", hue=250) if fixture else None,
            on_choice=self._share_choice,
        )

    def _fixture_people(self) -> tuple[Person, Person]:
        def photo(key: str):
            data = self.document.embedded_assets.get(key)
            if not data:
                return None
            try:
                return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                return None

        return (Person("Priya Raman", username="priya", hue=330, picture=photo("ari-street")),
                Person("Nora Feld", username="nora", hue=45, picture=photo("terrace")))

    def _share_choice(self, choice: str, _value: object) -> str | None:
        if choice == "save":
            self._save_copy()
            return None
        # TODO(kit-request canvas-04-share): let a document without a sharing
        # backend show only actions it can actually fulfill.
        return "Save a copy to share this document"

    def _restore_panel_sizes(self) -> None:
        navigation = self.recall("navigation-width", NAVIGATION_WIDTHS[2])
        inspector = self.recall("inspector-width", INSPECTOR_WIDTHS[2])
        self.layers_slot.set_size_request(self.navigation_gutter.clamp(
            navigation if isinstance(navigation, (int, float)) else NAVIGATION_WIDTHS[2]), -1)
        self.inspector_pane.set_size_request(self.inspector_gutter.clamp(
            inspector if isinstance(inspector, (int, float)) else INSPECTOR_WIDTHS[2]), -1)

    def _build_rail(self) -> Gtk.Widget:
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        rail.add_css_class("canvas-rail")
        rail.set_size_request(RAIL_WIDTH, -1)
        first = None
        for tool, label, key, icon in RAIL_TOOLS:
            button = Gtk.ToggleButton()
            button.set_name(f"cv-tool-{tool}")
            button.add_css_class("canvas-tool")
            button.set_halign(Gtk.Align.CENTER)
            overlay = Gtk.Overlay()
            image = Gtk.Image(icon_name=icon, pixel_size=18)
            overlay.set_child(image)
            button.set_child(overlay)
            button.canvas_image = image
            button.set_tooltip_text(f"{label} · {key}")
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._tool_toggled, tool)
            self._add_tool_lock(button, overlay, tool)
            if tool in {"shape", "hand"}:
                rail.append(Gtk.Box(height_request=8))
            rail.append(button)
            self._tool_buttons[tool] = button
            if tool == "shape":
                mark = Gtk.Image(icon_name="lumaui-chevron-down-symbolic", pixel_size=8,
                                 halign=Gtk.Align.END, valign=Gtk.Align.END)
                mark.add_css_class("canvas-flyout-mark")
                overlay.add_overlay(mark)
                secondary = Gtk.GestureClick(button=3)
                secondary.connect("pressed", lambda *_args, widget=button: self._open_shape_flyout(widget))
                button.add_controller(secondary)
                hold = Gtk.GestureLongPress()
                hold.connect("pressed", lambda *_args, widget=button: self._open_shape_flyout(widget))
                button.add_controller(hold)
        rail.append(Gtk.Box(vexpand=True))
        # The interaction stream's current style chip goes here.
        self.rail_footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER)
        self.rail_footer.add_css_class("canvas-rail-footer")
        rail.append(self.rail_footer)
        try:
            from .color_picker import StyleChip
        except ImportError:
            StyleChip = None
        if StyleChip is not None:
            # Fill and stroke for new shapes (the interaction stream's chip).
            self.swatch = StyleChip(lambda: getattr(self, "canvas", None))
            self.rail_footer.append(self.swatch)
        self._tool_buttons["select"].set_active(True)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    vscrollbar_policy=Gtk.PolicyType.EXTERNAL, vexpand=True)
        scroll.add_css_class("canvas-rail-scroll")
        scroll.set_size_request(RAIL_WIDTH, -1)
        scroll.set_child(rail)
        return scroll

    def _canvas_shape_tools(self) -> list[str]:
        from . import canvas as canvas_module
        supported = (getattr(self.canvas, "SHAPE_TOOLS", None) or getattr(self.canvas, "supported_shape_tools", None)
                     or getattr(canvas_module, "DRAW_TOOLS", None))
        if supported:
            return [tool for tool in SHAPE_TOOLS if tool in supported]
        return ["rectangle", "ellipse"]

    def _open_shape_flyout(self, button: Gtk.Widget) -> None:
        commands = []
        for tool in self._canvas_shape_tools():
            label, key, icon = SHAPE_TOOLS[tool]
            commands.append(Command(f"tool.{tool}", label, lambda value=tool: self._set_tool(value), icon,
                                    shortcut=(key,) if key else (), checked=lambda value=tool: self._shape_tool == value))
        command_popover(CommandRegistry((CommandGroup(None, tuple(commands)),))).present_for(button)

    def _build_center(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.action_bar.set_name("cv-action-bar")
        self.action_bar.add_css_class("canvas-toolbar")
        box.append(self.action_bar)
        self.context_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.context_bar.set_name("cv-context-bar")
        self.context_bar.add_css_class("canvas-toolbar")

        self.layers_toggle = Gtk.MenuButton()
        self.layers_toggle.set_name("cv-layers-toggle")
        self.layers_toggle.set_child(Gtk.Image(icon_name="luma-canvas-layers-symbolic", pixel_size=14))
        self.layers_toggle.add_css_class("canvas-toolbar-icon")
        self.layers_toggle.set_tooltip_text("Layers · Alt+1")
        self.layers_toggle.update_property([Gtk.AccessibleProperty.LABEL], ["Layers"])
        self.layers_toggle.set_visible(False)
        self.context_bar.append(self.layers_toggle)
        self.show_left_button = self._toolbar_icon("luma-canvas-sidebar-left-symbolic", "Show layers · Alt+1",
                                                   lambda: self._toggle_panel("navigation"))
        self.show_left_button.set_visible(False)
        self.context_bar.append(self.show_left_button)

        self.document_button = Gtk.MenuButton()
        self.document_button.set_name("cv-document-menu")
        self.document_button.add_css_class("canvas-dropbutton")
        self.document_button.add_css_class("canvas-document-button")
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.context_label = Gtk.Label(xalign=0)
        self.context_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.context_label.set_max_width_chars(28)
        content.append(self.context_label)
        content.append(Gtk.Image(icon_name="luma-canvas-chevron-down-symbolic", pixel_size=12))
        self.document_button.set_child(content)
        self.document_button.set_tooltip_text("Document")
        self.document_button.set_popover(self._document_menu())
        self.document_button.set_create_popup_func(self._document_menu_opening)
        self.context_bar.append(self.document_button)

        self.save_status = SaveStatus()
        self.save_status.add_css_class("canvas-save-status")
        self.save_status.saved("Saved")
        self.context_bar.append(self.save_status)

        self.page_button = Gtk.MenuButton()
        self.page_button.add_css_class("canvas-dropbutton")
        page_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.page_label = Gtk.Label(label="Page 1")
        page_content.append(self.page_label)
        page_content.append(Gtk.Image(icon_name="luma-canvas-chevron-down-symbolic", pixel_size=12))
        self.page_button.set_child(page_content)
        self.page_button.set_tooltip_text("Pages")
        self._page_menu = Gio.Menu()
        self._page_menu.append("Page 1", "win.page::page-1")
        self.page_button.set_menu_model(self._page_menu)
        self.context_bar.append(self.page_button)

        self.context_bar.append(Gtk.Box(hexpand=True))

        self.zoom_button = Gtk.MenuButton()
        self.zoom_button.set_name("cv-toolbar-zoom")
        self.zoom_button.add_css_class("canvas-dropbutton")
        self.zoom_button.add_css_class("canvas-zoom-button")
        zoom_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.toolbar_zoom_label = Gtk.Label(label="100%")
        self.toolbar_zoom_label.add_css_class("numeric")
        zoom_content.append(self.toolbar_zoom_label)
        zoom_content.append(Gtk.Image(icon_name="luma-canvas-chevron-down-symbolic", pixel_size=12))
        self.zoom_button.set_child(zoom_content)
        self.zoom_button.set_tooltip_text("Zoom")
        self.zoom_button.set_popover(self._zoom_menu())
        self.zoom_button.set_create_popup_func(self._zoom_menu_opening)
        self.context_bar.append(self.zoom_button)
        self.show_right_button = self._toolbar_icon("luma-canvas-sidebar-right-symbolic", "Show inspector · Alt+8",
                                                    lambda: self._toggle_panel("inspector"))
        self.show_right_button.set_visible(False)
        self.context_bar.append(self.show_right_button)
        box.append(self.context_bar)
        # The v70 document row and bottom-corner zoom replace this older
        # toolbar; its menus stay wired until their creative panel peers land.
        self.context_bar.set_visible(False)

        self._banner_action: Callable[[], None] | None = None
        self.banner = Adw.Banner(revealed=False)
        self.banner.add_css_class("canvas-banner")
        self.banner.connect("button-clicked", lambda *_: self._banner_action() if self._banner_action else None)
        box.append(self.banner)

        self.canvas = LayoutCanvas(self.editor)
        self.canvas.set_name("cv-canvas")
        self.canvas.add_css_class("canvas-canvas-frame")
        self.canvas.connect("selection-changed", lambda *_: self._selection_changed())
        self.canvas.connect("document-changed", lambda _canvas, label: self._changed(label))
        self.canvas.connect("context-requested", lambda _c, x, y, item: self._open_context_menu(
            self.canvas, x, y, item.id if item else None))
        self.canvas.connect("comment-requested", lambda _canvas, x, y: self._comment_at(x, y))
        self.canvas.connect("image-needed", lambda _canvas, x, y: self._choose_image_at(x, y))
        self._connect_canvas_interaction()
        self.canvas.show_rulers = bool(self.recall("show-rulers", False))

        # The canvas paints objects past its edge. Islands do not clip (their
        # shadows must show), so the stage does.
        self.stage_overlay = Gtk.Overlay()
        self.stage_overlay.set_name("cv-stage")
        self.stage_overlay.set_overflow(Gtk.Overflow.HIDDEN)
        self.stage_overlay.set_vexpand(True)
        self.stage_overlay.set_child(self.canvas)
        self.zoom_control = ZoomControl(self.document.zoom, self._zoom_to,
                                        on_fit=self._zoom_to_fit, kind="pill",
                                        minimum=ZOOM_LIMITS[0], maximum=min(ZOOM_LIMITS[1], 8.0))
        self.zoom_pill = self.zoom_control._build()
        self.zoom_pill.set_name("cv-zoom")
        self.zoom_pill.set_halign(Gtk.Align.END)
        self.zoom_pill.set_valign(Gtk.Align.END)
        self.zoom_pill.set_margin_end(16)
        self.zoom_pill.set_margin_bottom(16)
        self.stage_overlay.add_overlay(self.zoom_pill)
        self.compact_dock = self._build_dock()
        self.compact_dock.set_name("cv-tools")
        self.toast_overlay.track_bar(self.compact_dock)
        self.stage_overlay.add_overlay(self.compact_dock)

        self.sheet = Adw.BottomSheet(content=self.stage_overlay, show_drag_handle=True, modal=True)
        self.sheet.add_css_class("canvas-bottom-sheet")
        self.sheet_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.sheet_slot.set_size_request(-1, 360)
        self.sheet.set_sheet(self.sheet_slot)
        self.sheet.set_vexpand(True)
        box.append(self.sheet)
        return box

    def _open_bottom_more(self) -> None:
        # These primary actions already have homes in the library/action bar.
        registry = CommandRegistry(tuple(group for group in self.commands.groups
                                         if not group.quick_actions))
        menu = command_popover(registry, variant="desktop")
        def limit(widget):
            if isinstance(widget, Gtk.ScrolledWindow):
                widget.set_max_content_height(360)
                widget.set_propagate_natural_height(True)
            child = widget.get_first_child()
            while child is not None:
                limit(child)
                child = child.get_next_sibling()
        limit(menu)
        menu.present_for(self.bottom_more)

    def _toolbar_icon(self, icon: str, label: str, callback: Callable[[], None]) -> Gtk.Button:
        button = IconButton(icon, label, context=self.context, quiet=True)
        button.add_css_class("canvas-toolbar-icon")
        button.connect("clicked", lambda *_: callback())
        return button

    def _build_dock(self) -> Gtk.Widget:
        dock = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, halign=Gtk.Align.CENTER,
                       valign=Gtk.Align.END)
        dock.add_css_class("canvas-dock")
        for tool, label, key, icon in RAIL_TOOLS:
            button = IconButton(icon, f"{label} · {key}", context=self.context, quiet=True)
            button.set_name(f"cv-dock-{tool}")
            button.add_css_class("canvas-dock-button")
            if tool == "shape":
                button.connect("clicked", lambda anchor: self._open_shape_flyout(anchor))
                self.dock_shape_button = button
            else:
                button.connect("clicked", lambda _b, value=tool: self._set_tool(value))
            dock.append(button)
        self._dock_extra_buttons = []
        for icon, label, which in (("luma-canvas-layers-symbolic", "Layers", "layers"),
                                   ("luma-canvas-settings-symbolic", "Properties", "inspector")):
            button = IconButton(icon, label, context=self.context, quiet=True)
            button.add_css_class("canvas-dock-button")
            button.connect("clicked", lambda _b, value=which: self._open_sheet(value))
            dock.append(button)
            self._dock_extra_buttons.append(button)
        dock.set_visible(False)
        return dock

    def _apply_fixture_state(self, state: str) -> None:
        """Static capture states; fixture data and edits remain in memory."""
        if state == "assets":
            if self._creative_active:
                self.creative_left.set_page("assets")
                self._creative_left_page("assets")
            else:
                self.nav_segment_buttons["assets"].set_active(True)
        elif state == "export":
            if self._creative_active:
                self.creative_right.set_page("export")
                self._creative_inspector_page("export")
            else:
                self.inspector_segment_buttons["export"].set_active(True)
        elif state == "nothing_selected":
            self.document.select([])
            self._selection_changed()
        elif state == "left_collapsed":
            self._toggle_panel("navigation")
        elif state == "focus":
            self._toggle_all_panels()
        elif state == "zoom_menu":
            self._fixture_popup(lambda: self.zoom_pill.emit("clicked"))
        elif state == "shape_menu":
            if self._creative_active:
                self._fixture_popup(lambda: self.creative_toolbar.open_flyout("shape"))
            else:
                self._fixture_popup(lambda: self._open_shape_flyout(self.dock_shape_button))
        elif state == "share_menu":
            self._fixture_popup(lambda: self._open_share_sheet(getattr(self, "bottom_share", self.document_header.controls["share"])))
        elif state == "phone_design":
            self._fixture_popup(lambda:self._phone_grow('design'))
        elif state == "phone_layers":
            self.document.select([])
            self._selection_changed()
            self._fixture_popup(lambda:self._phone_grow('layers'))
        elif state == "inspector_drawer":
            self._fixture_popup(lambda: self._open_sheet("inspector"))
        elif state != "initial":
            raise ValueError(f"unknown Canvas fixture state: {state}")

    def _fixture_popup(self, open_popup: Callable[[], None]) -> None:
        """Open capture menus after their anchors reach their final allocation."""
        def when_ready() -> bool:
            if (not self.get_mapped() or not self._initial_fit_done
                    or time.monotonic() - self._fixture_sized_at < 0.3):
                return GLib.SOURCE_CONTINUE
            open_popup()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(25, when_ready)

    # ── adaptive ────────────────────────────────────────────────────────

    def _sync_layout_mode(self) -> bool:
        width = self.get_width()
        mode = "compact" if 0 < width < 720 else "medium" if 0 < width < 1100 else "wide"
        if self.get_current_breakpoint() is None:
            mode = "wide"
        if mode == self._layout_mode:
            if self._creative_active:
                self._place_panels()
            return False
        self._layout_mode = mode
        self._place_panels()
        return False

    @staticmethod
    def _children(widget: Gtk.Widget):
        child = widget.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            yield child
            child = following

    def _reparent(self, widget: Gtk.Widget, slot: Gtk.Box) -> None:
        parent = widget.get_parent()
        if parent is slot:
            return
        if parent is not None:
            parent.remove(widget)
        slot.append(widget)

    def _place_panels(self) -> None:
        mode = self._layout_mode
        hidden_left = self._panels_hidden["navigation"]
        hidden_right = self._panels_hidden["inspector"]
        has_selection = bool(self.document.selected_ids)
        if self._creative_active:
            focused = hidden_left and hidden_right
            self.creative_workspace.set_focused(focused)
            if self.creative_workspace.get_width() > 0 and not self.creative_workspace.get_is_phone():
                self.creative_left.set_folded(hidden_left)
                self.creative_right.set_folded(hidden_right)
            self.creative_left.set_visible(not getattr(self, "_phone_ui", False))
            self.creative_right.set_visible(has_selection and not getattr(self, "_phone_ui", False))
            self.zoom_pill.set_visible(not getattr(self, "_phone_ui", False))
            if hasattr(self, "focus_button"):
                if focused:
                    self.focus_button.add_css_class("on")
                else:
                    self.focus_button.remove_css_class("on")
                self.focus_button.update_state(
                    [Gtk.AccessibleState.PRESSED],
                    [int(Gtk.AccessibleTristate.TRUE if focused else Gtk.AccessibleTristate.FALSE)])
            return
        if mode != "compact":
            self.sheet.set_open(False)
        if mode == "wide":
            self._reparent(self.layers, self.layers_slot)
            self._reparent(self.inspector, self.inspector_slot)
        elif mode == "medium":
            self._reparent(self.layers, self.layers_popover_slot)
            self._reparent(self.inspector, self.inspector_slot)
        self.left_column.set_visible(mode != "compact")
        self.layers_slot.set_visible(mode == "wide" and not hidden_left)
        self.navigation_gutter.set_visible(mode != "compact")
        resizable = mode == "wide" and not hidden_left
        self.navigation_gutter.set_sensitive(resizable)
        self.navigation_gutter.set_cursor_from_name("col-resize" if resizable else None)
        self.inspector_pane.set_visible(mode != "compact" and not hidden_right and has_selection)
        self.inspector_gutter.set_visible(mode != "compact" and not hidden_right and has_selection)
        self.layers_toggle.set_visible(mode == "medium")
        self.show_left_button.set_visible(mode == "wide" and hidden_left)
        self.show_right_button.set_visible(mode != "compact" and hidden_right and has_selection)
        self.compact_dock.set_visible(True)
        for button in self._dock_extra_buttons:
            button.set_visible(mode == "compact")
        self.zoom_pill.set_visible(not (hidden_left and hidden_right))
        if hasattr(self, "focus_button"):
            focused = hidden_left and hidden_right
            if focused:
                self.focus_button.add_css_class("on")
            else:
                self.focus_button.remove_css_class("on")
            self.focus_button.update_state(
                [Gtk.AccessibleState.PRESSED],
                [int(Gtk.AccessibleTristate.TRUE if focused else Gtk.AccessibleTristate.FALSE)])

    def _open_sheet(self, which: str) -> None:
        if self._creative_active:
            panel = self.creative_left if which == "layers" else self.creative_right
            panel.set_folded(False)
            return
        widget = self.layers if which == "layers" else self.inspector
        for child in list(self._children(self.sheet_slot)):
            self.sheet_slot.remove(child)
        if widget.get_parent() is not None:
            widget.get_parent().remove(widget)
        self.sheet_slot.append(widget)
        self.sheet.set_open(True)

    def _toggle_panel(self, panel: str) -> None:
        if self._creative_active:
            self._panels_hidden[panel] = not self._panels_hidden[panel]
            self._place_panels()
            return
        if self._layout_mode == "medium" and panel == "navigation":
            self.layers_toggle.popup()
            return
        if self._layout_mode == "compact":
            self._open_sheet("layers" if panel == "navigation" else "inspector")
            return
        self._panels_hidden[panel] = not self._panels_hidden[panel]
        self._place_panels()

    def _toggle_all_panels(self) -> None:
        hide = not all(self._panels_hidden.values())
        self._panels_hidden = {"navigation": hide, "inspector": hide}
        self._place_panels()

    # ── welcome and first fit ───────────────────────────────────────────

    def _initial_map(self, *_args) -> None:
        if self._welcome_active or self._initial_fit_done:
            return
        self.canvas.add_tick_callback(self._initial_fit_frame)

    def _initial_fit_frame(self, canvas: Gtk.Widget, _clock) -> bool:
        if self._welcome_active or self._initial_fit_done:
            return GLib.SOURCE_REMOVE
        if canvas.get_width() <= 1 or canvas.get_height() <= 1:
            return GLib.SOURCE_CONTINUE
        self._initial_fit_done = True
        if self._fixture_mode and self._creative_active:
            self._fit_v70_initial()
        else:
            self._fit_active_artboard()
        if self._fixture_mode:
            self._fixture_size = self._current_fixture_size()
            self._fixture_sized_at = time.monotonic()
            GLib.timeout_add(50, self._fixture_watch_size)
        self._sync_layout_mode()
        # Keyboard focus starts on the canvas, not the identity menu, so
        # single-key tools and Space work at once.
        self.canvas.grab_focus()
        return GLib.SOURCE_REMOVE

    def _current_fixture_size(self) -> tuple[int, int, bool]:
        return (self.canvas.get_width(), self.canvas.get_height(),
                self.creative_workspace.get_is_phone() if self._creative_active else False)

    def _fixture_watch_size(self) -> bool:
        if not self.get_mapped():
            return GLib.SOURCE_REMOVE
        size = self._current_fixture_size()
        if size != self._fixture_size:
            self._fixture_size = size
            self._fixture_sized_at = time.monotonic()
            if self._creative_active:
                self._fit_v70_initial()
        return GLib.SOURCE_CONTINUE

    def _reveal_editor(self) -> None:
        self.set_geometry_scope("", default_width=1440, default_height=900)
        self._welcome_active = False
        self.action_bar.set_visible(not self._creative_active)
        self.pages.set_visible_child_name("editor")
        self._refresh_all()
        self._initial_fit_done = False
        self.canvas.add_tick_callback(self._initial_fit_frame)

    def replace_document(self, document: Document, path: Path | None, *, opened: bool = True,
                         snapshot: dict | None = None) -> None:
        """Put a document into this window (the welcome surface's Open)."""
        self.session.close()
        self.editor = Editor(document)
        self.path = path
        if self._fixture_mode:
            from .fixture import FixtureSession
            self.session = FixtureSession(self.editor.document)
        else:
            self.session = DocumentSession(lambda: self.editor.document, path=path,
                                           on_status=self._save_status_changed, snapshot=snapshot)
        if path is not None and opened:
            self.session.opened()
        self.canvas.replace_editor(self.editor)
        self._reveal_editor()
        if path is not None:
            GLib.idle_add(lambda: (self._warn_missing_fonts(), False)[1])

    def _warn_missing_fonts(self) -> None:
        """A document naming fonts this machine lacks says so once; the text is
        drawn with a similar font and the names are kept."""
        try:
            from .font_picker import missing_fonts
        except (ImportError, ValueError):
            return
        names = [str((item.metadata or {}).get("font_family", "Figtree")) for item in self.document.objects
                 if item.kind == "text"]
        missing = missing_fonts(names)
        if not missing:
            return
        listed = "“" + "”, “".join(missing[:3]) + "”" + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
        verb = "isn’t" if len(missing) == 1 else "aren’t"
        self._toast(f"{listed} {verb} installed. Canvas shows a similar font.", warning=True)

    # ── refresh ─────────────────────────────────────────────────────────

    def _refresh_all(self) -> None:
        name = self.session.display_name
        if self.session.path is not None and not self.session.untitled and self.document.name != name:
            self.document.name = name
        self.set_identity_subtitle(f"{name} · {self.page_label.get_label()}")
        if hasattr(self, "semantic_document"):
            self.semantic_document.set_name(name)
        self.context_label.set_label(name)
        self.document_header.set_title(name, self._header_status)
        self.canvas.replace_editor(self.editor)
        self._sync_pages()
        self.inspector.refresh()
        self._refresh_creative()
        self._refresh_phone_actions()
        self._place_panels()
        self._refresh_zoom_label()
        self._update_toggle_states()
        self.canvas.emit("style-changed")

    def _refresh_zoom_label(self) -> None:
        self.toolbar_zoom_label.set_label(f"{self.document.zoom * 100:.0f}%")
        self.zoom_control.set_value(self.document.zoom)
        if self._creative_active:
            self.creative_workspace.set_camera(
                self.document.zoom, self.document.canvas_x, self.document.canvas_y)

    def _refresh_creative(self) -> None:
        if not self._creative_active:
            return
        self.creative_inspector.refresh()
        self.prototype_panel.refresh()
        self.prototype_play.set_visible(self.creative_right.get_page() == "prototype")
        self.resource_panel.refresh()
        # Component synchronization/Undo can replace the Python list without
        # changing any layer rows. Only a new document or row data needs a model.
        key=(id(self.document),tuple((item.id,item.parent_id,item.name,item.kind,item.visible) for item in self.document.objects))
        rebuilt=key!=getattr(self,'_creative_layers_key',None)
        if rebuilt:
            store=Gio.ListStore.new(LumaUI.Layer)
            for layer in kit_layers(self.document):store.append(layer)
            self.creative_tree.set_layers(store)
            self._creative_layers_key=key
        selected=tuple(self.document.selected_ids)
        if rebuilt or selected!=getattr(self,'_creative_selected_ids',None):
            self.creative_tree.set_selected(selected)
            self._creative_selected_ids=selected
        page = self.document.current_page()
        if page is not None:
            self.creative_left.set_title(page.name)
        self.creative_left.set_summary(f"{len(self.document.objects)} layers")
        selection = self.document.selection
        subject = selection[0] if selection else None
        self.creative_right.set_title(subject.name if subject else "Properties")
        self.creative_right.set_icon(CREATIVE_ICONS.get(subject.kind, "layers") if subject else "layers")
        self.creative_right.set_title_editable(len(selection) == 1)
        self.prototype_play.set_sensitive(any(item.kind in {"frame", "page", "component", "instance"} for item in self.document.objects))
        if self.creative_left.get_page() == "assets":
            self.layers._sync_assets()

    def _refresh_inspectors(self) -> None:
        self.inspector.refresh()
        if self._creative_active:
            self.creative_inspector.refresh()

    def _update_toggle_states(self) -> None:
        for name, value in (("show-grid", self.canvas.show_grid), ("outline", self.canvas.outline),
                            ("pixel-snap", bool(getattr(self.canvas, "pixel_snap", False))),
                            ("show-rulers", bool(getattr(self.canvas, "show_rulers", False))),
                            ("pixel-grid", bool(getattr(self.canvas, "show_pixel_grid", False)))):
            action = self._toggle_actions[name]
            if action.get_state().get_boolean() != value:
                action.set_state(GLib.Variant.new_boolean(value))

    def _selection_changed(self) -> None:
        self._refresh_phone_actions()
        self.layers.select(self.document.selected_ids)
        self.inspector.refresh()
        self._refresh_creative()
        self._place_panels()
        self._update_command_states()

    def _changed(self, label: str) -> None:
        self.canvas.queue_draw()
        self._refresh_all()
        self.session.note_change()
        self._update_command_states()

    # ── identity menu ───────────────────────────────────────────────────

    def _install_app_menu(self, application) -> None:
        """Replace AppKit's flat identity menu with Canvas's nested one."""
        from . import app_menu
        recent = application.recent_documents() if hasattr(application, "recent_documents") else []
        application.set_menubar(app_menu.build(recent))
        # The toolkit's identity button reads the menu model when it is built,
        # so give the nested model to that button as well.
        self.connect("realize", lambda *_: self._apply_identity_menu())
        application.connect("notify::menubar", lambda *_: self._apply_identity_menu())

    def _apply_identity_menu(self) -> None:
        application = self.get_application()
        model = application.get_menubar() if application is not None else None
        if model is None:
            return

        def walk(widget):
            child = widget.get_first_child()
            while child is not None:
                yield child
                yield from walk(child)
                child = child.get_next_sibling()
        for widget in walk(self.title_bar):
            if isinstance(widget, Gtk.MenuButton) and "luma-identity-button" in widget.get_css_classes():
                if widget.get_menu_model() is not model:
                    widget.set_menu_model(model)
                    from luma_appkit.menus import show_model_icons
                    show_model_icons(widget.get_popover())

    def _update_command_states(self) -> None:
        """Application actions follow the active window's selection, so rows
        that do not apply are drawn disabled."""
        application = self.get_application()
        if application is None or (application.get_active_window() not in (None, self)):
            return
        for group in self.commands.groups:
            for command in group.commands:
                action = application.lookup_action(command.id.replace(".", "-"))
                if action is not None:
                    try:
                        enabled = bool(command.enabled())
                    except Exception:
                        enabled = False
                    if action.get_enabled() != enabled:
                        action.set_enabled(enabled)

    # ── editing through the host API the inspector and layers use ───────

    def edit(self, label: str, change: Callable[[], None], *, coalesce: bool = False) -> bool:
        """Run one named, undoable change. `coalesce` merges a run of the
        same change (a scrub, a picker drag, a nudge burst) into one entry."""
        key = f"property:{label}" if coalesce else None
        keyed = key is not None and "key" in inspect.signature(self.editor.transaction).parameters
        try:
            with (self.editor.transaction(label, key=key, window=1.0) if keyed else self.editor.transaction(label)):
                change()
        except (EditError, ValueError) as error:
            self._toast(str(error), warning=True)
            self.inspector.refresh()
            return False
        if coalesce and not keyed:
            self._coalesce_history(label)
        self._changed(label)
        return True

    def _coalesce_history(self, label: str, window: float = 1.0) -> None:
        stack = self.editor.undo_stack
        if not stack:
            return
        now = time.monotonic()
        top = stack[-1]
        if len(stack) > 1:
            previous = stack[-2]
            stamp = self._history_stamps.get(id(previous))
            if (previous.label == top.label == label and stamp is not None and now - stamp <= window
                    and hasattr(previous, "absorb")):
                previous.absorb(top)
                stack.pop()
                self._history_stamps[id(previous)] = now
                return
        self._history_stamps[id(top)] = now

    def rename_layer(self, object_id: str, name: str) -> bool:
        item = self.document.object(object_id)
        if item is None or not name.strip():
            return False
        return self.edit(f"Rename {item.name}", lambda: self.editor.set_property([object_id], "name", name.strip()))

    def align(self, action: str) -> None:
        selection = [item for item in self.document.selection if not item.locked]
        if not selection:
            return
        if len(selection) == 1:
            parent = self.document.object(selection[0].parent_id) if selection[0].parent_id else None
            if parent is None:
                self._toast("Put this layer in a frame to align it, or select more layers.")
                return
            left, top, right, bottom = parent.x, parent.y, parent.x + parent.width, parent.y + parent.height
        else:
            left = min(item.x for item in selection)
            top = min(item.y for item in selection)
            right = max(item.x + item.width for item in selection)
            bottom = max(item.y + item.height for item in selection)
        labels = {"left": "Align left", "right": "Align right", "center-x": "Align horizontal centers",
                  "top": "Align top", "bottom": "Align bottom", "center-y": "Align vertical centers",
                  "distribute-x": "Distribute horizontal spacing", "distribute-y": "Distribute vertical spacing"}

        def change() -> None:
            if action in {"distribute-x", "distribute-y"}:
                if len(selection) < 3:
                    return
                axis, size = ("x", "width") if action == "distribute-x" else ("y", "height")
                ordered = sorted(selection, key=lambda item: getattr(item, axis))
                span = (right - left if axis == "x" else bottom - top) - sum(getattr(item, size) for item in ordered)
                gap = span / (len(ordered) - 1)
                cursor = left if axis == "x" else top
                for item in ordered:
                    delta = cursor - getattr(item, axis)
                    self.editor.move([item.id], delta if axis == "x" else 0,
                                     delta if axis == "y" else 0)
                    cursor += getattr(item, size) + gap
                return
            for item in selection:
                values = {
                    "left": {"x": left}, "right": {"x": right - item.width},
                    "center-x": {"x": (left + right - item.width) / 2},
                    "top": {"y": top}, "bottom": {"y": bottom - item.height},
                    "center-y": {"y": (top + bottom - item.height) / 2},
                }[action]
                self.editor.move([item.id], values.get("x", item.x) - item.x,
                                 values.get("y", item.y) - item.y)

        self.edit(labels.get(action, "Align"), change)

    def export_selection(self, fmt: str, scale: int) -> None:
        self._export_dialog(fmt, scale)

    # ── export settings (the inspector's Export tab) ────────────────────

    def _page_export_settings(self) -> list[dict]:
        from .export import default_setting, normalize_setting
        stored = self.recall("export-page-settings", None)
        if not isinstance(stored, list):
            return [default_setting()]
        return [row for row in (normalize_setting(value) for value in stored) if row is not None]

    def _page_export_name(self) -> str:
        page = self.document.current_page() if hasattr(self.document, "current_page") else None
        return page.name if page is not None else self.session.display_name

    def export_targets(self, object_ids: list[str] | None) -> list:
        """What an Export button writes: each layer by its own rows, or the page."""
        from .export import export_settings
        if not object_ids:
            return [(self._page_export_name(), None, self._page_export_settings())]
        items = [item for item in (self.document.object(value) for value in object_ids) if item is not None]
        return [(item.name, [item.id], export_settings(item)) for item in items]

    def frame_export_targets(self) -> list:
        """Every top-level frame on the page, by its rows or else the page's."""
        from .export import export_settings
        fallback = self._page_export_settings()
        return [(item.name, [item.id], export_settings(item) or fallback) for item in self.document.objects
                if item.parent_id is None and item.visible and item.kind in {"frame", "component", "section", "page"}]

    def export_layers(self, object_ids: list[str] | None) -> None:
        from .export import plan_exports
        self._run_exports(plan_exports(self.export_targets(object_ids)))

    def export_frames(self) -> None:
        from .export import plan_exports
        self._run_exports(plan_exports(self.frame_export_targets()))

    def _run_exports(self, jobs: list) -> None:
        """One file: the save dialog. Several: pick a folder, write them all."""
        if self._fixture_blocked():
            return
        from .export import run_exports, write_export
        if not jobs:
            self._toast("Add an export setting first.")
            return
        document = self.document

        def finished(paths: list[Path], where: str) -> None:
            if not paths:
                return
            message = f"Exported {paths[0].name}" if len(paths) == 1 else f"Exported {len(paths)} files to {where}"
            self._toast(message)
            notification = Gio.Notification.new("Export complete")
            notification.set_body(message)
            application = self.get_application()
            if application is not None:
                application.send_notification("export-complete", notification)

        if len(jobs) == 1:
            job = jobs[0]
            dialog = Gtk.FileDialog(title="Export")
            dialog.set_initial_name(Path(job.path).name)

            def saved(dialog, result) -> None:
                try:
                    file = dialog.save_finish(result)
                except GLib.Error:
                    return
                destination = Path(file.get_path())
                try:
                    written = write_export(document, destination, job.setting, job.object_ids)
                except (OSError, ValueError) as error:
                    self._toast(f"Couldn’t export: {error}", warning=True)
                    return
                finished([written], destination.parent.name)
            dialog.save(self, None, saved)
            return

        dialog = Gtk.FileDialog(title=f"Export {len(jobs)} files to a folder", accept_label="Export")

        def chosen(dialog, result) -> None:
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            directory = Path(folder.get_path())
            try:
                written = run_exports(document, directory, jobs)
            except (OSError, ValueError) as error:
                self._toast(f"Couldn’t export: {error}", warning=True)
                return
            skipped = len(jobs) - len(written)
            finished(written, directory.name)
            if skipped:
                self._toast(f"{skipped} {'file was' if skipped == 1 else 'files were'} skipped: nothing visible to export.",
                            warning=True)
        dialog.select_folder(self, None, chosen)

    # ── layers callbacks ────────────────────────────────────────────────

    def _layers_selected(self, ids: list[str]) -> None:
        if ids == self.document.selected_ids:
            return
        self.document.select(ids)
        self.canvas.queue_draw()
        self.canvas.emit("selection-changed")

    def _layers_toggled(self, key: str, ids: list[str], value: bool) -> None:
        first = self.document.object(ids[0]) if ids else None
        noun = f"{len(ids)} layers" if len(ids) > 1 else (first.name if first else "layer")
        label = {("visible", True): f"Show {noun}", ("visible", False): f"Hide {noun}",
                 ("locked", True): f"Lock {noun}", ("locked", False): f"Unlock {noun}"}[(key, value)]
        self.edit(label, lambda: self.editor.set_property(ids, key, value))

    def _layers_reordered(self, moving: list[str], target: str, zone: str) -> None:
        placement = drop_target(self.document, moving, target, zone)
        if placement is None:
            return
        parent, before = placement
        wanted = set(moving)
        ordered = [item.id for item in self.document.objects if item.id in wanted]
        first = self.document.object(ordered[0]) if ordered else None
        if first is None:
            return
        label = f"Reorder {first.name}" if len(ordered) == 1 else f"Reorder {len(ordered)} layers"

        def change() -> None:
            for object_id in ordered:
                self.editor.reorder(object_id, before, parent)
        self.edit(label, change)

    def _layer_hovered(self, object_id: str | None) -> None:
        setter = getattr(self.canvas, "set_hover_object", None)
        if setter is not None:
            setter(object_id)
        elif hasattr(self.canvas, "_hover_id"):
            # Until the canvas has set_hover_object, outline through its hover state.
            self.canvas._hover_id = object_id
            self.canvas.queue_draw()

    def _asset_color(self, style_id: str) -> None:
        try:
            apply_color(self.editor, style_id)
        except (EditError, ValueError) as error:
            self._toast(str(error), warning=True)
            return
        self._changed("Apply color style")

    def _asset_text(self, style_id: str) -> None:
        try:
            apply_text_style(self.editor, style_id)
        except (EditError, ValueError) as error:
            self._toast(str(error), warning=True)
            return
        self._changed("Apply text style")

    def _asset_photo(self, asset_id: str) -> None:
        try:
            choose_photo(self.editor, asset_id)
        except (EditError, ValueError) as error:
            self._toast(str(error), warning=True)
            return
        self._changed("Choose photo")

    def _choose_image_at(self, x: float, y: float) -> None:
        """Import the selected image into the document before drawing it."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Add image")
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        image_filter.add_mime_type("image/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)
        dialog.set_filters(filters)

        def chosen(source, result) -> None:
            try:
                file = source.open_finish(result)
                if file is None or not file.get_path():
                    return
                self._import_image_at(Path(file.get_path()), x, y)
            except GLib.Error as error:
                if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    self._toast(str(error), warning=True)
            except (OSError, ValueError, EditError) as error:
                self._toast(f"Could not add image: {error}", warning=True)

        dialog.open(self, None, chosen)

    def _import_image_at(self, path: Path, x: float, y: float) -> bool:
        data = path.read_bytes()
        texture = Gdk.Texture.new_from_filename(str(path))
        wx, wy = self.canvas.view_to_world(x, y)
        asset_id = new_id("asset")
        embedded_name = f"{asset_id}{path.suffix.lower()}"
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        asset = Asset(asset_id, path.name, media_type, embedded_name=embedded_name,
                      width=texture.get_width(), height=texture.get_height())
        self.document.assets.append(asset)
        self.document.embedded_assets[embedded_name] = data
        added = self.edit("Add image", lambda: self.editor.create(
            "image", wx, wy, min(texture.get_width(), 400), min(texture.get_height(), 300),
            parent_id=self.canvas._parent_for_new(wx, wy), name=path.stem,
            asset_id=asset_id, fill=None, radius=16))
        if not added:
            self.document.assets.remove(asset)
            self.document.embedded_assets.pop(embedded_name, None)
            return False
        self._set_tool("select")
        return True

    def _comment_at(self, x: float, y: float) -> Gtk.Popover:
        """Pin or edit a document comment at the clicked canvas point."""
        existing = next((item for item in self.document.objects if item.kind == "comment"
                         and math.dist(self.canvas.world_to_view(item.x, item.y), (x, y)) <= 16), None)
        wx, wy = self.canvas.view_to_world(x, y)
        popover = Gtk.Popover(has_arrow=True)
        popover.set_parent(self.canvas)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        entry = Gtk.Entry(text=existing.text if existing else "")
        entry.set_placeholder_text("Write a comment")
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Comment"])
        body.append(entry)
        save = Gtk.Button(label="Save comment" if existing else "Add comment")
        body.append(save)
        popover.set_child(body)

        def commit(*_args) -> None:
            message = entry.get_text().strip()
            if not message:
                return
            if existing:
                self.edit("Edit comment", lambda: self.editor.set_property([existing.id], "text", message))
            else:
                self.edit("Add comment", lambda: self.editor.create(
                    "comment", wx, wy, 1, 1, name="Comment", text=message,
                    fill=None, use_style=False))
            popover.popdown()
            self.canvas.queue_draw()

        save.connect("clicked", commit)
        entry.connect("activate", commit)
        popover.connect("closed", lambda *_: GLib.idle_add(lambda: (popover.unparent(), False)[1]))
        popover.popup()
        entry.grab_focus()
        return popover

    def _asset_component(self, preset: str) -> None:
        try:
            insert_component_preset(self.editor, preset)
        except (EditError, ValueError) as error:
            self._toast(str(error), warning=True)
            return
        self._changed("Insert component")

    def open_layer_menu(self, button: Gtk.Widget) -> None:
        command_popover(self._selection_registry()).present_for(button)

    def _selection_registry(self) -> CommandRegistry:
        selection = self.document.selection
        groups = []
        if selection:
            ids = [item.id for item in selection]
            hidden = all(not item.visible for item in selection)
            locked = all(item.locked for item in selection)
            groups.append(CommandGroup(None, (
                Command("layer.rename", "Rename", lambda: self.layers.begin_rename(), shortcut=("Ctrl", "R")),
                Command("layer.duplicate", "Duplicate", self._duplicate, shortcut=("Ctrl", "D")),
            )))
            grouping = [Command("layer.group", "Group selection", lambda: self._group("group"), shortcut=("Ctrl", "G")),
                        Command("layer.frame", "Frame selection", lambda: self._group("frame"), shortcut=("Ctrl", "Alt", "G"))]
            if any(item.kind in {"group", "frame"} for item in selection):
                grouping.insert(1, Command("layer.ungroup", "Ungroup", lambda: self._group("ungroup"),
                                           shortcut=("Ctrl", "Shift", "G")))
            from .geometry import mask_mode
            masking = all(mask_mode(item) or (item.metadata.get("mask_group") and any(
                mask_mode(child) for child in self.document.children(item.id))) for item in selection)
            grouping.append(Command("layer.mask", "Release mask" if masking else "Use as mask",
                                    lambda: self.canvas.toggle_mask_selection(), shortcut=("Ctrl", "Alt", "M")))
            frames = [item for item in selection if item.kind in {"frame", "page", "component", "section"}]
            if frames:
                clipped = all(getattr(item, "clip_content", True) for item in frames)
                grouping.append(Command("layer.clip-content", "Clip content",
                                        lambda: self._set_clip_content([item.id for item in frames], not clipped),
                                        checked=lambda: clipped))
            groups.append(CommandGroup(None, tuple(grouping)))
            groups.append(CommandGroup(None, (
                Command("layer.front", "Bring to front", lambda: self._restack("front")),
                Command("layer.back", "Send to back", lambda: self._restack("back")),
            )))
            groups.append(CommandGroup(None, (
                Command("layer.visible", "Show" if hidden else "Hide",
                        lambda: self._layers_toggled("visible", ids, hidden)),
                Command("layer.lock", "Unlock" if locked else "Lock",
                        lambda: self._layers_toggled("locked", ids, not locked)),
            )))
            if len(selection) > 1 or (len(selection) == 1 and selection[0].kind == "boolean"):
                groups.append(CommandGroup(None, tuple(
                    Command(f"layer.{operation}", operation.title(), lambda op=operation: self._boolean(op),
                            f"luma-canvas-boolean-{operation}-symbolic", shortcut=("Alt", "Shift", key))
                    for operation, key in (("union", "U"), ("subtract", "S"), ("intersect", "I"), ("exclude", "E")))
                    + (Command("layer.flatten", "Flatten", lambda: self.canvas.flatten_selection(),
                               shortcut=("Ctrl", "E")),)))
            elif any(item.kind not in {"frame", "page", "section", "component", "instance"} for item in selection):
                groups.append(CommandGroup(None, (
                    Command("layer.flatten", "Flatten", lambda: self.canvas.flatten_selection(), shortcut=("Ctrl", "E")),)))
            groups.append(CommandGroup(None, (
                Command("layer.delete", "Delete", self._delete_selection, shortcut=("Delete",), destructive=True),
            )))
        else:
            groups.append(CommandGroup(None, (
                Command("canvas.fit", "Zoom to fit", self._zoom_to_fit, shortcut=("Shift", "1")),
                Command("canvas.grid", "Show grid", self._toggle_grid, checked=lambda: self.canvas.show_grid),
            )))
        return CommandRegistry(tuple(groups))

    def _open_context_menu(self, widget: Gtk.Widget, x: float, y: float, object_id: str | None) -> None:
        if object_id and object_id not in self.document.selected_ids:
            self.document.select([object_id])
            self._selection_changed()
            self.canvas.queue_draw()
        command_popover(self._selection_registry(), variant="desktop").present_at_pointer(widget, x, y)

    # ── toolbar menus ───────────────────────────────────────────────────

    def _document_menu(self) -> Gtk.PopoverMenu:
        self._document_model = Gio.Menu()
        rename = Gio.Menu()
        item = Gio.MenuItem.new(None, None)
        item.set_attribute_value("custom", GLib.Variant.new_string("rename"))
        rename.append_item(item)
        self._document_model.append_section(None, rename)
        location = Gio.Menu()
        location.append("Move…", "app.document-move")
        self._document_model.append_section("In Canvas", location)
        main = Gio.Menu()
        for label, action in (("Save a version", "app.document-save-version"),
                              ("Duplicate", "app.document-duplicate"),
                              ("Save a copy…", "app.document-save-copy")):
            main.append(label, action)
        revert = Gio.Menu()
        revert.append("Last opened", "app.document-revert-last-opened")
        revert.append("Last saved version", "app.document-revert-last-saved")
        revert.append("Browse versions…", "app.document-browse-versions")
        main.append_submenu("Revert to", revert)
        self._document_model.append_section(None, main)
        tail = Gio.Menu()
        tail.append("Show in Filer", "app.document-reveal")
        tail.append("Move to Trash", "app.document-trash")
        self._document_model.append_section(None, tail)
        popover = Gtk.PopoverMenu.new_from_model(self._document_model)
        popover.add_css_class("canvas-document-menu")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("canvas-rename-row")
        self.rename_entry = Gtk.Entry()
        self.rename_entry.add_css_class("canvas-rename-entry")
        self.rename_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Document name"])
        self.rename_entry.connect("activate", lambda entry: self._rename_document(entry.get_text()))
        self.rename_error = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.rename_error.add_css_class("canvas-field-error")
        box.append(self.rename_entry)
        box.append(self.rename_error)
        popover.add_child(box, "rename")
        return popover

    def _document_menu_opening(self, _button) -> None:
        self.rename_entry.set_text(self.session.display_name)
        self.rename_error.set_visible(False)
        # The folder heading says where the file is.
        location = Gio.Menu()
        location.append("Move…", "app.document-move")
        self._document_model.remove(1)
        self._document_model.insert_section(1, f"In {self.session.location_label}", location)

    def _zoom_menu(self) -> Gtk.PopoverMenu:
        menu = Gio.Menu()
        entry_section = Gio.Menu()
        item = Gio.MenuItem.new(None, None)
        item.set_attribute_value("custom", GLib.Variant.new_string("zoom-entry"))
        entry_section.append_item(item)
        menu.append_section(None, entry_section)
        steps = Gio.Menu()
        for label, action, accel in (("Zoom in", "win.zoom-in", "<Control>plus"),
                                     ("Zoom out", "win.zoom-out", "<Control>minus"),
                                     ("Zoom to fit", "win.zoom-fit", "<Shift>1"),
                                     ("Zoom to selection", "win.zoom-selection", "<Shift>2")):
            entry = Gio.MenuItem.new(label, action)
            entry.set_attribute_value("accel", GLib.Variant.new_string(accel))
            steps.append_item(entry)
        menu.append_section(None, steps)
        levels = Gio.Menu()
        for label, action in (("Zoom to 50%", "win.zoom-50"), ("Zoom to 100%", "win.zoom-100"),
                              ("Zoom to 200%", "win.zoom-200")):
            entry = Gio.MenuItem.new(label, action)
            if action == "win.zoom-100":
                entry.set_attribute_value("accel", GLib.Variant.new_string("<Shift>0"))
            levels.append_item(entry)
        menu.append_section(None, levels)
        toggles = Gio.Menu()
        rulers_item = Gio.MenuItem.new("Rulers", "win.show-rulers")
        rulers_item.set_attribute_value("accel", GLib.Variant.new_string("<Shift>r"))
        toggles.append_item(rulers_item)
        toggles.append("Show grid", "win.show-grid")
        toggles.append("Snap to pixel grid", "win.pixel-snap")
        toggles.append("Outline view", "win.outline")
        menu.append_section(None, toggles)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.add_css_class("canvas-zoom-menu")
        self.zoom_entry = Gtk.Entry(input_purpose=Gtk.InputPurpose.NUMBER)
        self.zoom_entry.add_css_class("canvas-zoom-entry")
        self.zoom_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Zoom percentage"])
        self.zoom_entry.connect("activate", self._zoom_entry_activated)
        popover.add_child(self.zoom_entry, "zoom-entry")
        return popover

    def _zoom_menu_opening(self, _button) -> None:
        self.zoom_entry.set_text(f"{self.document.zoom * 100:.0f}%")
        self._update_toggle_states()

    def _zoom_entry_activated(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip().rstrip("%").strip()
        try:
            value = float(text)
        except ValueError:
            entry.set_text(f"{self.document.zoom * 100:.0f}%")
            return
        self.zoom_button.popdown()
        self._zoom_to(value / 100)

    # ── zoom (the interaction stream's viewport API wins when present) ──

    def _zoom_to(self, zoom: float) -> None:
        zoom = _clamp(zoom, ZOOM_LIMITS[0], ZOOM_LIMITS[1])
        if zoom == 1.0 and hasattr(self.canvas, "zoom_to_100"):
            self.canvas.zoom_to_100()
        elif hasattr(self.canvas, "zoom_to"):
            self.canvas.zoom_to(zoom)
        else:
            width, height = self.canvas.get_width(), self.canvas.get_height()
            self.canvas.zoom_at(zoom / self.document.zoom, width / 2, height / 2)
        self._refresh_zoom_label()

    def _zoom_step(self, direction: int) -> None:
        if hasattr(self.canvas, "zoom_step"):
            self.canvas.zoom_step(direction, at_pointer=False)
            self._refresh_zoom_label()
            return
        current = self.document.zoom
        if direction > 0:
            target = next((level for level in ZOOM_LADDER if level > current * 1.0001), ZOOM_LIMITS[1])
        else:
            target = next((level for level in reversed(ZOOM_LADDER) if level < current / 1.0001), ZOOM_LIMITS[0])
        self._zoom_to(target)

    @staticmethod
    def _bounds_of(items: list[LayoutObject]) -> tuple[float, float, float, float] | None:
        if not items:
            return None
        left = min(item.x for item in items)
        top = min(item.y for item in items)
        return (left, top, max(item.x + item.width for item in items) - left,
                max(item.y + item.height for item in items) - top)

    def _fit_bounds(self, bounds: tuple[float, float, float, float] | None) -> None:
        if bounds is None:
            return
        left, top, width, height = bounds
        view_w, view_h = self.canvas.get_width(), self.canvas.get_height()
        if view_w <= 1 or view_h <= 1 or width <= 0 or height <= 0:
            return
        zoom = min((view_w - FIT_MARGIN * 2) / width, (view_h - FIT_MARGIN * 2) / height, 1.0)
        zoom = _clamp(zoom, ZOOM_LIMITS[0], ZOOM_LIMITS[1])
        self.document.zoom = zoom
        self.document.canvas_x = (view_w - width * zoom) / 2 - left * zoom
        self.document.canvas_y = (view_h - height * zoom) / 2 - top * zoom
        self.canvas.queue_draw()
        self._refresh_zoom_label()

    def _zoom_to_fit(self) -> None:
        if hasattr(self.canvas, "zoom_to_fit"):
            self.canvas.zoom_to_fit()
            self._refresh_zoom_label()
            return
        self._fit_bounds(self._bounds_of([item for item in self.document.objects
                                          if item.parent_id is None and item.visible]))

    def _zoom_to_selection(self) -> None:
        if not self.document.selection:
            self._zoom_to_fit()
            return
        if hasattr(self.canvas, "zoom_to_selection"):
            self.canvas.zoom_to_selection()
            self._refresh_zoom_label()
            return
        self._fit_bounds(self._bounds_of(self.document.selection))

    def _fit_active_artboard(self) -> None:
        self._zoom_to_fit()

    def _fit_v70_initial(self) -> None:
        """v71 fits two initial desktop artboards and one full-width phone artboard."""
        phone = self.creative_workspace.get_is_phone()
        frames = [item for item in self.document.objects
                  if item.parent_id is None and item.visible]
        frames = frames[:1 if phone else 2]
        bounds = self._bounds_of(frames)
        if bounds is None:
            return
        x, y, width, height = bounds
        view = fit_rect_with_insets(
            x, y, x + width, y + height,
            self.canvas.get_width(), self.canvas.get_height(),
            inset_left=290 if not phone and self.creative_left.get_visible() else 16,
            inset_right=316 if not phone and self.creative_right.get_visible() else 16,
            inset_top=150 if phone else 60, inset_bottom=120 if phone else 90,
        )
        self.document.zoom = view.zoom
        self.document.canvas_x = view.x
        self.document.canvas_y = view.y
        self.canvas.queue_draw()
        self._refresh_zoom_label()

    def _fit_all(self) -> None:
        self._zoom_to_fit()

    def _set_grid(self, value: bool) -> None:
        self.canvas.show_grid = value
        self.canvas.queue_draw()

    def _toggle_grid(self) -> None:
        self._set_grid(not self.canvas.show_grid)
        self._update_toggle_states()

    def _set_pixel_grid(self, value: bool) -> None:
        self.canvas.show_pixel_grid = value
        self.canvas.queue_draw()

    def _set_outline(self, value: bool) -> None:
        self.canvas.outline = value
        self.canvas.queue_draw()

    def _toggle_outline(self) -> None:
        self._set_outline(not self.canvas.outline)
        self._update_toggle_states()

    # ── tools ───────────────────────────────────────────────────────────

    OFF_RAIL_TOOLS = {"direct", "arrow", "zoom", "dropper", "scale"}
    LOCKABLE_TOOLS = {"frame", "shape", "path", "text", "image"}

    # ── canvas interaction hooks (see docs/batch1-interfaces.md) ────────

    def _connect_canvas_interaction(self) -> None:
        signals = {
            # The canvas changed tool itself: a shape finished, Escape, O, L.
            "tool-changed": self._canvas_tool_changed,
            # Zoom or scroll changed.
            "view-changed": lambda *_: self._refresh_zoom_label(),
            "viewport-changed": lambda *_: self._refresh_zoom_label(),
            # A drag or scrub in progress: the inspector follows the pointer.
            "transform-preview": lambda *_: self._refresh_inspectors(),
            # The current style or the selection's paint changed.
            "style-changed": lambda *_: self._refresh_inspectors(),
            # Vector edit mode began or ended: the inspector shows point controls.
            "vector-editing-changed": lambda *_: self._refresh_inspectors(),
            # Shift+R on the canvas: the menu check follows and the choice is remembered.
            "rulers-changed": lambda _canvas, shown: (self._update_toggle_states(), self.remember("show-rulers", bool(shown))),
        }
        for name, handler in signals.items():
            if GObject.signal_lookup(name, type(self.canvas)):
                self.canvas.connect(name, handler)

    # ── pages (interaction stream hooks) ────────────────────────────────

    def _sync_pages(self) -> None:
        """Layers' Pages group, the toolbar page menu and label follow the document."""
        document = self.document
        if not hasattr(document, "ensure_pages"):
            self.layers.sync(document)
            return
        current = document.ensure_pages()
        names = [page.name for page in document.pages]
        self.layers.sync(document, names, document.page_index())
        self.page_label.set_label(current.name)
        if not getattr(self, "_pages_wired", False):
            self._pages_wired = True
            self.layers.pages.connect("row-activated", lambda _list, row: self._show_page_index(row.get_index()))
            self.layers.pages.connect("row-selected", lambda _list, row: row is not None and self._show_page_index(row.get_index()))
            head = self.layers.pages.get_prev_sibling()
            if isinstance(head, Gtk.Box):
                add = IconButton("luma-plus-symbolic", "New page", context=self.context, quiet=True)
                add.add_css_class("canvas-page-add")
                add.connect("clicked", lambda *_: self._new_page())
                head.append(add)
            rename = Gtk.GestureClick(button=1)
            rename.connect("pressed", lambda _g, count, _x, _y: count == 2 and self._rename_page())
            self.layers.pages.add_controller(rename)
        signature = [(page.id, page.name) for page in document.pages]
        if signature != getattr(self, "_page_signature", None):
            self._page_signature = signature
            self._page_menu.remove_all()
            pages = Gio.Menu()
            for page in document.pages:
                pages.append(page.name, f"win.page::{page.id}")
            self._page_menu.append_section(None, pages)
            commands = Gio.Menu()
            commands.append("New page", "win.page-new")
            commands.append("Rename page…", "win.page-rename")
            if len(document.pages) > 1:
                commands.append("Delete page", "win.page-delete")
            self._page_menu.append_section(None, commands)
        state = self._page_action_ref.get_state().get_string()
        if state != current.id:
            self._page_action_ref.set_state(GLib.Variant.new_string(current.id))

    def _page_action(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        action.set_state(value)
        page_id = value.get_string()
        if page_id != getattr(self.document, "current_page_id", None) and hasattr(self.canvas, "show_page"):
            self.canvas.show_page(page_id)
            self._refresh_all()

    def _show_page_index(self, index: int) -> bool:
        pages = getattr(self.document, "pages", [])
        if 0 <= index < len(pages) and pages[index].id != self.document.current_page_id:
            self.canvas.show_page(pages[index].id)
            self._refresh_all()
        return False

    def _new_page(self) -> None:
        def add():
            self.editor.add_page()
            self.editor.create("frame", 0, 0, 1920, 1080, name="Frame 1", fill="#ffffff")
        if self.edit("Add page", add):
            self.canvas.zoom_to_fit(animate=False)
            self.canvas.queue_draw()
            self.canvas.emit("page-changed")

    def _delete_page(self) -> None:
        page = self.document.current_page()
        if page is not None:
            self.edit(f"Delete {page.name}", lambda: self.editor.delete_page(page.id))

    def _rename_page(self) -> bool:
        page = self.document.current_page()
        if page is None:
            return False
        popover = Gtk.Popover()
        popover.set_has_arrow(False)
        entry = Gtk.Entry(text=page.name)
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Page name"])
        entry.set_width_chars(18)

        def commit(*_args) -> None:
            name = entry.get_text().strip()
            popover.popdown()
            if name and name != page.name:
                self.edit(f"Rename page to {name}", lambda: self.editor.rename_page(page.id, name))
        entry.connect("activate", commit)
        popover.set_child(entry)
        popover.set_parent(self.page_button)
        popover.connect("closed", lambda widget: GLib.idle_add(lambda: widget.unparent() or False))
        popover.popup()
        entry.grab_focus()
        return False

    def _set_clip_content(self, ids: list[str], active: bool) -> None:
        self.edit("Clip content" if active else "Show overflow",
                  lambda: self.editor.set_property(ids, "clip_content", active))

    def _add_tool_lock(self, button: Gtk.ToggleButton, overlay: Gtk.Overlay, tool: str) -> None:
        """Double-click a drawing tool to keep it after each shape (a lock shows)."""
        if tool not in self.LOCKABLE_TOOLS:
            return
        badge = Gtk.Image(icon_name="luma-canvas-lock-symbolic", pixel_size=8,
                          halign=Gtk.Align.END, valign=Gtk.Align.START)
        badge.add_css_class("canvas-tool-lock")
        badge.set_visible(False)
        overlay.add_overlay(badge)
        button.canvas_lock = badge
        clicks = Gtk.GestureClick(button=1)
        clicks.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        clicks.connect("released", lambda gesture, count, _x, _y, value=tool: self._tool_clicked(count, value))
        button.add_controller(clicks)

    def _tool_clicked(self, count: int, tool: str) -> None:
        if count < 2 or not hasattr(self, "canvas"):
            return
        target = self._shape_tool if tool == "shape" else tool
        self._set_tool(target)
        if hasattr(self.canvas, "tool_locked"):
            self.canvas.set_tool(target, locked=not self.canvas.tool_locked)
        self._sync_tool_locks()

    def _sync_tool_locks(self) -> None:
        locked = bool(getattr(self.canvas, "tool_locked", False)) if hasattr(self, "canvas") else False
        tool = getattr(self.canvas, "tool", "") if hasattr(self, "canvas") else ""
        rail_tool = "shape" if tool in SHAPE_TOOLS or tool == "arrow" else tool
        for name, button in self._tool_buttons.items():
            badge = getattr(button, "canvas_lock", None)
            if badge is not None:
                badge.set_visible(locked and name == rail_tool)
        move = self._tool_buttons.get("select")
        if move is not None:
            scale = tool == "scale"
            move.canvas_image.set_from_icon_name("luma-canvas-scale-symbolic" if scale else "luma-canvas-move-symbolic")
            move.set_tooltip_text("Scale · K" if scale else "Move · V")
            move.update_property([Gtk.AccessibleProperty.LABEL], ["Scale" if scale else "Move"])

    def _canvas_tool_changed(self, _canvas, tool: str) -> None:
        """Sync the rail without re-entering the canvas."""
        GLib.idle_add(lambda: self._sync_tool_locks() or False)
        if self._creative_active:
            creative_tool = "move" if tool in {"select", "scale"} else tool
            if creative_tool in {"move", "frame", "rectangle", "ellipse", "line", "arrow", "path",
                                 "text", "image", "hand", "comment"}:
                self.creative_toolbar.set_current(creative_tool)
        if tool == "scale":
            tool = "select"
        if tool in SHAPE_TOOLS:
            self._shape_tool = tool
            label, key, icon = SHAPE_TOOLS[tool]
            self._tool_buttons["shape"].canvas_image.set_from_icon_name(icon)
            self._tool_buttons["shape"].set_tooltip_text(f"{label} · {key}" if key else label)
            rail_tool = "shape"
        else:
            rail_tool = tool
        button = self._tool_buttons.get(rail_tool)
        if button is not None and not button.get_active():
            button.handler_block_by_func(self._tool_toggled)
            button.set_active(True)
            button.handler_unblock_by_func(self._tool_toggled)

    def _tool_toggled(self, button, tool: str) -> None:
        if button.get_active():
            if tool == "select" and getattr(getattr(self, "canvas", None), "tool", None) == "scale":
                return
            self._set_tool(self._shape_tool if tool == "shape" else tool)

    def _set_tool(self, tool: str) -> None:
        if not hasattr(self, "canvas"):
            return
        if tool in SHAPE_TOOLS:
            if tool not in self._canvas_shape_tools():
                return
            self._shape_tool = tool
            button = self._tool_buttons["shape"]
            label, key, icon = SHAPE_TOOLS[tool]
            button.canvas_image.set_from_icon_name(icon)
            button.set_tooltip_text(f"{label} · {key}" if key else label)
            rail_tool = "shape"
        elif tool == "scale":
            rail_tool = "select"
        elif tool in self._tool_buttons or tool in self.OFF_RAIL_TOOLS:
            rail_tool = tool
        else:
            return
        self.canvas.cancel_pen()
        if rail_tool in self._tool_buttons and not self._tool_buttons[rail_tool].get_active():
            self._tool_buttons[rail_tool].set_active(True)
        if self.canvas.tool != tool:
            self.canvas.set_tool(tool)
        self._sync_tool_locks()
        if self._creative_active:
            creative_tool = "move" if tool in {"select", "scale"} else tool
            if creative_tool in {"move", "frame", "rectangle", "ellipse", "line", "arrow", "path",
                                 "text", "image", "hand", "comment"}:
                self.creative_toolbar.set_current(creative_tool)

    # ── keyboard (the interaction stream's input router may take this over) ──

    def _modifiers_changed(self, controller, state=None, *_args) -> bool:
        if state is None:
            state = controller.get_current_event_state()
        if hasattr(self.canvas, "set_snap_suppressed"):
            self.canvas.set_snap_suppressed(bool(state & Gdk.ModifierType.ALT_MASK))
        if hasattr(self.canvas, "set_measuring"):
            self.canvas.set_measuring(bool(state & Gdk.ModifierType.CONTROL_MASK))
        return False

    def _typing(self) -> bool:
        focus = self.get_focus()
        return isinstance(focus, (Gtk.Text, Gtk.Entry, Gtk.TextView, Gtk.SearchEntry, Gtk.SpinButton))

    def _key_pressed(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if self._typing():
            return False
        key = Gdk.keyval_name(keyval) or ""
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if control and not alt and getattr(self.canvas, "_text", None) is None:
            command = {"x": "edit.cut", "c": "edit.copy", "v": "edit.paste-over" if shift else "edit.paste",
                       "a": "edit.select-all"}.get(key.lower())
            if command is not None:
                self.commands.invoke(command)
                return True
        if control or alt:
            return False
        if key == "F2":
            return self.layers.begin_rename()
        if shift and key in {"parenright", "0"}:
            self._zoom_to(1.0)
            return True
        if shift and key in {"exclam", "1"}:
            self._zoom_to_fit()
            return True
        if shift and key in {"at", "2"}:
            self._zoom_to_selection()
            return True
        if shift and key.lower() == "r":
            self.canvas.toggle_rulers()
            return True
        if key.lower() in TOOL_KEYS and not shift:
            self._set_tool(TOOL_KEYS[key.lower()])
            return True
        if key in {"plus", "equal"}:
            self._zoom_step(1)
            return True
        if key == "minus":
            self._zoom_step(-1)
            return True
        if key in {"Delete", "BackSpace"}:
            self._delete_selection()
            return True
        if key == "Escape":
            self.canvas.cancel_pen()
            if self.canvas.tool != "select":
                self._set_tool("select")
            elif self.document.selected_ids:
                self.document.select([])
                self._selection_changed()
                self.canvas.queue_draw()
            return True
        directions = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}
        if key in directions and self.document.selected_ids:
            dx, dy = directions[key]
            movable = [item.id for item in self.document.selection if not item.locked]
            if movable:
                step = self.large_nudge if shift else 1.0
                self.edit("Nudge", lambda: self.editor.move(movable, dx * step, dy * step), coalesce=True)
            return True
        return False

    # ── edit commands ───────────────────────────────────────────────────

    def _undo(self) -> None:
        if self.editor.undo_stack:
            self.editor.undo()
            self._changed("Undo")

    def _redo(self) -> None:
        if self.editor.redo_stack:
            self.editor.redo()
            self._changed("Redo")

    def _duplicate(self) -> None:
        ids = list(self.document.selected_ids)
        if ids and self.edit("Duplicate", lambda: self.editor.duplicate(ids)):
            self._selection_changed()

    def _delete_selection(self) -> None:
        ids = list(self.document.selected_ids)
        if ids and self.edit("Delete", lambda: self.editor.delete(ids)):
            self._selection_changed()

    def _group(self, how: str) -> None:
        """Group, ungroup or frame the selection through the canvas."""
        method = {"group": "group_selection", "ungroup": "ungroup_selection", "frame": "frame_selection"}[how]
        action = getattr(self.canvas, method, None)
        if action is None:
            return
        action()  # the canvas emits selection-changed and document-changed

    def _restack(self, where: str) -> None:
        selection = [item for item in self.document.selection if not item.locked]
        if not selection:
            return
        # Moving several layers up one step goes top-most first, down one step
        # bottom-most first, so they keep their order.
        ordered = sorted(selection, key=self.document.objects.index, reverse=where in {"forward", "front"})

        def change() -> None:
            for item in ordered:
                siblings = [other for other in self.document.objects
                            if other.parent_id == item.parent_id and other.id != item.id]
                index = next((i for i, other in enumerate(self.document.objects) if other.id == item.id), 0)
                above = [other for other in siblings if self.document.objects.index(other) > index]
                below = [other for other in siblings if self.document.objects.index(other) < index]
                if where == "front":
                    before = None
                elif where == "back":
                    before = siblings[0].id if siblings else None
                elif where == "forward":
                    if not above:
                        continue
                    before = above[1].id if len(above) > 1 else None
                else:
                    if not below:
                        continue
                    before = below[-1].id
                self.editor.reorder(item.id, before, item.parent_id)
        labels = {"front": "Bring to front", "back": "Send to back", "forward": "Bring forward", "backward": "Send backward"}
        self.edit(labels[where], change)

    def _outline_stroke(self) -> None:
        from .vector import outline_stroke
        items = [item for item in self.document.selection
                 if item.kind == "path" and item.stroke and item.stroke_width and not item.locked]

        def change() -> None:
            for item in items:
                path = outline_stroke(item)
                if not path:
                    continue
                self.editor.set_property([item.id], "path", path)
                self.editor.set_property([item.id], "fill", item.stroke)
                self.editor.set_property([item.id], "stroke", None)
                self.editor.set_property([item.id], "stroke_width", 0.0)
        if items:
            self.edit("Outline stroke", change)

    def _boolean(self, operation: str) -> None:
        selection = [item for item in self.document.selection if not item.locked]
        if len(selection) < 2 and not (len(selection) == 1 and selection[0].kind == "boolean"):
            self._toast("Select two or more shapes to combine them.")
            return
        self.canvas.boolean_selection(operation)

    # ── documents ───────────────────────────────────────────────────────

    def _save_status_changed(self, state: str, detail: str) -> None:
        if state == SaveState.SAVING:
            self._header_status = "Canvas · Saving"
            self.save_status.pending()
        elif state == SaveState.FAILED:
            self._header_status = "Canvas · Not saved"
            self.save_status.failed(detail)
            self._show_banner(f"Couldn’t save “{self.session.display_name}”. {detail}", "Save a copy…", self._save_copy)
        else:
            self._header_status = "Canvas · Saved"
            self.save_status.saved("Saved")
            if self._banner_action == self._save_copy:
                self._hide_banner()
        if hasattr(self, "document_header"):
            self.document_header.set_title(self.session.display_name, self._header_status)
        if self.session.path != self.path:
            self.path = self.session.path

    def _show_banner(self, title: str, button: str, action: Callable[[], None]) -> None:
        self.banner.set_title(title)
        self.banner.set_button_label(button)
        self._banner_action = action
        self.banner.set_revealed(True)

    def _hide_banner(self) -> None:
        self.banner.set_revealed(False)
        self._banner_action = None

    def _activity_changed(self, *_args) -> None:
        if self.is_active():
            self._update_command_states()
        if not self.is_active():
            self.session.note_change()
            self.session.save_now()

    def flush(self, timeout: float = 5.0) -> bool:
        self.session.note_change()
        return self.session.flush(timeout)

    def _close_requested(self, _window) -> bool:
        if self._closing_confirmed or self.flush():
            self.session.close()
            return False
        dialog = Adw.AlertDialog(
            heading=f"Close without saving “{self.session.display_name}”?",
            body="Recent changes are kept in Canvas’s recovery folder and will reopen next time.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("close", "Close")
        dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")

        def answered(_dialog, response: str) -> None:
            if response == "close":
                self._closing_confirmed = True
                self.close()
        dialog.connect("response", answered)
        dialog.present(self)
        return True

    def _new(self) -> None:
        if self._welcome_active:
            self.replace_document(new_document(), None, opened=False)
            return
        application = self.get_application()
        if hasattr(application, "new_window"):
            application.new_window()
        else:
            CanvasWindow(application, show_welcome=False).present()

    def _open(self) -> None:
        if not self._fixture_blocked():
            self._show_documents()

    def _open_saved_design(self, path: Path) -> None:
        if self.session.path is not None and path.resolve() == self.session.path.resolve():
            self._reveal_editor()
            return
        self.get_application().open_path(path, parent=self, reuse=self)

    def _browse_files(self) -> None:
        if self._fixture_blocked():
            return
        dialog = Gtk.FileDialog(title="Open")
        documents = Gtk.FileFilter(name="All supported files")
        for pattern in (f"*{DOCUMENT_SUFFIX}", *[f"*{suffix}" for suffix in LEGACY_DOCUMENT_SUFFIXES], "*.svg", "*.fig"):
            documents.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(documents)
        for name, patterns in (("Canvas documents", (f"*{DOCUMENT_SUFFIX}", *[f"*{suffix}" for suffix in LEGACY_DOCUMENT_SUFFIXES])),
                               ("SVG files", ("*.svg",)), ("Figma files", ("*.fig",))):
            single = Gtk.FileFilter(name=name)
            for pattern in patterns:
                single.add_pattern(pattern)
            filters.append(single)
        dialog.set_filters(filters)
        dialog.set_default_filter(documents)
        dialog.open(self, None, self._open_finished)

    def _import(self) -> None:
        """Import… adds another file's pages to this document."""
        if self._fixture_blocked():
            return
        dialog = Gtk.FileDialog(title="Import")
        figma = Gtk.FileFilter(name="Figma files")
        figma.add_pattern("*.fig")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(figma)
        dialog.set_filters(filters)
        dialog.set_default_filter(figma)

        def chosen(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file is not None and file.get_path():
                self.import_file(Path(file.get_path()))
        dialog.open(self, None, chosen)

    def import_file(self, path: Path) -> None:
        if self._fixture_blocked():
            return
        """Add a Figma file's pages after the current page, as one undo step."""
        from .fig_import_ui import merge_pages, note_missing_fonts, run_import, show_report

        def succeeded(result) -> None:
            if merge_pages(self, result.document, path.stem) is None:
                return
            note_missing_fonts(result.report, result.document, self)
            show_report(self, result.report, path.name, pages=len(result.document.pages),
                        layers=int(result.report.stats.get("layers", 0)))
            GLib.idle_add(lambda: ((self.canvas.zoom_to_selection() if any(item.metadata.get("prototype_start") for item in self.document.selection) else self._zoom_to_fit()), False)[1])

        def failed(reason: str) -> None:
            dialog = Adw.AlertDialog(heading=f"Couldn’t import “{path.name}”", body=reason)
            dialog.add_response("close", "Close")
            dialog.set_close_response("close")
            dialog.present(self)

        run_import(path, self, succeeded, failed, verb="Importing")

    def _open_finished(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file is None or file.get_path() is None:
            return
        application = self.get_application()
        application.open_path(Path(file.get_path()), parent=self, reuse=self if self._welcome_active else None)

    def _files_dropped(self, _target, value, _x: float, _y: float) -> bool:
        if self._fixture_blocked():
            return True
        application = self.get_application()
        opened = False
        for file in value.get_files():
            if file.get_path() and Path(file.get_path()).suffix.lower() in {DOCUMENT_SUFFIX, *LEGACY_DOCUMENT_SUFFIXES, ".svg", ".fig"}:
                application.open_path(Path(file.get_path()), parent=self,
                                      reuse=self if self._welcome_active else None)
                opened = True
        if not opened:
            self._toast("Drop a Canvas document, an SVG or a Figma file.")
        return opened

    def _focus_rename(self) -> None:
        self.document_button.popup()
        GLib.idle_add(lambda: (self.rename_entry.grab_focus(), False)[1])

    def _rename_document(self, name: str) -> None:
        try:
            self.session.note_change()
            self.session.rename(name)
        except FileExistsError:
            self.rename_error.set_label("A document with that name already exists")
            self.rename_error.set_visible(True)
            return
        except (ValueError, OSError) as error:
            self.rename_error.set_label(str(error))
            self.rename_error.set_visible(True)
            return
        self.document_button.popdown()
        self.document.name = self.session.display_name
        self.path = self.session.path
        self._refresh_all()

    def _move_document(self) -> None:
        if self._fixture_blocked():
            return
        dialog = Gtk.FileDialog(title="Move to folder")

        def chosen(dialog, result) -> None:
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            try:
                self.session.move(Path(folder.get_path()))
            except (FileExistsError, OSError) as error:
                self._toast(str(error), warning=True)
                return
            self.path = self.session.path
            self._refresh_all()
            self._toast(f"Moved to {self.session.location_label}")
        dialog.select_folder(self, None, chosen)

    def _save_version(self) -> None:
        if self._fixture_blocked():
            return
        self.session.note_change()

        def created(version) -> None:
            Toast.show(self.center_pane, "Version saved", kind="saved")
        self.session.create_version("named", on_done=created)

    def _duplicate_document(self) -> None:
        if self._fixture_blocked():
            return
        self.session.note_change()
        try:
            path = self.session.duplicate()
        except OSError as error:
            self._toast(f"Couldn’t duplicate: {error}", warning=True)
            return
        self.get_application().open_path(path, parent=self)

    def _save_copy(self) -> None:
        if self._fixture_blocked():
            return
        dialog = Gtk.FileDialog(title="Save a copy")
        dialog.set_initial_name(f"{self.session.display_name} copy{DOCUMENT_SUFFIX}")

        def chosen(dialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return
            destination = Path(file.get_path())
            if destination.suffix.lower() != DOCUMENT_SUFFIX:
                destination = destination.with_suffix(DOCUMENT_SUFFIX)
            try:
                self.session.copy_to(destination)
            except (OSError, ValueError) as error:
                self._toast(f"Couldn’t save a copy: {error}", warning=True)
                return
            self._toast(f"Saved a copy as {destination.name}")
        dialog.save(self, None, chosen)

    def _apply_snapshot(self, snapshot: dict, label: str, assets: dict | None = None) -> None:
        replacement = Document.from_dict(snapshot)

        def change() -> None:
            document = self.document
            for field in ("objects", "styles", "variables", "assets", "connections", "current_style"):
                if hasattr(replacement, field) and hasattr(document, field):
                    setattr(document, field, getattr(replacement, field))
            known = {item.id for item in document.objects}
            document.select([object_id for object_id in document.selected_ids if object_id in known])
            if assets:
                document.embedded_assets.update(assets)
            document.touch()
        self.session.keep_restore_point(f"Before {label[0].lower()}{label[1:]}")
        if self.edit(label, change):
            self._selection_changed()

    def _revert(self, which: str) -> None:
        snapshot = self.session.last_opened if which == "opened" else self.session.last_saved
        if snapshot is None:
            self._toast("This document hasn’t been saved yet.")
            return
        self._apply_snapshot(snapshot, "Revert to last opened" if which == "opened" else "Revert to last saved version")

    def _browse_versions(self, select: str | None = None, rename: bool = False) -> None:
        if self._fixture_blocked():
            return
        from .versions_sheet import VersionsSheet
        if self._versions_sheet is not None:
            return
        self.session.note_change()
        self._versions_sheet = VersionsSheet(self, select=select, rename=rename)

    def versions_sheet_closed(self) -> None:
        self._versions_sheet = None

    def restore_version(self, version_id: str) -> None:
        snapshot, assets = self.session.load_version(version_id)
        self._apply_snapshot(snapshot, "Restore version", assets)

    def _reveal_in_files(self) -> None:
        if self.session.path is None or not self.session.path.exists():
            self._toast("This document isn’t saved to a file yet.")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.session.path)))

        def done(launcher, result) -> None:
            try:
                launcher.open_containing_folder_finish(result)
            except GLib.Error:
                pass
        launcher.open_containing_folder(self, None, done)

    def _trash_document(self) -> None:
        if self._fixture_blocked():
            return
        path = self.session.path
        self.flush()
        snapshot = self.document.to_dict()
        assets = dict(self.document.embedded_assets)
        if path is not None and path.exists():
            try:
                Gio.File.new_for_path(str(path)).trash(None)
            except GLib.Error as error:
                self._toast(f"Couldn’t move it to Trash: {error.message}", warning=True)
                return
        self.session.pristine = True
        self.session.untitled = True
        application = self.get_application()
        self._closing_confirmed = True
        self.close()
        if path is not None and hasattr(application, "announce_trashed"):
            application.announce_trashed(path, snapshot, assets)

    def _export_dialog(self, fmt: str = "png", scale: int = 1) -> None:
        if self._fixture_blocked():
            return
        selection = self.document.selection
        base = selection[0].name if len(selection) == 1 else self.session.display_name
        suffix = {"jpeg": ".jpg"}.get(fmt, f".{fmt}")
        dialog = Gtk.FileDialog(title="Export")
        dialog.set_initial_name(f"{base}{'@' + str(scale) + 'x' if scale > 1 else ''}{suffix}")

        def chosen(dialog, result) -> None:
            try:
                file = dialog.save_finish(result)
            except GLib.Error:
                return
            destination = Path(file.get_path())
            try:
                export(self.document, destination, fmt=fmt, scale=scale, object_ids=self.document.selected_ids or None)
            except (OSError, ValueError) as error:
                self._toast(f"Couldn’t export: {error}", warning=True)
                return
            self._toast(f"Exported {destination.name}")
            notification = Gio.Notification.new("Export complete")
            notification.set_body(f"Exported {destination.name}")
            self.get_application().send_notification("export-complete", notification)
        dialog.save(self, None, chosen)

    # ── search and help ─────────────────────────────────────────────────

    def _show_command_search(self) -> None:
        popover = Gtk.Popover()
        popover.add_css_class("canvas-command-search")
        popover.set_parent(self.context_bar)
        popover.connect("closed", lambda *_: GLib.idle_add(lambda: (popover.unparent(), False)[1]))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        search = Gtk.SearchEntry(placeholder_text="Search commands")
        box.append(search)
        results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        results.add_css_class("canvas-command-results")
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=240,
                                    max_content_height=360, propagate_natural_height=True)
        scroll.set_child(results)
        box.append(scroll)
        rows = []
        for group in self.commands.visible_groups():
            for command in group.commands:
                row = Gtk.ListBoxRow()
                line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                line.append(Gtk.Label(label=command.label, xalign=0, hexpand=True))
                if command.shortcut:
                    keys = Gtk.Label(label="+".join(command.shortcut))
                    keys.add_css_class("canvas-row-meta")
                    line.append(keys)
                row.set_child(line)
                row.command_id = command.id
                results.append(row)
                rows.append((row, f"{command.label} {group.label or ''}".casefold()))
        results.connect("row-activated", lambda _list, row: (popover.popdown(), self.commands.invoke(row.command_id)))
        search.connect("search-changed", lambda entry: [row.set_visible(entry.get_text().casefold() in text)
                                                        for row, text in rows])
        popover.set_child(box)
        popover.popup()
        search.grab_focus()

    def _shortcuts(self) -> None:
        lines = [f"{command.label}: {'+'.join(command.shortcut)}"
                 for group in self.commands.visible_groups() for command in group.commands if command.shortcut]
        lines += ["Cut: Ctrl+X", "Copy: Ctrl+C", "Paste: Ctrl+V", "Paste over selection: Ctrl+Shift+V",
                  "Select all: Ctrl+A", "Delete: Delete / Backspace"]
        lines += ["Move: V", "Frame: F", "Rectangle: R", "Ellipse: O", "Pen: P", "Text: T", "Hand: H"]
        dialog = Adw.AlertDialog(heading="Keyboard shortcuts", body="\n".join(lines))
        dialog.add_response("close", "Close")
        dialog.present(self)

    def _toast(self, message: str, warning: bool = False) -> None:
        Toast.show(self.center_pane, message, kind="warning" if warning else "done")

    def _fixture_blocked(self) -> bool:
        if not self._fixture_mode:
            return False
        self._toast("Fixture mode keeps documents in memory")
        return True
