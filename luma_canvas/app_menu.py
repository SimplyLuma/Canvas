# SPDX-License-Identifier: MPL-2.0
"""Canvas's identity menu, nested so it stays short.

AppKit's `command_menu_model` turns the command registry into one flat menu
with a section per group, which for Canvas ran past forty rows. The registry
still owns every command (actions, shortcuts, command search); this module
only arranges those commands into GMenu submenus, which the toolkit's menu
contract draws like every other Luma menu.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from gi.repository import Gio, GLib

# (label, command id) rows, `None` for a section break, or (label, [rows]) for a submenu.
Row = tuple


# Shortcuts the canvas handles itself (so text fields keep their own Ctrl+C,
# Ctrl+V and Ctrl+A). They are shown in the menu, not registered as
# application accelerators.
DISPLAY_ACCELS = {
    "edit.cut": "<Control>x", "edit.copy": "<Control>c", "edit.paste": "<Control>v",
    "edit.paste-over": "<Shift><Control>v", "edit.select-all": "<Control>a", "edit.delete": "Delete",
    "view.zoom-fit": "<Shift>1", "view.zoom-selection": "<Shift>2",
}


def action_for(command_id: str) -> str:
    return "app." + command_id.replace(".", "-")


TOP: Sequence = (
    ("New document", "document.new"),
    ("Open…", "document.open"),
    ("Browse files…", "document.browse-files"),
    ("Import…", "document.import"),
    None,
    ("Save a version", "document.save-version"),
    ("Export…", "document.export"),
    None,
    ("Document", (
        ("Rename…", "document.rename"),
        ("Move…", "document.move"),
        ("Duplicate", "document.duplicate"),
        ("Save a copy…", "document.save-copy"),
        None,
        ("Show in Filer", "document.reveal"),
        ("Move to Trash", "document.trash"),
    )),
    ("Versions", (
        ("Browse versions…", "document.browse-versions"),
        None,
        ("Revert to last saved version", "document.revert-last-saved"),
        ("Revert to last opened", "document.revert-last-opened"),
    )),
    None,
    ("Edit", (
        ("Undo", "edit.undo"),
        ("Redo", "edit.redo"),
        None,
        ("Cut", "edit.cut"),
        ("Copy", "edit.copy"),
        ("Paste", "edit.paste"),
        ("Paste over selection", "edit.paste-over"),
        None,
        ("Duplicate", "edit.duplicate"),
        ("Delete", "edit.delete"),
        ("Rename layer", "edit.rename-layer"),
        ("Select all", "edit.select-all"),
    )),
    ("Arrange", (
        ("Align", (
            ("Left", "arrange.align-left"),
            ("Horizontal centers", "arrange.align-center-x"),
            ("Right", "arrange.align-right"),
            None,
            ("Top", "arrange.align-top"),
            ("Vertical centers", "arrange.align-center-y"),
            ("Bottom", "arrange.align-bottom"),
        )),
        ("Distribute", (
            ("Horizontal spacing", "arrange.distribute-x"),
            ("Vertical spacing", "arrange.distribute-y"),
        )),
        None,
        ("Group selection", "arrange.group"),
        ("Ungroup", "arrange.ungroup"),
        ("Frame selection", "arrange.frame"),
        None,
        ("Bring forward", "arrange.forward"),
        ("Send backward", "arrange.backward"),
        ("Bring to front", "arrange.front"),
        ("Send to back", "arrange.back"),
    )),
    ("Shape", (
        ("Union", "path.union"),
        ("Subtract", "path.subtract"),
        ("Intersect", "path.intersect"),
        ("Exclude", "path.exclude"),
        None,
        ("Outline stroke", "path.outline-stroke"),
    )),
    ("View", (
        ("Zoom in", "view.zoom-in"),
        ("Zoom out", "view.zoom-out"),
        ("Zoom to 100%", "view.zoom-100"),
        ("Zoom to fit", "view.zoom-fit"),
        ("Zoom to selection", "view.zoom-selection"),
        None,
        ("Pixel grid", "win.pixel-grid"),
        ("Snap to pixel grid", "win.pixel-snap"),
        ("Show grid", "win.show-grid"),
        ("Outline view", "win.outline"),
        None,
        ("Show or hide panels", "view.toggle-panels"),
        ("Search commands", "view.commands"),
    )),
    None,
    ("Keyboard shortcuts", "application.shortcuts"),
    ("About Canvas", "application.about"),
    ("Quit Canvas", "application.quit"),
)


def command_ids(rows: Sequence = TOP) -> list[str]:
    """Every registry command the menu points at (for tests)."""
    found: list[str] = []
    for row in rows:
        if row is None:
            continue
        label, target = row
        if isinstance(target, tuple):
            found += command_ids(target)
        elif not target.startswith(("@", "win.")):
            found.append(target)
    return found


ICONS = {
    "New document": "file-text", "Open…": "folder-open", "Open recent": "history",
    "Import…": "download", "Browse files…": "folder-open", "Save a version": "archive", "Export…": "share",
    "Document": "file-text", "Versions": "history", "Edit": "pencil", "Arrange": "layers",
    "Shape": "square", "View": "eye", "Keyboard shortcuts": "keyboard",
    "About Canvas": "info", "Quit Canvas": "log-out", "Undo": "undo", "Redo": "redo",
    "Cut": "scissors", "Copy": "copy", "Paste": "clipboard", "Duplicate": "copy-plus",
    "Delete": "trash-2", "Rename…": "pencil", "Rename layer": "pencil",
    "Move…": "folder", "Save a copy…": "archive", "Show in Filer": "folder-open",
    "Move to Trash": "trash-2", "Browse versions…": "history", "Align": "align-left",
    "Distribute": "align-left", "Select all": "square-dashed",
}


def _icon(item: Gio.MenuItem, label: str) -> None:
    # Use the same symbolic icon family as the other Luma applications.
    name = ICONS.get(label, "square" if label in {"Union", "Subtract", "Intersect", "Exclude"}
                     else "layers" if "roup" in label or "front" in label or "back" in label
                     else "zoom-in" if "Zoom" in label else "settings-2")
    item.set_icon(Gio.ThemedIcon.new(f"lumaui-{name}-symbolic"))


def _menu(rows: Sequence, recent: Sequence[Path]) -> Gio.Menu:
    menu = Gio.Menu()
    section = Gio.Menu()
    for row in rows:
        if row is None:
            if section.get_n_items():
                menu.append_section(None, section)
            section = Gio.Menu()
            continue
        label, target = row
        if isinstance(target, tuple):
            item = Gio.MenuItem.new_submenu(label, _menu(target, recent))
            _icon(item, label)
            section.append_item(item)
        elif target == "@recent":
            item = Gio.MenuItem.new_submenu(label, recent_menu(recent))
            _icon(item, label)
            section.append_item(item)
        else:
            action = target if target.startswith("win.") else action_for(target)
            item = Gio.MenuItem.new(label, action)
            _icon(item, label)
            if target in DISPLAY_ACCELS:
                item.set_attribute_value("accel", GLib.Variant.new_string(DISPLAY_ACCELS[target]))
            section.append_item(item)
    if section.get_n_items():
        menu.append_section(None, section)
    return menu


def recent_menu(recent: Sequence[Path]) -> Gio.Menu:
    menu = Gio.Menu()
    if not recent:
        # No action: the row is drawn insensitive, saying why the list is empty.
        item = Gio.MenuItem.new("No recent documents", None)
        _icon(item, "Open recent")
        menu.append_item(item)
        return menu
    for path in recent:
        item = Gio.MenuItem.new(path.stem, None)
        _icon(item, "Document")
        item.set_action_and_target_value("app.open-recent", GLib.Variant.new_string(str(path)))
        menu.append_item(item)
    return menu


def build(recent: Iterable[Path] = ()) -> Gio.Menu:
    return _menu(TOP, list(recent))
