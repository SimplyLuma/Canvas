# SPDX-License-Identifier: MPL-2.0
"""Opening and importing Figma files: a worker thread, progress, cancel, and
the import report.

The import runs in a child process (`fig.worker`) watched by a thread, so a
large file never freezes the window. A progress dialog appears only if the
import takes longer than a moment, and Cancel ends the child at once. When the document is ready it
opens in a new window (Open, drag and drop) or joins the current document as
new pages (Import…), and a sheet lists everything that was approximated,
missing or left out.
"""

from __future__ import annotations

from dataclasses import asdict
import copy
import gc
from pathlib import Path
import threading
import time
from typing import Any, Callable

from gi.repository import Adw, GLib, Gtk, Pango

from .fig import FigImportError, ImportCancelled, import_fig
from .fig.report import APPROXIMATED, MISSING, UNSUPPORTED, ImportReport
from .model import Document, Page, new_id

FIG_SUFFIX = ".fig"
PROGRESS_DELAY_MS = 300
STAGE_LABELS = {"read": "Reading the file…", "decode": "Reading layers…", "convert": "Building pages…"}
SECTION_TITLES = {UNSUPPORTED: "Left out", MISSING: "Missing", APPROXIMATED: "Different in Canvas"}


def is_figma_file(path: Path) -> bool:
    return path.suffix.lower() == FIG_SUFFIX


class FigImportJob:
    """One import on a worker thread; callbacks arrive on the main loop."""

    def __init__(self, path: Path, on_done: Callable[[Any, FigImportError | None], None],
                 on_progress: Callable[[float, str], None] | None = None) -> None:
        self.path = path
        self.on_done = on_done
        self.on_progress = on_progress
        self.cancel_event = threading.Event()
        self._progress = (0.0, "read")
        self._progress_source = 0
        self._lock = threading.Lock()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="canvas-fig-import", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _run(self) -> None:
        # A full garbage collection over hundreds of thousands of new objects
        # holds the interpreter for a noticeable moment: postpone collection
        # while importing, then freeze what was built so later collections
        # on the main thread skip it.
        collecting = gc.isenabled()
        gc.disable()
        result, error = None, None
        try:
            try:
                from .fig.worker import import_in_process
                result = import_in_process(self.path, progress=self._progressed, cancel=self.cancel_event)
            except OSError:
                # No interpreter to start a child with: import here instead.
                result = import_fig(self.path, progress=self._progressed, cancel=self.cancel_event)
            error = None
        except FigImportError as caught:
            result, error = None, caught
        except (MemoryError, RecursionError) as caught:
            result = None
            error = FigImportError(str(caught), reason="The Figma file is too large or too deeply nested to open.")
        except Exception as caught:  # a bug must not leave the progress dialog up forever
            import traceback
            traceback.print_exc()
            result = None
            error = FigImportError(repr(caught), reason="Canvas couldn’t read this Figma file.")
        finally:
            if error is None and result is not None:
                gc.freeze()
            if collecting:
                gc.enable()
        GLib.idle_add(self._finish, result, error)

    def _progressed(self, fraction: float, stage: str) -> None:
        with self._lock:
            self._progress = (fraction, stage)
            if self._progress_source or self.on_progress is None:
                return
            self._progress_source = GLib.idle_add(self._deliver_progress)
        # No `time.sleep(0)` or other voluntary release here: frequent short
        # releases let this thread win the interpreter lock straight back and
        # starve the main loop. Plain bytecode is preempted fairly.

    def _deliver_progress(self) -> bool:
        with self._lock:
            fraction, stage = self._progress
            self._progress_source = 0
        if self.on_progress is not None and not self.cancel_event.is_set():
            self.on_progress(fraction, stage)
        return False

    def _finish(self, result, error) -> bool:
        self.on_done(result, error)
        return False


