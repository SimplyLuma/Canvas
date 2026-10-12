# SPDX-License-Identifier: Apache-2.0
"""Exercise the real welcome widget, commands and persistent opt-out."""
import tempfile
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk
from luma_canvas.application import CanvasApplication
from luma_canvas.window import APP_ID, CanvasWindow
from luma_canvas.model import Document
from luma_canvas.persistence import DocumentStore


def settle(predicate=lambda: True):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Canvas did not settle')


def descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


app = CanvasApplication()
assert app.register(None)
settings = Gio.Settings.new(APP_ID)
# Reproduce the installed bug: the editor was deliberately maximized, but
# that preference must not maximize a fresh welcome surface.
state = Path(os.environ['XDG_STATE_HOME']) / 'luma/windows' / f'{APP_ID}.json'
state.parent.mkdir(parents=True, exist_ok=True)
editor_geometry = {'width': 1440, 'height': 836, 'maximized': True}
state.write_text(json.dumps(editor_geometry))
settings.set_boolean('show-welcome', True)
first = CanvasWindow(app)
maximized = []
first.maximize = lambda: maximized.append(True)
first.present(); settle(lambda: first.get_allocated_width() == 900)
assert first.get_allocated_height() == 560 and not maximized
assert json.loads(state.read_text()) == editor_geometry
first.welcome.get_new_button().emit('clicked'); settle(lambda: bool(maximized))
assert first.get_default_size() == (1440, 836)
assert first.pages.get_visible_child_name() == 'editor'
first.destroy(); settle()
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / 'Direct.canvas'
    DocumentStore.save(Document.empty(), path)
    direct = CanvasWindow(app, path)
    direct.maximize = lambda: maximized.append(True)
    settle(lambda: len(maximized) == 2)
    assert not direct._welcome_active and direct.get_default_size() == (1440, 836)
    direct.destroy(); settle()
state.unlink()
state.with_name(f'{APP_ID}.welcome.json').unlink()
print('PASS: Canvas welcome900×560 despite saved maximized editor; New and direct-file open restore editor preferences')

for scheme in (Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.FORCE_DARK):
    Adw.StyleManager.get_default().set_color_scheme(scheme)
    for width in (360, 500, 900, 1440):
        settings.set_boolean('show-welcome', True)
        window = CanvasWindow(app)
        window.set_default_size(width, 700)
        window.present()
        settle(lambda: window.welcome.get_width() > 0)
        assert window.pages.get_visible_child_name() == 'welcome'
        assert window.get_allocated_width() == width, (width, window.get_allocated_width())
        assert [item.name for item in window.document.objects] == ['Frame 1'], 'A new document starts with one frame'
        window.welcome.get_new_button().emit('clicked')
        settle()
        assert window.pages.get_visible_child_name() == 'editor'
        assert [item.name for item in window.document.objects] == ['Frame 1']
        assert len(app.get_windows()) == 1, 'New must reuse the welcome window'
        window.destroy()
        settle()

with tempfile.TemporaryDirectory() as root:
    path = Path(root) / 'Existing.canvas'
    document = Document.empty()
    document.name = 'Existing'
    DocumentStore.save(document, path)
    window = CanvasWindow(app)
    # Exercise the chooser completion path using a real saved document.
    window._open_finished(SimpleNamespace(open_finish=lambda _: Gio.File.new_for_path(str(path))), None)
    # Documents are read on a worker thread; the window shows it when it lands.
    settle(lambda: window.path == path)
    assert window.path == path and window.document.name == 'Existing'
    assert window.pages.get_visible_child_name() == 'editor'
    window.destroy()
    opened = CanvasWindow(app, path)
    assert not opened._welcome_active and opened.document.name == 'Existing'
    opened.destroy()

window = CanvasWindow(app)
check = next(child for child in descendants(window.welcome) if isinstance(child, Gtk.CheckButton))
check.set_active(False)
assert not settings.get_boolean('show-welcome')
window.destroy()
window = CanvasWindow(app)
assert not window._welcome_active and len(window.document.objects) == 1
window.destroy()
print('PASS: Canvas welcome widths/themes, New, real Open completion, file activation and opt-out')

# Closing never asks "Save changes?": an edited document is flushed to disk.
settings.set_boolean('show-welcome', True)
with tempfile.TemporaryDirectory() as root:
    os.environ['XDG_DOCUMENTS_DIR'] = root
    window = CanvasWindow(app)
    window._new()
    window.session.folder = Path(root)
    window.present(); settle()
    window.edit('Create', lambda: window.editor.create('rectangle', 10, 10, 40, 40))
    assert window.session.path == Path(root) / 'Untitled.canvas'
    assert not window._close_requested(window), 'closing asked a question'
    assert (Path(root) / 'Untitled.canvas').is_file()
    window.destroy(); settle()
print('PASS: Canvas closes an edited untitled document without a dialog and keeps its file')
