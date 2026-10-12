# SPDX-License-Identifier: MPL-2.0
"""Clipping frames never break GSK clipping on the GPU renderers.

GTK 4.22's GPU node processor leaks a clip when a clip node fails its
occlusion pass; later layers vanish and GSK logs "Clipping is broken" (seen on
Intel GPUs). Canvas clips frames with a construct that pass never tries (see
`scene.append_clipped`). This drives the real window through the case that
reproduced it on lavapipe and fails on any such warning.

    GSK_RENDERER=vulkan python3 tests/runtime_gpu_clip.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

capture = tempfile.TemporaryFile()
saved_stderr = os.dup(2)
os.dup2(capture.fileno(), 2)

from luma_canvas.application import CanvasApplication  # noqa: E402
from luma_canvas.editing import Editor  # noqa: E402
from luma_canvas.model import Document, LayoutObject, new_id  # noqa: E402


def report(text: str) -> None:
    os.write(saved_stderr, (text + "\n").encode())


def warnings() -> int:
    capture.seek(0)
    return capture.read().decode(errors="replace").count("Clipping is broken")


def pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def layer(kind, name, x, y, w, h, parent=None, **fields):
    return LayoutObject(new_id(kind), kind, name, parent, x, y, w, h, **fields)


def document() -> Document:
    doc = Document.empty("Clip")
    frame = layer("frame", "Frame", 0, 0, 400, 300, fill="#ffffff", stroke="#2060e0", stroke_width=6,
                  stroke_align="center")
    nested = layer("frame", "Nested", -60, 200, 140, 140, frame.id, fill="#e02080", stroke="#e02080",
                   stroke_width=12, stroke_align="outside", clip_content=False)
    doc.objects += [
        frame,
        layer("rectangle", "Outside stroke", 330, 20, 120, 60, frame.id, fill="#e02080", stroke="#e02080",
              stroke_width=16, stroke_align="outside"),
        layer("rectangle", "Center stroke", -40, 120, 100, 60, frame.id, fill="#e02080", stroke="#e02080",
              stroke_width=16),
        layer("ellipse", "Inside and shadow", 150, 250, 100, 100, frame.id, fill="#e02080", stroke="#e02080",
              stroke_width=8, stroke_align="inside",
              effects=[{"type": "drop_shadow", "x": 0, "y": 30, "blur": 0, "spread": 0, "color": "#e02080",
                        "visible": True}]),
        layer("text", "Text", 250, -30, 300, 60, frame.id, fill="#e02080", text="CLIPPED TEXT",
              metadata={"font_size": 48, "font_weight": 700}),
        nested,
        layer("rectangle", "Nested child", -90, 230, 60, 60, nested.id, fill="#e02080"),
        layer("frame", "Rounded", 480, 0, 300, 300, fill="#ffffff", radius=32),
    ]
    return doc


application = CanvasApplication()
application.set_flags(application.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
status = {"code": 2}


def run() -> bool:
    windows = [window for window in application.get_windows() if hasattr(window, "canvas")]
    if not windows:
        return True
    window = windows[0]
    window.set_default_size(1400, 900)
    pump(1.0)
    for _round in range(4):
        window.editor = Editor(document())
        window.canvas.replace_editor(window.editor)
        if hasattr(window, "_reveal_editor"):
            window._reveal_editor()
        window._refresh_all()
        pump(0.4)
        for zoom, selected in ((1.0, False), (1.37, True), (0.8, True), (1.37, False)):
            doc = window.document
            doc.zoom, doc.canvas_x, doc.canvas_y = zoom, 200.3, 150.6
            doc.select([doc.objects[0].id] if selected else [])
            window._selection_changed()
            window.canvas.queue_draw()
            pump(0.5)
    renderer = type(window.get_native().get_renderer()).__name__
    count = warnings()
    report(f"runtime_gpu_clip: renderer {renderer}, 'Clipping is broken' warnings: {count}")
    status["code"] = 0 if count == 0 else 1
    application.quit()
    return False


GLib.timeout_add(2500, run)
GLib.timeout_add_seconds(120, application.quit)
application.run([])
sys.exit(status["code"])