class ImportProgress:
    """A dialog with a progress bar and Cancel, shown only for slow imports."""

    def __init__(self, parent: Gtk.Widget | None, path: Path, job: FigImportJob, verb: str = "Opening") -> None:
        self.parent = parent
        self.job = job
        self.dialog = Adw.AlertDialog(heading=f"{verb} “{path.stem}”", body=STAGE_LABELS["read"])
        self.dialog.add_response("cancel", "Cancel")
        self.dialog.set_close_response("cancel")
        self.bar = Gtk.ProgressBar(show_text=False)
        self.bar.set_size_request(280, -1)
        self.bar.update_property([Gtk.AccessibleProperty.LABEL], [f"{verb} {path.stem}"])
        self.dialog.set_extra_child(self.bar)
        self.dialog.connect("response", self._response)
        self._shown = False
        self._finished = False
        self._timer = GLib.timeout_add(PROGRESS_DELAY_MS, self._show)

    def _show(self) -> bool:
        self._timer = 0
        if not self._finished:
            self.dialog.present(self.parent)
            self._shown = True
        return False

    def update(self, fraction: float, stage: str) -> None:
        self.bar.set_fraction(max(0.0, min(1.0, fraction)))
        self.dialog.set_body(STAGE_LABELS.get(stage, STAGE_LABELS["convert"]))

    def _response(self, _dialog, response: str) -> None:
        if response == "cancel" and not self._finished:
            self.job.cancel()

    def finish(self) -> None:
        self._finished = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._shown:
            self.dialog.force_close()


def run_import(path: Path, parent: Gtk.Widget | None,
               on_success: Callable[[Any], None],
               on_failure: Callable[[str], None], *, verb: str = "Opening") -> FigImportJob:
    """Import `path` in the background and call back on the main loop."""
    holder: dict[str, ImportProgress] = {}

    def done(result, error) -> None:
        holder["progress"].finish()
        if error is not None:
            if not isinstance(error, ImportCancelled):
                on_failure(error.reason)
            return
        on_success(result)

    job = FigImportJob(path, done, lambda fraction, stage: holder["progress"].update(fraction, stage))
    holder["progress"] = ImportProgress(parent, path, job, verb)
    job.start()
    return job


# ── fonts ────────────────────────────────────────────────────────────────

def note_missing_fonts(report: ImportReport, document: Document, widget: Gtk.Widget | None = None) -> None:
    """Add the fonts this machine lacks to the report, with what is shown instead."""
    try:
        from .font_picker import family_names
    except (ImportError, ValueError):
        return
    wanted: dict[str, list[str]] = {}
    for item in document.all_objects():
        if item.kind != "text":
            continue
        metadata = item.metadata or {}
        families = [metadata.get("font_family")] + [run.get("font_family") for run in metadata.get("ranges") or []
                                                    if isinstance(run, dict)]
        for family in families:
            if family:
                layers = wanted.setdefault(str(family), [])
                if len(layers) < 12 and item.name not in layers:
                    layers.append(item.name)
    installed = {name.casefold() for name in family_names()}
    context = widget.get_pango_context() if widget is not None else None
    for family, layers in sorted(wanted.items(), key=lambda entry: entry[0].casefold()):
        if family.casefold() in installed:
            continue
        substitute = _substitute(family, context)
        detail = (f"“{family}” isn’t installed. Canvas shows it in “{substitute}” and keeps the font name, "
                  "so the text uses the right font once it’s installed.")
        for layer in layers:
            report.note(f"font:{family}", layer, detail=detail, severity=MISSING,
                        title=f"The font “{family}” isn’t installed")


def _substitute(family: str, context: Pango.Context | None) -> str:
    if context is None:
        return "a similar font"
    description = Pango.FontDescription.from_string(family)
    description.set_family(family)
    font_map = context.get_font_map()
    font = font_map.load_font(context, description) if font_map is not None else None
    if font is None:
        return "a similar font"
    return font.describe().get_family() or "a similar font"


# ── the report sheet ─────────────────────────────────────────────────────

def show_report(parent: Gtk.Widget, report: ImportReport, source: str, *, pages: int, layers: int) -> Adw.Dialog | None:
    findings = report.visible_findings
    if not findings:
        return None
    dialog = Adw.Dialog(title=f"Imported “{source}”", content_width=460, content_height=560)
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    toolbar.add_top_bar(header)
    page_word = "page" if pages == 1 else "pages"
    layer_word = "layer" if layers == 1 else "layers"
    summary = Gtk.Label(
        label=(f"Canvas imported {layers:,} {layer_word} on {pages} {page_word}. "
               "These parts look or work differently than in Figma:"),
        wrap=True, xalign=0)
    summary.add_css_class("canvas-import-summary")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=18,
                  margin_start=18, margin_end=18)
    box.append(summary)
    for severity in (UNSUPPORTED, MISSING, APPROXIMATED):
        group = [finding for finding in findings if finding.severity == severity]
        if not group:
            continue
        heading = Gtk.Label(label=SECTION_TITLES[severity], xalign=0)
        heading.add_css_class("heading")
        box.append(heading)
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listing.add_css_class("boxed-list")
        for finding in group:
            listing.append(_finding_row(finding))
        box.append(listing)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    scroll.set_child(box)
    toolbar.set_content(scroll)
    done = Gtk.Button(label="Done", halign=Gtk.Align.END, margin_top=6, margin_bottom=12, margin_end=18)
    done.add_css_class("suggested-action")
    done.connect("clicked", lambda *_: dialog.close())
    toolbar.add_bottom_bar(done)
    dialog.set_child(toolbar)
    dialog.set_default_widget(done)
    dialog.present(parent)
    return dialog


