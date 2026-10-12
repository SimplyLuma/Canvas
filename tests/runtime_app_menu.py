# SPDX-License-Identifier: MPL-2.0
"""Every identity-menu row triggers its command, and rows that do not apply are disabled."""
from __future__ import annotations

import sys
import time
import traceback

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from luma_canvas import app_menu
from luma_canvas.application import CanvasApplication
from luma_canvas.editing import Editor
from luma_canvas.model import Document
from luma_canvas.window import CanvasWindow

errors: list[str] = []
sys.excepthook = lambda *info: errors.append("".join(traceback.format_exception(*info)))


def settle(seconds: float = 0.3) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(0.01)


app = CanvasApplication()
assert app.register(None)
window = CanvasWindow(app, show_welcome=False)
window.present()
settle(1)
window.editor = Editor(Document.demonstration())
window._refresh_all()
window.document.select([])
window._selection_changed()
settle()

action = lambda command_id: app.lookup_action(command_id.replace(".", "-"))
for command_id in ("arrange.align-left", "edit.copy", "path.union", "arrange.ungroup"):
    assert not action(command_id).get_enabled(), f"{command_id} should be disabled with nothing selected"
print("PASS: arrange, copy and shape rows are disabled with nothing selected")

names = ["Accent", "Plate", "Rule"]
ids = [item.id for item in window.document.objects if item.name in names]
window.document.select(ids)
window._selection_changed()
settle()
for command_id in ("arrange.align-left", "arrange.distribute-x", "edit.copy", "path.union", "arrange.group"):
    assert action(command_id).get_enabled(), f"{command_id} should be enabled with three layers selected"
print("PASS: rows enable with a selection")

# Rows that open a chooser, a dialog or quit are checked for existence only.
interactive = {"document.open", "document.export", "document.save-copy", "document.move", "document.trash",
               "application.quit", "application.about", "application.shortcuts", "document.reveal",
               "document.duplicate", "document.browse-versions", "document.rename", "view.commands"}
ran = 0
for command_id in app_menu.command_ids():
    assert action(command_id) is not None, f"no action for {command_id}"
    if command_id in interactive or command_id == "document.new":
        continue
    if not action(command_id).get_enabled():
        continue
    before = len(window.editor.undo_stack)
    app.activate_action(command_id.replace(".", "-"), None)
    settle(0.05)
    ran += 1
    if not window.document.selected_ids:
        window.document.select([item.id for item in window.document.objects if item.name in names])
        window._selection_changed()
print(f"PASS: {ran} menu commands ran; every row has an action")
for toggle in ("pixel-grid", "pixel-snap", "show-grid", "outline"):
    state = window.lookup_action(toggle).get_state().get_boolean()
    window.activate_action(f"win.{toggle}", None) if False else window.lookup_action(toggle).change_state(GLib.Variant.new_boolean(not state))
    assert window.lookup_action(toggle).get_state().get_boolean() is (not state), toggle
print("PASS: view toggles change state")
assert not errors, errors[0]
window.destroy()
print("PASS: no exceptions")
