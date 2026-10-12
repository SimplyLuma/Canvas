# SPDX-License-Identifier: MPL-2.0
"""Browse versions: a sheet over the canvas island (CANVAS-SPEC 13.3).

A list on the leading side, grouped by day with named versions in bold, and a
read-only preview of the chosen version on the canvas. Restore keeps the
current state as a version first; Escape or Done returns to the document
untouched.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from gi.repository import Gdk, GLib, Gtk, Pango
from luma_appkit import InlineRenameField

from .editing import Editor
from .model import Document
from .versions import NAMED


def _day_label(moment: datetime.datetime, today: datetime.date) -> str:
    if moment.date() == today:
        return "Today"
    if moment.date() == today - datetime.timedelta(days=1):
        return "Yesterday"
    return moment.strftime("%B %-d, %Y") if moment.year != today.year else moment.strftime("%B %-d")


class VersionsSheet:
    def __init__(self, window, *, select: str | None = None, rename: bool = False) -> None:
        self.window = window
        self.session = window.session
        self.versions = self.session.list_versions()
        self.selected: str | None = None
        self._rows: dict[str, Gtk.ListBoxRow] = {}

        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START)
        self.panel.add_css_class("canvas-versions-panel")
        self.panel.set_size_request(260, -1)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.add_css_class("canvas-versions-head")
        title = Gtk.Label(label="Versions", xalign=0, hexpand=True)
        title.add_css_class("canvas-versions-title")
        head.append(title)
        done = Gtk.Button(label="Done")
        done.add_css_class("luma-button")
        done.add_css_class("small")
        done.connect("clicked", lambda *_: self.close())
        head.append(done)
        self.panel.append(head)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("canvas-versions-list")
        self.list.connect("row-selected", self._row_selected)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_child(self.list)
        self.panel.append(scroll)

        self.actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.actions.add_css_class("canvas-versions-actions")
        self.restore = self._action("Restore this version", self._restore, primary=True)
        self.copy = self._action("Duplicate as new document", self._duplicate)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        self.name = self._action("Name…", self._begin_rename, container=line)
        self.delete = self._action("Delete version", self._delete, container=line)
        self.delete.add_css_class("destructive")
        self.actions.append(line)
        self.panel.append(self.actions)

        self._fill()
        window.stage_overlay.add_overlay(self.panel)
        self.keys = Gtk.EventControllerKey()
        self.keys.connect("key-pressed", self._key)
        window.add_controller(self.keys)
        self.original_editor = window.editor
        if not self.versions:
            empty = Gtk.Label(label="Versions appear here as you work, and each time you save one with Ctrl+S.",
                              wrap=True, xalign=0)
            empty.add_css_class("canvas-panel-note")
            self.list.append(empty)
            self._set_actions(False)
            return
        target = self._rows.get(select) if select else self.list.get_row_at_index(1)
        if target is None:
            target = next(iter(self._rows.values()))
        self.list.select_row(target)
        if rename:
            GLib.idle_add(lambda: (self._begin_rename(), False)[1])

    def _action(self, label: str, callback, *, primary: bool = False, container: Gtk.Box | None = None) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.add_css_class("luma-button")
        button.add_css_class("small")
        if primary:
            button.add_css_class("primary")
        button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        button.connect("clicked", lambda *_: callback())
        (container or self.actions).append(button)
        return button

    def _set_actions(self, enabled: bool) -> None:
        for button in (self.restore, self.copy, self.name, self.delete):
            button.set_sensitive(enabled)

    def _fill(self) -> None:
        today = datetime.date.today()
        current_day = None
        for version in self.versions:
            moment = datetime.datetime.fromtimestamp(version.created)
            day = _day_label(moment, today)
            if day != current_day:
                current_day = day
                label = Gtk.Label(label=day, xalign=0)
                label.add_css_class("canvas-group-label")
                self.list.append(label)
                label.get_parent().set_selectable(False)
                label.get_parent().set_activatable(False)
            row = Gtk.ListBoxRow()
            row.version_id = version.id
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            line.add_css_class("canvas-version-row")
            thumb_path = self.session.versions.thumbnail_path(self.window.document.id, version.id)
            picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
            picture.set_size_request(48, 36)
            picture.add_css_class("canvas-version-thumb")
            if thumb_path is not None:
                picture.set_filename(str(thumb_path))
            line.append(picture)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
            title = Gtk.Label(label=version.title, xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.add_css_class("canvas-version-title")
            if version.kind == NAMED or version.name:
                title.add_css_class("named")
            text.append(title)
            time_label = Gtk.Label(label=moment.strftime("%-I:%M %p"), xalign=0)
            time_label.add_css_class("canvas-row-meta")
            text.append(time_label)
            line.append(text)
            row.set_child(line)
            row.title_label = title
            self.list.append(row)
            self._rows[version.id] = row

    def _row_selected(self, _list, row) -> None:
        if row is None or not hasattr(row, "version_id"):
            return
        self.selected = row.version_id
        try:
            snapshot, assets = self.session.load_version(row.version_id)
            preview = Document.from_dict(snapshot)
        except (OSError, KeyError, ValueError):
            self.window._toast("That version can’t be read.", warning=True)
            return
        preview.embedded_assets = assets
        preview.zoom, preview.canvas_x, preview.canvas_y = (
            self.window.document.zoom, self.window.document.canvas_x, self.window.document.canvas_y)
        preview.select([])
        self.window.canvas.replace_editor(Editor(preview))
        self.window.canvas.set_sensitive(False)
        self._set_actions(True)

    def _key(self, _controller, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def close(self) -> None:
        self.window.stage_overlay.remove_overlay(self.panel)
        self.window.remove_controller(self.keys)
        self.window.canvas.set_sensitive(True)
        self.window.canvas.replace_editor(self.window.editor)
        self.window.versions_sheet_closed()

    def _restore(self) -> None:
        version_id = self.selected
        if version_id is None:
            return
        self.close()
        self.window.restore_version(version_id)
        self.window._toast("Version restored. Undo returns to what you had.")

    def _duplicate(self) -> None:
        if self.selected is None:
            return
        snapshot, _assets = self.session.load_version(self.selected)
        document = Document.from_dict(snapshot)
        document.embedded_assets = _assets
        folder = self.session.path.parent if self.session.path else self.session.folder
        from .persistence import next_free_path
        target = next_free_path(folder, f"{self.session.display_name} copy")
        from .documents import DocumentSession
        temporary = DocumentSession(lambda: document, path=None, thumbnailer=None, synchronous=True)
        temporary.copy_to(target, name=target.stem)
        self.close()
        self.window.get_application().open_path(target, parent=self.window)

    def _begin_rename(self) -> None:
        row = self._rows.get(self.selected or "")
        if row is None:
            return
        line = row.get_child()
        text = row.title_label.get_parent()

        def finish(value: str | None) -> bool:
            if value is not None:
                self.session.versions.rename(self.window.document.id, self.selected, value)
                row.title_label.set_label(value.strip() or row.title_label.get_label())
                row.title_label.add_css_class("named")
            if field.get_parent() is text:
                text.remove(field)
            row.title_label.set_visible(True)
            return True

        field = InlineRenameField(row.title_label.get_label(), commit=finish, cancel=lambda: finish(None))
        row.title_label.set_visible(False)
        text.prepend(field)
        field.begin()

    def _delete(self) -> None:
        if self.selected is None:
            return
        self.session.versions.delete(self.window.document.id, self.selected)
        row = self._rows.pop(self.selected)
        self.list.remove(row)
        self.selected = None
        self.window.canvas.replace_editor(self.window.editor)
        self._set_actions(False)