def _finding_row(finding) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin_top=9, margin_bottom=9,
                  margin_start=12, margin_end=12)
    title = finding.title
    if finding.extra:
        title += f" ({', '.join(sorted(finding.extra))})"
    label = Gtk.Label(label=title, wrap=True, xalign=0)
    row.append(label)
    lines = [finding.detail] if finding.detail else []
    if finding.layers:
        more = finding.count - len(finding.layers)
        names = ", ".join(f"“{name}”" for name in finding.layers)
        lines.append(names + (f" and {more:,} more" if more > 0 and finding.count > len(finding.layers) else ""))
    if lines:
        detail = Gtk.Label(label="\n".join(lines), wrap=True, xalign=0)
        detail.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        row.append(detail)
    wrapper = Gtk.ListBoxRow(activatable=False)
    wrapper.set_child(row)
    return wrapper


# ── into the current document ────────────────────────────────────────────

def merge_pages(window, imported: Document, source: str) -> str | None:
    """Add an imported document's pages after the current one (one undo step)."""
    document = window.document
    document.ensure_pages()
    new_pages: list[Page] = []
    for page in imported.pages:
        objects = imported.objects if page.id == imported.current_page_id else page.objects
        values = [asdict(item) if not isinstance(item, dict) else copy.deepcopy(item) for item in objects]
        new_pages.append(Page(new_id("page"), page.name, page.background, values))
    if not new_pages:
        return None

    # A file's prototype state belongs to its imported pages. Preserve existing
    # variables, and rename collisions in both expressions and bindings.
    existing_values=document.variables.get('Canvas',{}).get('values',{})
    imported_values=imported.variables.get('Canvas',{}).get('values',{})
    variable_names={};used=set(existing_values)
    for name in imported_values:
        candidate=name;number=2
        while candidate in used:
            candidate=f'{source} / {name}' if number==2 else f'{source} / {name} {number}'
            number+=1
        used.add(candidate);variable_names[name]=candidate
    connections=copy.deepcopy(imported.connections)
    def remap_variables(value):
        if isinstance(value,dict):
            if isinstance(value.get('variable'),str):value['variable']=variable_names.get(value['variable'],value['variable'])
            for child in value.values():remap_variables(child)
        elif isinstance(value,list):
            for child in value:remap_variables(child)
    for connection in connections:
        connection.variable=variable_names.get(connection.variable,connection.variable)
        remap_variables(connection.script)
    for page in new_pages:
        for item in page.objects:
            bindings=item.get('metadata',{}).get('variable_bindings')
            if bindings:
                item['metadata']['variable_bindings']={field:variable_names.get(name,name) for field,name in bindings.items()}

    def change() -> None:
        index = document.page_index() + 1
        document.pages[index:index] = new_pages
        known = {asset.id for asset in document.assets}
        document.assets.extend(asset for asset in imported.assets if asset.id not in known)
        document.embedded_assets.update(imported.embedded_assets)
        document.connections.extend(connections)
        if imported_values:
            collection=copy.deepcopy(document.variables.get('Canvas',{}))
            values=copy.deepcopy(collection.get('values',{}))
            values.update({variable_names[name]:copy.deepcopy(value) for name,value in imported_values.items()})
            collection['values']=values;document.variables={**document.variables,'Canvas':collection}
        document.touch()

    if not window.edit(f"Import {source}", change):
        return None
    target=next((page for page in new_pages if any(item.get('metadata',{}).get('prototype_start') for item in page.objects)),new_pages[0])
    canvas = getattr(window, "canvas", None)
    if canvas is not None and hasattr(canvas, "show_page"):
        canvas.show_page(target.id)
    else:
        window.editor.show_page(target.id)
    start=next((item for item in window.document.objects if item.metadata.get("prototype_start")),None)
    window.document.select([start.id] if start else [])
    window._refresh_all()
    return target.id
