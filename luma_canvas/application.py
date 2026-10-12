# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("LumaUI", "1")
gi.require_version("LumaSemantics", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from .documents import recover
from .journal import Journal
from .persistence import DocumentStore, PackageError, state_home, write_package
from .window import APP_ID, APP_NAME, CanvasWindow
from luma_appkit import Toast

SESSION_FILE = "session.json"
RECENT_FILE = "recent.json"


# How long a document may take to read before Canvas says it is working on it.
OPEN_PROGRESS_DELAY_MS = 350


class _OpenProgress:
    """A quiet dialog with a pulsing bar, shown only for a slow read."""

    def __init__(self, parent: Gtk.Window | None, path: Path) -> None:
        self.dialog = Adw.AlertDialog(heading=f"Opening “{path.stem}”", body="Reading the document…")
        self.bar = Gtk.ProgressBar(show_text=False)
        self.bar.set_size_request(280, -1)
        self.bar.update_property([Gtk.AccessibleProperty.LABEL], [f"Opening {path.stem}"])
        self.dialog.set_extra_child(self.bar)
        self.parent = parent
        self._shown = False
        self._finished = False
        self._pulse = 0
        self._timer = GLib.timeout_add(OPEN_PROGRESS_DELAY_MS, self._show)

    def _show(self) -> bool:
        self._timer = 0
        if self._finished:
            return False
        self.dialog.present(self.parent)
        self._shown = True
        self._pulse = GLib.timeout_add(90, self._step)
        return False

    def _step(self) -> bool:
        if self._finished:
            self._pulse = 0
            return False
        self.bar.pulse()
        return True

    def finish(self) -> None:
        self._finished = True
        for source in ("_timer", "_pulse"):
            handle = getattr(self, source)
            if handle:
                GLib.source_remove(handle)
                setattr(self, source, 0)
        if self._shown:
            self.dialog.force_close()


class CanvasApplication(Adw.Application):
    """Documents save themselves: quitting, closing and session end flush every
    window, a crash is recovered from the journal at the next launch, and a file
    that cannot be opened says why instead of becoming an empty document."""

    def __init__(self) -> None:
        fixture_path = os.environ.get("LUMA_CANVAS_FIXTURE")
        fixture_home = None
        fixture_env_before: dict[str, str | None] = {}
        if fixture_path:
            # Keep AppWindow geometry, recent files, caches and GSettings away
            # from the normal profile even when a fixture is launched manually.
            fixture_home = tempfile.TemporaryDirectory(prefix="luma-canvas-fixture-")
            for key, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
                target = Path(fixture_home.name) / folder
                target.mkdir()
                fixture_env_before[key] = os.environ.get(key)
                os.environ[key] = str(target)
            fixture_env_before["GSETTINGS_BACKEND"] = os.environ.get("GSETTINGS_BACKEND")
            os.environ["GSETTINGS_BACKEND"] = "memory"
        application_id = (APP_ID + ".LumaUIPreview"
                          if os.environ.get("LUMA_CANVAS_PREVIEW") == "1" else APP_ID)
        super().__init__(application_id=application_id, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self._fixture_home = fixture_home
        self._fixture_env_before = fixture_env_before
        self._fixture_path = fixture_path
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._quit)
        self.add_action(quit_action)
        open_recent = Gio.SimpleAction.new("open-recent", GLib.VariantType.new("s"))
        open_recent.connect("activate", lambda _action, value: self.open_path(Path(value.get_string())))
        self.add_action(open_recent)
        self._recovered = False
        self._quitting = False
        # Session end: GTK asks before the session logs out; flush first.
        if hasattr(self, "set_register_session"):
            try:
                self.set_register_session(True)
            except TypeError:
                pass
        try:
            self.connect("query-end", lambda *_: self.flush_all())
        except TypeError:
            pass

    # ── lifecycle ───────────────────────────────────────────────────────

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._terminated)

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is not None:
            window.present()
            return
        if self._fixture_path:
            from .fixture import load_fixture

            assets = os.environ.get("LUMA_CANVAS_FIXTURE_ASSETS")
            if not assets:
                raise RuntimeError("LUMA_CANVAS_FIXTURE_ASSETS is required in fixture mode")
            document = load_fixture(Path(self._fixture_path), assets_dir=Path(assets))
            CanvasWindow(self, document=document, show_welcome=False, fixture_mode=True).present()
            return
        if self._recover_documents():
            return
        if self._reopen_session():
            return
        CanvasWindow(self).present()

    def do_open(self, files, count: int, hint: str) -> None:
        if self._fixture_path:
            self.do_activate()
            return
        self._recover_documents()
        opened = False
        for file in files[:count] if count else files:
            if file.get_path():
                opened = self.open_path(Path(file.get_path())) or opened
        if not opened and not self.get_windows():
            self.quit()

    def do_shutdown(self) -> None:
        if not self._fixture_path:
            self.flush_all()
        Adw.Application.do_shutdown(self)
        if self._fixture_home is not None:
            self._fixture_home.cleanup()
            for key, value in self._fixture_env_before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _terminated(self) -> bool:
        self.flush_all()
        self.quit()
        return GLib.SOURCE_REMOVE

    def flush_all(self, timeout: float = 5.0) -> bool:
        """Write every open document now. Returns whether all are on disk."""
        if self._fixture_path:
            return True
        ok = True
        self._remember_session()
        for window in list(self.get_windows()):
            if isinstance(window, CanvasWindow):
                ok = window.flush(timeout) and ok
        return ok

    def _quit(self, *_args) -> None:
        self._quitting = True
        self._remember_session()
        for window in list(self.get_windows()):
            window.close()
        if not self.get_windows():
            self.quit()

    # ── opening ─────────────────────────────────────────────────────────

    def new_window(self) -> CanvasWindow:
        window = CanvasWindow(self, show_welcome=False, fixture_mode=bool(self._fixture_path))
        window.present()
        return window

    def window_for(self, path: Path) -> CanvasWindow | None:
        for window in self.get_windows():
            session = getattr(window, "session", None)
            if session is not None and session.path is not None and _same_file(session.path, path):
                return window
        return None

    def open_path(self, path: Path, *, parent: Gtk.Window | None = None,
                  reuse: CanvasWindow | None = None) -> bool:
        """Open a document, or focus the window already showing it.

        A file that cannot be read shows an alert with the reason and opens
        nothing: never an empty document in its place.
        """
        if self._fixture_path:
            return False
        existing = self.window_for(path)
        if existing is not None:
            existing.present()
            return True
        if path.suffix.lower() == ".fig":
            return self._open_figma(path, parent=parent, reuse=reuse)
        if path.suffix.lower() != ".svg":
            return self._open_package(path, parent=parent, reuse=reuse)
        from .importers import import_svg

        try:
            document = import_svg(path)
        except PackageError as error:
            self._open_failed(path, error.reason, parent)
            return False
        except (OSError, ValueError) as error:
            reason = "The file is damaged." if isinstance(error, ValueError) else "Canvas couldn’t read it."
            self._open_failed(path, reason, parent)
            return False
        # An SVG opens as a new untitled document.
        if reuse is not None:
            reuse.replace_document(document, None)
            reuse.session.pristine = True
            return True
        window = CanvasWindow(self, None, document=document, show_welcome=False)
        window.present()
        return True

    def _open_package(self, path: Path, *, parent: Gtk.Window | None,
                      reuse: CanvasWindow | None) -> bool:
        """Read a Canvas package on a worker thread and show it when it is
        read, so opening a document of tens of thousands of layers never
        freezes the window that asked for it."""
        import threading

        state: dict[str, Any] = {}
        progress = _OpenProgress(parent or self.get_active_window(), path)
        self.hold()

        def finished() -> bool:
            progress.finish()
            self.release()
            reason = state.get("error")
            if reason is not None:
                self._open_failed(path, reason, parent)
                return False
            document, snapshot = state["document"], state["snapshot"]
            self.remember_recent(path)
            if reuse is not None:
                reuse.replace_document(document, path, snapshot=snapshot)
            else:
                window = CanvasWindow(self, path, document=document, show_welcome=False, snapshot=snapshot)
                window.present()
            return False

        def read() -> None:
            try:
                document, snapshot = DocumentStore.load_with_snapshot(path)
                state["document"], state["snapshot"] = document, snapshot
            except PackageError as error:
                state["error"] = error.reason
            except (OSError, ValueError) as error:
                state["error"] = ("The file is damaged." if isinstance(error, ValueError)
                                  else "Canvas couldn’t read it.")
            GLib.idle_add(finished)

        threading.Thread(target=read, name="canvas-open", daemon=True).start()
        return True

    def _open_figma(self, path: Path, *, parent: Gtk.Window | None = None,
                    reuse: CanvasWindow | None = None) -> bool:
        """Import a Figma file on a worker thread into a new Canvas document.

        The `.fig` is never written to: the document is saved as a Canvas
        document in the Canvas documents folder, named after the file, and
        an import report lists what came across differently.
        """
        from .fig_import_ui import note_missing_fonts, run_import, show_report

        self.hold()

        def succeeded(result) -> None:
            self.release()
            document = result.document
            if reuse is not None:
                reuse.replace_document(document, None, opened=False)
                window = reuse
            else:
                window = CanvasWindow(self, None, document=document, show_welcome=False)
                window.present()
            if hasattr(window.session, "adopt_imported"):
                window.session.adopt_imported(path.stem)
            start=next((item for item in document.all_objects() if item.metadata.get('prototype_start')),None)
            if start:
                for page in document.pages:
                    if any(item['id']==start.id for item in page.objects) or (page.id==document.current_page_id and document.object(start.id)):
                        window.canvas.show_page(page.id);break
                document.select([start.id])
                GLib.idle_add(lambda:(window.canvas.zoom_to_selection(),False)[1])
            window._refresh_all()
            note_missing_fonts(result.report, document, window)
            show_report(window, result.report, path.name, pages=len(document.pages),
                        layers=int(result.report.stats.get("layers", len(document.objects))))

        def failed(reason: str) -> None:
            self.release()
            self._open_failed(path, reason, parent)

        run_import(path, parent or self.get_active_window(), succeeded, failed)
        return True

    # ── recent documents ────────────────────────────────────────────────

    def recent_documents(self) -> list[Path]:
        """The last ten documents opened or saved, newest first, that still exist."""
        try:
            values = json.loads((state_home() / RECENT_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            return []
        return [Path(value) for value in values if isinstance(value, str) and Path(value).exists()][:10]

    def remember_recent(self, path: Path) -> None:
        paths = [str(path)] + [str(item) for item in self.recent_documents() if not _same_file(item, path)]
        try:
            target = state_home() / RECENT_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(paths[:10]), "utf-8")
        except OSError:
            return
        from . import app_menu
        self.set_menubar(app_menu.build(self.recent_documents()))

    def _open_failed(self, path: Path, reason: str, parent: Gtk.Window | None) -> None:
        dialog = Adw.AlertDialog(heading=f"Couldn’t open “{path.name}”", body=reason)
        dialog.add_response("close", "Close")
        dialog.set_close_response("close")
        self.hold()
        dialog.connect("response", lambda *_: self.release())
        dialog.present(parent or self.get_active_window())

    # ── recovery and reopening ──────────────────────────────────────────

    def _recover_documents(self) -> bool:
        if self._recovered:
            return False
        self._recovered = True
        try:
            found = recover(Journal(state_home() / "journal"), DocumentStore())
        except OSError:
            return False
        for item in found:
            CanvasWindow(self, recovered=item, show_welcome=False).present()
        return bool(found)

    def _session_path(self) -> Path:
        return state_home() / SESSION_FILE

    def _remember_session(self) -> None:
        if self._fixture_path:
            return
        paths = [str(window.session.path) for window in self.get_windows()
                 if isinstance(window, CanvasWindow) and window.session.path is not None
                 and not window.session.pristine]
        try:
            target = self._session_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"documents": paths}), "utf-8")
        except OSError:
            pass

    def _reopen_session(self) -> bool:
        settings_source = Gio.SettingsSchemaSource.get_default()
        schema = settings_source.lookup(APP_ID, True) if settings_source else None
        if schema is not None and schema.has_key("reopen-documents") and \
                not Gio.Settings.new(APP_ID).get_boolean("reopen-documents"):
            return False
        try:
            paths = json.loads(self._session_path().read_text("utf-8")).get("documents", [])
        except (OSError, ValueError):
            return False
        opened = False
        for value in paths:
            path = Path(value)
            if path.exists():
                opened = self.open_path(path) or opened
        return opened

    def announce_trashed(self, path: Path, snapshot: dict, assets: dict) -> None:
        """Moved to Trash, with Undo in whatever window is left."""
        window = next((item for item in self.get_windows() if isinstance(item, CanvasWindow)), None)
        if window is None:
            return
        def undo() -> None:
            if not path.exists():
                write_package(snapshot, assets, path)
            self.open_path(path)
        Toast.show(window.center_pane, f"Moved “{path.stem}” to Trash", kind="deleted", undo=undo)

    def _about(self, *_args) -> None:
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Simply Luma",
            version="0.1.0",
            comments="Visual design and interactive prototyping with Luma UI.",
            license_type=Gtk.License.CUSTOM,
            license="Canvas includes Apache-2.0 and MPL-2.0 software. The bundled toolkit also retains LGPL-2.1-or-later material. Artwork, icons and fonts keep their separate licenses. See https://github.com/SimplyLuma/Canvas/blob/main/THIRD_PARTY_NOTICES.md for complete notices and license texts.",
            website="https://github.com/SimplyLuma/Canvas",
        )
        dialog.present(self.get_active_window())


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return first == second


def main() -> int:
    return CanvasApplication().run(sys.argv)
