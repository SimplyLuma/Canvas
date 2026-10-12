# SPDX-License-Identifier: MPL-2.0
"""Blend modes on the real renderers, against the compositing spec.

The Cairo path is checked by tests/test_blend_pixels.py in the usual suite.
This drives a real window, so the renderer is the one the canvas would use:

    GSK_RENDERER=opengl python3 tests/runtime_blend_pixels.py
    GSK_RENDERER=vulkan python3 tests/runtime_blend_pixels.py
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from blend_reference import CASES, as_bytes, case_document  # noqa: E402
from pixels import OffscreenRenderer, difference  # noqa: E402

TOLERANCE = 3   # GPU renderers work in 8 bit premultiplied; one step each way


def main() -> int:
    if not Gtk.init_check():
        print("no display")
        return 2
    renderer = OffscreenRenderer()
    wanted_kind = os.environ.get("GSK_RENDERER", "cairo").lower()
    name = renderer.name
    print(f"renderer: {name} (GSK_RENDERER={wanted_kind})")
    if wanted_kind in {"opengl", "gl", "ngl", "vulkan"} and "Cairo" in name:
        print(f"FAIL: asked for {wanted_kind}, got {name}")
        return 1
    failures = []
    for case in CASES:
        document, spot = case_document(case)
        found = renderer.pixels(document, [spot])[0]
        wanted = as_bytes(case.expected)
        if difference(found, wanted) > TOLERANCE:
            failures.append(f"{case.name}: drew {found[:3]}, wanted {wanted}")
    checked = len(CASES) + fixture_probes(renderer, failures)
    renderer.close()
    for line in failures:
        print("FAIL " + line)
    print(f"{checked - len(failures)}/{checked} blend and fixture probes match on {name}")
    return 1 if failures else 0


def fixture_probes(renderer, failures: list[str]) -> int:
    """The `.fig` fixture's own probes, imported and drawn on this renderer."""
    import tempfile
    from pathlib import Path

    import fig_fixtures
    from luma_canvas.fig import import_fig
    fixture = fig_fixtures.blending()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "blending.fig"
        path.write_bytes(fixture.data)
        document = import_fig(path).document
    points = [(x, y) for x, y, _ in fixture.probes]
    found = renderer.pixels(document, points)
    for (x, y, wanted), pixel in zip(fixture.probes, found):
        want = tuple(int(wanted[index:index + 2], 16) for index in (1, 3, 5))
        if difference(pixel, want) > TOLERANCE:
            failures.append(f"fixture probe {x},{y}: drew {pixel[:3]}, wanted {want}")
    return len(fixture.probes)


if __name__ == "__main__":
    sys.exit(main())
