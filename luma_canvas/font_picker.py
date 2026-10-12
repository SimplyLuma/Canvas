# SPDX-License-Identifier: MPL-2.0
"""Every installed font, searchable and previewed in its own face.

Families come from Pango's font map, which is fontconfig: the system and user
font folders on a Luma machine, and the host's fonts (`/run/host/fonts`,
`/run/host/user-fonts`) inside the Flatpak. The list is a virtualized
`Gtk.ListView`, so only the rows on screen are laid out in their face, and it
is rebuilt when fontconfig reports new fonts while Canvas runs.

The popover opens with the document's fonts, then recently used fonts, then
every family; typing filters every family by name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Iterable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, GObject, Gtk, Pango, PangoCairo

RECENT_LIMIT = 8

# (weight, italic) -> name, for faces that do not name themselves usefully.
WEIGHT_NAMES = {100: "Thin", 200: "Extra light", 300: "Light", 400: "Regular", 500: "Medium",
                600: "Semibold", 700: "Bold", 800: "Extra bold", 900: "Black", 1000: "Extra black"}

_families_cache: list[Pango.FontFamily] | None = None


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "")
    return (Path(value) if value and Path(value).is_absolute() else Path.home() / ".config") / "luma-canvas"


def font_families(*, refresh: bool = False) -> list[Pango.FontFamily]:
    """Every installed family, sorted by name, from the default font map."""
    global _families_cache
    if _families_cache is None or refresh:
        font_map = PangoCairo.FontMap.get_default()
        if refresh and hasattr(font_map, "changed"):
            font_map.changed()
        _families_cache = sorted(font_map.list_families(), key=lambda family: family.get_name().casefold())
    return _families_cache


def family_names(*, refresh: bool = False) -> list[str]:
    return [family.get_name() for family in font_families(refresh=refresh)]


def find_family(name: str) -> Pango.FontFamily | None:
    folded = name.casefold()
    return next((family for family in font_families() if family.get_name().casefold() == folded), None)


def styles_for(name: str) -> list[tuple[tuple[int, bool], str]]:
    """The family's real faces as ((weight, italic), label), lightest first.

    A variable family offers every named weight its axis covers; a family
    Canvas cannot find offers the standard weights so a document that names a
    missing font can still be edited.
    """
    family = find_family(name)
    choices: dict[tuple[int, bool], str] = {}
    if family is not None:
        for face in family.list_faces():
            if hasattr(face, "is_synthesized") and face.is_synthesized():
                continue
            description = face.describe()
            weight = int(description.get_weight())
            italic = description.get_style() != Pango.Style.NORMAL
            label = face.get_face_name() or WEIGHT_NAMES.get(weight, str(weight))
            choices.setdefault((weight, italic), label)
        if family.is_variable() if hasattr(family, "is_variable") else False:
            italics = {italic for _weight, italic in choices} or {False}
            for weight in range(100, 1000, 100):
                for italic in italics:
                    name_ = WEIGHT_NAMES[weight] + (" italic" if italic else "")
                    choices.setdefault((weight, italic), "Italic" if italic and weight == 400 else name_)
    if not choices:
        for weight in (300, 400, 500, 600, 700, 800):
            choices[(weight, False)] = WEIGHT_NAMES[weight]
            choices[(weight, True)] = "Italic" if weight == 400 else f"{WEIGHT_NAMES[weight]} italic"
    return sorted(choices.items(), key=lambda entry: (entry[0][0], entry[0][1]))


def recent_fonts() -> list[str]:
    try:
        values = json.loads((_config_home() / "recent-fonts.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [value for value in values if isinstance(value, str)][:RECENT_LIMIT]


def remember_font(name: str) -> None:
    names = [name] + [value for value in recent_fonts() if value.casefold() != name.casefold()]
    try:
        folder = _config_home()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "recent-fonts.json").write_text(json.dumps(names[:RECENT_LIMIT]), "utf-8")
    except OSError:
        pass


def missing_fonts(names: Iterable[str]) -> list[str]:
    installed = {name.casefold() for name in family_names()}
    return sorted({name for name in names if name and name.casefold() not in installed}, key=str.casefold)


class FontEntry(GObject.Object):
    __gtype_name__ = "CanvasFontEntry"

    def __init__(self, name: str, *, header: str = "", missing: bool = False) -> None:
        super().__init__()
        self.name = name
        self.header = header
        self.missing = missing


class FontPicker(Gtk.Popover):
    """The font list. `on_chosen(name)` is called when a family is picked."""

    def __init__(self, current: str, document_fonts: Iterable[str], on_chosen: Callable[[str], None]) -> None:
        super().__init__(position=Gtk.PositionType.LEFT)
        self.add_css_class("canvas-font-picker")
        self.on_chosen = on_chosen
        self.current = current
        self.document_fonts = sorted({name for name in document_fonts if name}, key=str.casefold)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(264, 420)
        self.search = Gtk.SearchEntry(placeholder_text="Search fonts")
        self.search.add_css_class("canvas-font-search")
        self.search.connect("search-changed", lambda *_: self.rebuild())
        self.search.connect("activate", lambda *_: self._activate_first())
        box.append(self.search)
        self.store = Gio_ListStore()
        self.selection = Gtk.SingleSelection(model=self.store, autoselect=False, can_unselect=True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        self.list = Gtk.ListView(model=self.selection, factory=factory, single_click_activate=True)
        self.list.add_css_class("canvas-font-list")
        self.list.connect("activate", self._activated)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_child(self.list)
        box.append(scroll)
        self.count = Gtk.Label(xalign=0)
        self.count.add_css_class("canvas-row-meta")
        box.append(self.count)
        self.set_child(box)
        self.rebuild()
        self.connect("map", lambda *_: self.search.grab_focus())

    def all_names(self) -> list[str]:
        return family_names()

    def rebuild(self) -> None:
        query = self.search.get_text().strip().casefold()
        names = self.all_names()
        entries: list[FontEntry] = []
        if query:
            entries = [FontEntry(name) for name in names if query in name.casefold()]
            self.count.set_label(f"{len(entries)} of {len(names)} fonts")
        else:
            installed = {name.casefold() for name in names}
            if self.document_fonts:
                for index, name in enumerate(self.document_fonts):
                    entries.append(FontEntry(name, header="In this document" if index == 0 else "",
                                             missing=name.casefold() not in installed))
            recent = [name for name in recent_fonts() if name.casefold() in installed]
            for index, name in enumerate(recent):
                entries.append(FontEntry(name, header="Recent" if index == 0 else ""))
            for index, name in enumerate(names):
                entries.append(FontEntry(name, header="All fonts" if index == 0 else ""))
            self.count.set_label(f"{len(names)} fonts")
        self.store.remove_all()
        self.store.splice(0, 0, entries)
        for index, entry in enumerate(entries):
            if entry.name == self.current and not entry.header and (query or index >= len(self.document_fonts)):
                self.selection.set_selected(index)
                GLib.idle_add(lambda position=index: (self.list.scroll_to(position, Gtk.ListScrollFlags.NONE, None), False)[1])
                break

    def _setup(self, _factory, item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header = Gtk.Label(xalign=0)
        header.add_css_class("canvas-group-label")
        box.append(header)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.add_css_class("canvas-font-row")
        name = Gtk.Label(xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        row.append(name)
        warning = Gtk.Label(label="Missing")
        warning.add_css_class("canvas-row-meta")
        row.append(warning)
        box.append(row)
        item.set_child(box)
        item.canvas_parts = (header, name, warning)

    def _bind(self, _factory, item) -> None:
        entry: FontEntry = item.get_item()
        header, name, warning = item.canvas_parts
        header.set_label(entry.header)
        header.set_visible(bool(entry.header))
        name.set_label(entry.name)
        # The name in its own face, laid out only when the row is on screen.
        attributes = Pango.AttrList()
        if not entry.missing:
            attributes.insert(Pango.attr_family_new(entry.name))
        name.set_attributes(attributes)
        warning.set_visible(entry.missing)

    def _activated(self, _list, position: int) -> None:
        entry = self.store.get_item(position)
        if entry is None:
            return
        remember_font(entry.name)
        self.popdown()
        self.on_chosen(entry.name)

    def _activate_first(self) -> None:
        if self.store.get_n_items():
            self._activated(self.list, 0)


def Gio_ListStore():
    from gi.repository import Gio
    return Gio.ListStore(item_type=FontEntry)


def watch_font_changes(callback: Callable[[], None]) -> None:
    """Call `callback` when fontconfig reports installed or removed fonts."""
    settings = Gtk.Settings.get_default()
    if settings is None:
        return

    def changed(*_args) -> None:
        font_families(refresh=True)
        callback()
    settings.connect("notify::gtk-fontconfig-timestamp", changed)
