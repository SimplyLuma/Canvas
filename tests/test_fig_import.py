# SPDX-License-Identifier: MPL-2.0
"""The Figma importer: container, Kiwi decoding, conversion and robustness."""

from __future__ import annotations

import io
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fig_fixtures  # noqa: E402
from fig_writer import FigWriter, SchemaBuilder, matrix, pack_canvas, solid, vector  # noqa: E402

from luma_canvas import geometry  # noqa: E402
from luma_canvas.fig import (CloudOnlyFigmaFile, DamagedFigmaFile, EmptyFigmaFile, EncryptedFigmaFile,  # noqa: E402
                             FigImportError, FigmaFileTooLarge, ImportCancelled, Limits, NotAFigmaFile,
                             import_fig, kiwi, paths)
from luma_canvas.model import Document, LayoutObject  # noqa: E402
from luma_canvas.paint import paint_stack  # noqa: E402
from luma_canvas.persistence import DocumentStore  # noqa: E402


def write_temp(data: bytes, suffix: str = ".fig") -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(data)
    handle.close()
    return Path(handle.name)


def imported(fixture_or_data, **options):
    data = fixture_or_data.data if hasattr(fixture_or_data, "data") else fixture_or_data
    path = write_temp(data)
    try:
        return import_fig(path, **options)
    finally:
        path.unlink()


def all_objects(document: Document) -> list[LayoutObject]:
    return document.all_objects()


def by_name(document: Document, name: str) -> LayoutObject:
    matches = [item for item in all_objects(document) if item.name == name]
    if not matches:
        raise AssertionError(f"no layer named {name!r}: {[item.name for item in all_objects(document)]}")
    return matches[0]


class KiwiTests(unittest.TestCase):
    def schema(self) -> SchemaBuilder:
        s = SchemaBuilder()
        s.enum("Kind", ["A", "B", "C"])
        s.struct("Point", [("x", "float"), ("y", "float")])
        s.message("Inner", [("label", "string"), ("kind", "Kind"), ("points", "Point[]"), ("child", "Inner")])
        s.message("Root", [("count", "uint"), ("delta", "int"), ("flag", "bool"), ("raw", "byte[]"),
                           ("items", "Inner[]"), ("big", "uint64"), ("neg", "int64"), ("ratio", "float"),
                           ("numbers", "int[]")])
        return s

    def test_round_trip_every_type(self):
        s = self.schema()
        value = {"count": 300000, "delta": -123456, "flag": True, "raw": b"\x00\xff\x10",
                 "items": [{"label": "héllo ✓", "kind": "C", "points": [{"x": 1.5, "y": -2.25}],
                            "child": {"label": "", "kind": "A"}}],
                 "big": 2 ** 60 + 7, "neg": -(2 ** 40), "ratio": 0.0, "numbers": [0, -1, 1, -2147483648]}
        schema = kiwi.decode_schema(s.encode())
        decoded = kiwi.Decoder(schema).decode(s.encode_value("Root", value), "Root")
        self.assertEqual(decoded["count"], 300000)
        self.assertEqual(decoded["delta"], -123456)
        self.assertTrue(decoded["flag"])
        self.assertEqual(decoded["raw"], b"\x00\xff\x10")
        self.assertEqual(decoded["items"][0]["label"], "héllo ✓")
        self.assertEqual(decoded["items"][0]["kind"], "C")
        self.assertEqual(decoded["items"][0]["points"][0], {"x": 1.5, "y": -2.25})
        self.assertEqual(decoded["items"][0]["child"]["kind"], "A")
        self.assertEqual(decoded["big"], 2 ** 60 + 7)
        self.assertEqual(decoded["neg"], -(2 ** 40))
        self.assertEqual(decoded["numbers"], [0, -1, 1, -2147483648])

    def test_skipped_fields_are_marked_and_watch_reports_progress(self):
        s = self.schema()
        schema = kiwi.decode_schema(s.encode())
        items = [{"label": "x" * 10, "points": [{"x": 1, "y": 2}] * 3} for _ in range(600)]
        data = s.encode_value("Root", {"items": items, "count": 1})
        seen = []
        decoded = kiwi.Decoder(schema, skip=["Inner.points"], watch={"Root.items": seen.append}).decode(data, "Root")
        self.assertIs(decoded["items"][0]["points"], True)
        self.assertEqual(decoded["items"][599]["label"], "x" * 10)
        self.assertGreaterEqual(len(seen), 3)
        self.assertTrue(all(0 <= value <= 1 for value in seen))

    def test_every_truncation_fails_cleanly(self):
        s = self.schema()
        schema = kiwi.decode_schema(s.encode())
        decoder = kiwi.Decoder(schema)
        data = s.encode_value("Root", {"count": 5, "items": [{"label": "abc", "points": [{"x": 3, "y": 4}]}],
                                       "ratio": 2.5, "raw": b"abcdef"})
        for end in range(len(data)):
            with self.assertRaises(kiwi.KiwiError):
                decoder.decode(data[:end], "Root")

    def test_hostile_lengths_and_nesting(self):
        s = self.schema()
        schema = kiwi.decode_schema(s.encode())
        decoder = kiwi.Decoder(schema)
        # An array claiming four billion elements.
        with self.assertRaises(kiwi.KiwiError):
            decoder.decode(bytes([5]) + b"\xff\xff\xff\xff\x0f" + b"\x00" * 8, "Root")
        # Nesting far past the recursion limit.
        nested = bytes([5, 1]) + bytes([4]) * 50000 + b"\x00" * 50002
        with self.assertRaises(kiwi.KiwiError):
            decoder.decode(nested, "Root")
        # A field id the schema does not have.
        with self.assertRaises(kiwi.KiwiError):
            decoder.decode(bytes([99, 0]), "Root")

    def test_damaged_schemas(self):
        good = self.schema().encode()
        for end in range(len(good)):
            try:
                kiwi.decode_schema(good[:end])
            except kiwi.KiwiError:
                continue
        with self.assertRaises(kiwi.KiwiError):
            kiwi.decode_schema(b"\xff\xff\xff\xff\x0f")
        # A field whose type names a definition that does not exist.
        with self.assertRaises(kiwi.KiwiError):
            kiwi.decode_schema(bytes([1]) + b"M\x00" + bytes([2, 1]) + b"f\x00" + bytes([40, 0, 1]))

    def test_names_cannot_inject_code(self):
        s = SchemaBuilder()
        s.message("Root", [("x'); import os; os._exit(3) #", "uint")])
        schema = kiwi.decode_schema(s.encode())
        data = bytes([1, 7, 0])
        decoded = kiwi.Decoder(schema).decode(data, "Root")
        self.assertEqual(list(decoded.values()), [7])


class ContainerTests(unittest.TestCase):
    def small_file(self) -> FigWriter:
        w = FigWriter()
        w.add("RECTANGLE", "Box", w.page, size=vector(10, 10), transform=matrix(), fillPaints=[solid("#ff0000")])
        return w

    def test_zip_raw_and_zstd_containers(self):
        for data in (self.small_file().build(), self.small_file().build(zipped=False),
                     self.small_file().build(compression="zstd")):
            result = imported(data)
            self.assertEqual(by_name(result.document, "Box").fill, "#ff0000")

    def test_clear_errors(self):
        cases = [
            (b"", EmptyFigmaFile),
            (b"   \n", EmptyFigmaFile),
            (b'{"url": "https://www.figma.com/design/abc123/Thing"}', CloudOnlyFigmaFile),
            (b"[InternetShortcut]\nURL=https://www.figma.com/file/xyz\n", CloudOnlyFigmaFile),
            (os.urandom(512), NotAFigmaFile),
            (b"fig-kiwi" + struct.pack("<I", 20) + struct.pack("<I", 1 << 30), DamagedFigmaFile),
            (b"fig-kiwi" + struct.pack("<I", 20), DamagedFigmaFile),
        ]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("meta.json", "{}")
        cases.append((stream.getvalue(), NotAFigmaFile))
        encrypted = bytearray(self.small_file().build())
        # Set the encryption flag on canvas.fig's local and central headers.
        for marker in (b"PK\x03\x04", b"PK\x01\x02"):
            offset = encrypted.find(marker)
            flag_offset = offset + (6 if marker == b"PK\x03\x04" else 8)
            encrypted[flag_offset] |= 1
        cases.append((bytes(encrypted), EncryptedFigmaFile))
        for data, error in cases:
            with self.subTest(error=error.__name__, head=data[:12]):
                with self.assertRaises(error) as caught:
                    imported(data)
                self.assertTrue(caught.exception.reason)

    def test_cloud_only_message_names_save_local_copy(self):
        self.assertIn("Save local copy", CloudOnlyFigmaFile.reason)

    def test_decompression_bombs_are_refused(self):
        w = self.small_file()
        bomb = pack_canvas(w.schema.encode(), b"\x00" * (8 << 20))
        with self.assertRaises(FigmaFileTooLarge):
            imported(bomb, limits=Limits(message_bytes=1 << 20))

    def test_fuzzed_files_only_raise_import_errors(self):
        rng = random.Random(20260915)
        sources = [fixture().data for fixture in (fig_fixtures.shapes, fig_fixtures.vectors,
                                                   fig_fixtures.components)]
        sources += [fig_fixtures.shapes().data]
        raw = FigWriter()
        raw.add("VECTOR", "V", raw.page, size=vector(10, 10), transform=matrix())
        canvases = [zipfile.ZipFile(io.BytesIO(data)).read("canvas.fig") for data in sources]
        started = time.monotonic()
        attempts = 0
        for canvas in canvases:
            for _ in range(60):
                mutated = bytearray(canvas)
                for _ in range(rng.randint(1, 12)):
                    position = rng.randrange(len(mutated))
                    mutated[position] = rng.randrange(256)
                if rng.random() < 0.3:
                    mutated = mutated[: rng.randrange(len(mutated))]
                attempts += 1
                try:
                    imported(bytes(mutated))
                except FigImportError:
                    pass
        # Mutate decompressed messages too, which reach the decoder and converter.
        from luma_canvas.fig import container
        for data in sources:
            archive = container.open_fig(write_temp(data))
            for _ in range(80):
                message = bytearray(archive.message)
                for _ in range(rng.randint(1, 6)):
                    position = rng.randrange(len(message))
                    message[position] = rng.randrange(256)
                attempts += 1
                try:
                    imported(pack_canvas(archive.schema, bytes(message)))
                except FigImportError:
                    pass
            archive.close()
        self.assertGreater(attempts, 400)
        self.assertLess(time.monotonic() - started, 120)

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ImportCancelled):
            imported(fig_fixtures.shapes(), cancel=cancel)


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {fixture.__name__: imported(fixture()) for fixture in fig_fixtures.ALL}

    def document(self, name: str) -> Document:
        return self.results[name].document

    def test_every_fixture_is_a_valid_document_that_saves_and_reopens(self):
        for name, result in self.results.items():
            with self.subTest(fixture=name):
                result.document.validate()
                path = write_temp(b"", ".canvas")
                try:
                    DocumentStore.save(result.document, path)
                    reopened = DocumentStore.load(path)
                    self.assertEqual(len(reopened.all_objects()), len(result.document.all_objects()))
                    self.assertEqual(set(reopened.embedded_assets), set(result.document.embedded_assets))
                finally:
                    path.unlink()

    def test_shapes(self):
        document = self.document("shapes")
        card = by_name(document, "Card")
        self.assertEqual(card.kind, "rectangle")
        self.assertEqual(card.corner_radii, [0.0, 12.0, 24.0, 36.0])
        self.assertEqual((card.x, card.y, card.width, card.height), (20.0, 20.0, 120.0, 80.0))
        self.assertEqual(by_name(document, "Dot").kind, "ellipse")
        rotated = by_name(document, "Rotated")
        self.assertAlmostEqual(rotated.rotation, 30.0, places=4)
        corners = geometry.world_corners(rotated)
        self.assertAlmostEqual(corners[0][0], 330.0, places=3)
        self.assertAlmostEqual(corners[0][1], 20.0, places=3)
        rule = by_name(document, "Rule")
        self.assertEqual((rule.kind, rule.stroke, rule.stroke_width, rule.stroke_cap), ("line", "#222222", 6.0, "round"))
        self.assertAlmostEqual(by_name(document, "Half").opacity, 0.5)
        self.assertFalse(by_name(document, "Hidden").visible)
        self.assertEqual(by_name(document, "Translucent fill").fill, "#ff880066")
        frame = by_name(document, "Shapes")
        self.assertTrue(frame.clip_content)
        self.assertEqual(card.parent_id, frame.id)

    def test_polygons_use_figma_geometry_when_canvas_would_differ(self):
        document = self.document("polygons")
        triangle = by_name(document, "Triangle")
        self.assertEqual(triangle.kind, "path")
        self.assertEqual(triangle.metadata["figma_shape"], "polygon")
        star = by_name(document, "Star")
        self.assertEqual((star.kind, star.point_count, star.inner_ratio), ("star", 5, 0.4))

    def test_strokes(self):
        document = self.document("strokes")
        for align in ("inside", "center", "outside"):
            item = by_name(document, f"Stroke {align}")
            self.assertEqual((item.stroke, item.stroke_width, item.stroke_align), ("#223344", 10.0, align))
        dashed = by_name(document, "Dashed")
        self.assertEqual(dashed.stroke_dash, [12.0, 8.0])
        border = by_name(document, "Bottom border")
        self.assertEqual(border.metadata["stroke_sides"], {"top": 0.0, "right": 0.0, "bottom": 6.0, "left": 0.0})

    def test_gradient_geometry_is_exact(self):
        document = self.document("gradients")
        linear = by_name(document, "Linear").fill_gradient
        self.assertEqual(linear["type"], "linear")
        # The start is the unit box point the transform sends to (0, 0.5).
        self.assertEqual(linear["handles"][0], [0.0, 0.0])
        # In a 200 x 100 box, lines of equal color run perpendicular to the
        # gradient in pixels, not in the unit box.
        sx, sy = linear["handles"][0]
        ex, ey = linear["handles"][1]
        direction = ((ex - sx) * 200, (ey - sy) * 100)
        iso = (1.0 * 200, -1.0 * 100)   # unit box direction of an equal-color line, in pixels
        self.assertAlmostEqual(direction[0] * iso[0] + direction[1] * iso[1], 0.0, places=3)
        radial = by_name(document, "Radial").fill_gradient
        self.assertEqual(radial["handles"], [[0.5, 0.5], [0.5, 1.0], [1.0, 0.5]])
        self.assertEqual([stop["position"] for stop in radial["stops"]], [0.0, 0.5, 1.0])
        self.assertAlmostEqual(by_name(document, "Faded").fill_gradient["opacity"], 0.5)

    def test_vectors(self):
        document = self.document("vectors")
        donut = by_name(document, "Donut")
        self.assertEqual(donut.metadata["fill_rule"], "evenodd")
        self.assertEqual(len(geometry.outline(donut)), 2)
        self.assertTrue(all(shape.closed for shape in geometry.outline(donut)))
        zigzag = by_name(document, "Zigzag")
        self.assertIsNone(zigzag.fill)
        self.assertFalse(any(shape.closed for shape in geometry.outline(zigzag)))
        self.assertEqual(zigzag.stroke_join, "round")
        triangle = by_name(document, "Rounded triangle")
        cubic_segments = [segment for shape in geometry.outline(triangle) for segment in shape.segments
                          if segment[0] == "C"]
        self.assertEqual(len(cubic_segments), 3)
        punched = by_name(document, "Punched")
        self.assertEqual((punched.kind, punched.metadata["boolean_operation"]), ("boolean", "subtract"))
        self.assertEqual(len(geometry.outline(punched)), 2)
        # The shapes stay editable inside the boolean, and Figma's result is kept until they change.
        self.assertEqual([child.name for child in document.children(punched.id)], ["Square", "Hole"])
        figma_path = [dict(node) for node in punched.path]
        from luma_canvas.booleans import update_booleans
        self.assertFalse(update_booleans(document.objects))
        self.assertEqual(punched.path, figma_path)
        hole = by_name(document, "Hole")
        hole.width = hole.height = 20
        self.assertTrue(update_booleans(document.objects))
        self.assertEqual(len(geometry.outline(punched)), 2)
        self.assertFalse(self.results["vectors"].report.has("boolean"))

    def test_images(self):
        result = self.results["images"]
        document = result.document
        photo = by_name(document, "Photo")
        self.assertEqual(photo.kind, "image")
        self.assertIn(photo.asset_id, document.embedded_assets)
        asset = next(asset for asset in document.assets if asset.id == photo.asset_id)
        self.assertEqual((asset.media_type, asset.width, asset.height), ("image/png", 40, 20))
        self.assertEqual(asset.embedded_name, asset.id)
        modes = {name: paint_stack(by_name(document, name), "fill")[-1]["scale_mode"]
                 for name in ("Fill", "Fit", "Tile", "Crop")}
        self.assertEqual(modes, {"Fill": "fill", "Fit": "fit", "Tile": "tile", "Crop": "crop"})
        self.assertEqual(by_name(document, "Fit").fill, "#dddddd")
        self.assertEqual(len(document.assets), 2)

    def test_clipping_and_masks(self):
        document = self.document("clipping")
        self.assertTrue(by_name(document, "Clips").clip_content)
        self.assertFalse(by_name(document, "Shows overflow").clip_content)
        # Figma's mask is Canvas's own: the mask layer masks the siblings above it.
        mask = by_name(document, "Mask")
        self.assertEqual(mask.metadata["mask"], "outline")
        self.assertTrue(mask.visible)
        group = document.object(mask.parent_id)
        self.assertEqual(by_name(document, "Masked stripe").parent_id, group.id)
        siblings = document.children(group.id)
        self.assertEqual(siblings[0].id, mask.id)

    def test_groups_rotation_and_flips(self):
        document = self.document("groups")
        outer = by_name(document, "Outer group")
        self.assertEqual(outer.kind, "group")
        a = by_name(document, "A")
        self.assertAlmostEqual(a.rotation, 45.0, places=4)
        corners = geometry.world_corners(a)
        self.assertAlmostEqual(corners[0][0], 120.0, places=3)
        self.assertAlmostEqual(corners[0][1], 20.0, places=3)
        flipped = by_name(document, "Flipped")
        self.assertTrue(flipped.flip_h)
        self.assertAlmostEqual(flipped.rotation, 0.0)
        self.assertAlmostEqual(flipped.x, 40.0)
        bounds = geometry.union_bounds(document.children(outer.id))
        self.assertEqual((outer.x, outer.y), (bounds[0], bounds[1]))

    def test_effects(self):
        result = self.results["effects"]
        shadow = by_name(result.document, "Shadowed").effects[0]
        self.assertEqual(shadow, {"type": "drop_shadow", "x": 0.0, "y": 8.0, "blur": 24.0, "spread": 0.0,
                                  "color": "#00000040", "visible": True, "blend": "normal", "behind": False})
        self.assertEqual(by_name(result.document, "Inset").effects[0]["type"], "inner_shadow")
        self.assertEqual(by_name(result.document, "Blurred").effects[0], {"type": "layer_blur", "radius": 8.0,
                                                                          "visible": True})
        self.assertEqual(by_name(result.document, "Frosted").effects[0],
                          {"type": "background_blur", "radius": 20.0, "visible": True})
        self.assertFalse(result.report.has("background_blur"))   # Canvas draws it

    def test_line_ends(self):
        document = self.document("arrows")
        arrow = by_name(document, "Arrow")
        self.assertEqual((arrow.start_cap, arrow.end_cap), ("circle", "triangle_arrow"))
        both = by_name(document, "Both ends")
        self.assertEqual((both.start_cap, both.end_cap), ("line_arrow", "line_arrow"))

    def test_text(self):
        result = self.results["text"]
        mixed = by_name(result.document, "Mixed")
        self.assertEqual(mixed.text, "Mixed bold and colored text")
        self.assertEqual(mixed.metadata["font_family"], "Figtree")
        self.assertEqual(mixed.metadata["line_height"], {"unit": "percent", "value": 150.0})
        self.assertEqual(mixed.metadata["letter_spacing"], {"unit": "percent", "value": 2.0})
        ranges = mixed.metadata["ranges"]
        self.assertEqual(ranges[0], {"start": 6, "end": 10, "font_weight": 700})
        self.assertEqual(ranges[1]["start"], 15)
        self.assertEqual(ranges[1]["end"], 22)
        self.assertEqual(ranges[1]["fill"], "#cc0033")
        self.assertEqual(ranges[1]["decoration"], "underline")
        self.assertEqual(ranges[1]["font_size"], 28.0)
        centered = by_name(result.document, "Centered")
        self.assertEqual((centered.metadata["align_h"], centered.metadata["align_v"]), ("center", "bottom"))
        self.assertEqual((centered.metadata["sizing"], centered.metadata["case"]), ("auto_height", "upper"))
        self.assertEqual((centered.metadata["font_weight"], centered.metadata["italic"]), (600, True))
        self.assertTrue(centered.metadata["truncate"])
        self.assertEqual(centered.metadata["max_lines"], 2)
        self.assertEqual(centered.metadata["line_height"], {"unit": "px", "value": 20.0})

    def test_components_and_instances(self):
        result = self.results["components"]
        document = result.document
        self.assertEqual(by_name(document, "Button").kind, "component")
        objects = all_objects(document)
        plain = by_name(document, "Plain")
        self.assertEqual(plain.kind, "instance")
        self.assertEqual(plain.component_source_id,by_name(document,"Button").id)
        plain_children = [item for item in objects if item.parent_id == plain.id]
        self.assertEqual([item.name for item in plain_children], ["Label", "Icon"])
        self.assertEqual(plain_children[0].text, "Default")
        self.assertAlmostEqual(plain_children[0].x, 30.0)
        self.assertAlmostEqual(plain_children[0].y, 90.0)
        overridden = by_name(document, "Overridden")
        label, icon = [item for item in objects if item.parent_id == overridden.id]
        self.assertEqual((label.text, label.fill), ("Buy now", "#ffff00"))
        self.assertFalse(icon.visible)
        stretched = by_name(document, "Stretched")
        stretched_icon = [item for item in objects if item.parent_id == stretched.id][1]
        self.assertAlmostEqual(stretched_icon.x, 20 + 184)
        holder = by_name(document, "Holder instance")
        nested = next(item for item in objects if item.parent_id == holder.id)
        self.assertEqual(nested.metadata["figma_component"], "Badge")
        self.assertEqual((nested.fill, nested.width, nested.height, nested.radius), ("#ff9900", 40.0, 40.0, 20.0))
        self.assertEqual([item for item in objects if item.parent_id == nested.id], [])
        missing = by_name(document, "Missing library component")
        self.assertEqual([item for item in objects if item.parent_id == missing.id], [])
        self.assertTrue(result.report.has("components_missing"))
        self.assertFalse(result.report.has("instances"))
        ids = [item.id for item in objects]
        self.assertEqual(len(ids), len(set(ids)))

    def test_pages_constraints_and_report(self):
        result = self.results["pages"]
        document = result.document
        self.assertEqual([page.name for page in document.pages], ["Cover", "Details"])
        self.assertEqual(document.pages[1].background, "#222222")
        pinned = by_name(document, "Pinned")
        self.assertEqual(pinned.constraints, {"horizontal": "right", "vertical": "top-bottom"})
        self.assertEqual(pinned.metadata["blend"], "multiply")
        frame = by_name(document, "Auto layout")
        self.assertEqual(frame.metadata["figma_auto_layout"]["stackMode"], "HORIZONTAL")
        report = result.report
        self.assertTrue(frame.layout["auto"])
        self.assertFalse(report.has("auto_layout"))
        for topic in ("figjam", "layer_types", "hidden_pages"):
            self.assertTrue(report.has(topic), topic)
        self.assertNotIn("Library thing", [item.name for item in all_objects(document)])
        sticky = by_name(document, "Sticky")
        self.assertEqual(sticky.kind, "frame")
        self.assertIn("Remember", [item.text for item in all_objects(document)])
        titles = [finding.title for finding in report.visible_findings]
        self.assertTrue(all("—" not in title for title in titles))


class ExportSettingsImportTests(unittest.TestCase):
    def test_figma_export_rows_become_canvas_export_settings(self):
        from luma_canvas.export import export_settings, plan_exports
        result = imported(fig_fixtures.exports())
        document = result.document
        card = by_name(document, "Card")
        self.assertEqual(export_settings(card), [
            {"format": "png", "constraint": "scale", "value": 1.0, "suffix": ""},
            {"format": "png", "constraint": "scale", "value": 2.0, "suffix": "@2x"},
            {"format": "jpeg", "constraint": "width", "value": 512.0, "suffix": "-thumb"},
            {"format": "svg", "constraint": "scale", "value": 1.0, "suffix": ""},
            {"format": "pdf", "constraint": "scale", "value": 1.0, "suffix": ""},
        ])
        dot = by_name(document, "icons/dot")
        self.assertEqual(export_settings(dot), [{"format": "png", "constraint": "height", "value": 120.0,
                                                 "suffix": ""}])
        self.assertNotIn("export_settings", by_name(document, "Plain").metadata)
        report = result.report
        self.assertTrue(report.has("export_settings"))
        finding = next(item for item in report.findings if item.topic == "export_settings")
        self.assertEqual(finding.extra, {"bmp"})
        self.assertEqual(finding.layers, ["Card"])
        paths_out = [job.path for job in plan_exports([(card.name, [card.id], export_settings(card))])]
        self.assertEqual(paths_out, ["Card.png", "Card@2x.png", "Card-thumb.jpg", "Card.svg", "Card.pdf"])

    def test_settings_survive_saving(self):
        from luma_canvas.export import export_settings
        document = imported(fig_fixtures.exports()).document
        with tempfile.TemporaryDirectory() as folder:
            loaded = DocumentStore.load(DocumentStore.save(document, Path(folder) / "Exports.canvas"))
        self.assertEqual(len(export_settings(by_name(loaded, "Card"))), 5)


class RegistryTests(unittest.TestCase):
    def test_registry_reads_fig(self):
        from luma_canvas import importers
        fmt = importers.format_for(Path("design.fig"))
        self.assertTrue(fmt.available)
        path = write_temp(fig_fixtures.shapes().data)
        try:
            document = importers.import_document(path)
        finally:
            path.unlink()
        self.assertIn("Card", [item.name for item in document.all_objects()])


class PathTests(unittest.TestCase):
    def test_commands_and_nodes(self):
        from fig_writer import commands
        subpaths = paths.decode_commands(commands(("M", 0, 0), ("L", 10, 0), ("Q", 10, 10, 0, 10), ("Z",),
                                                  ("M", 2, 2), ("C", 3, 2, 4, 3, 4, 4)))
        self.assertEqual(len(subpaths), 2)
        self.assertTrue(subpaths[0].closed)
        nodes = paths.to_nodes(subpaths)
        self.assertTrue(nodes[3]["move"])
        shapes = geometry.nodes_outlines(nodes)
        self.assertEqual(len(shapes), 2)
        with self.assertRaises(paths.GeometryError):
            paths.decode_commands(b"\x09")
        with self.assertRaises(paths.GeometryError):
            paths.decode_commands(b"\x04\x00\x00")

    def test_damaged_networks(self):
        from fig_writer import network
        good = network([(0, 0), (1, 0), (1, 1)], [(0, 1), (1, 2), (2, 0)], [("NONZERO", [[0, 1, 2]])])
        for end in range(len(good)):
            try:
                paths.decode_network(good[:end])
            except paths.GeometryError:
                pass
        bad = bytearray(good)
        struct.pack_into("<I", bad, 12 + 3 * 12 + 4, 99)   # a segment naming vertex 99
        with self.assertRaises(paths.GeometryError):
            paths.decode_network(bytes(bad))


class PerformanceTests(unittest.TestCase):
    def test_thousands_of_nodes_import_quickly(self):
        data = fig_fixtures.stress(5000)
        progress: list[float] = []
        started = time.monotonic()
        result = imported(data, progress=lambda fraction, _stage: progress.append(fraction))
        elapsed = time.monotonic() - started
        layers = len(result.document.all_objects())
        print(f"\nstress: {len(data) / 1e6:.1f} MB, {layers} layers in {elapsed:.2f} s", file=sys.stderr)
        self.assertGreater(layers, 5000)
        self.assertEqual(progress, sorted(progress))
        self.assertAlmostEqual(progress[-1], 1.0)
        self.assertLess(elapsed, float(os.environ.get("FIG_IMPORT_BUDGET", "20")))


SAMPLES = os.environ.get("LUMA_FIG_SAMPLES")


@unittest.skipUnless(SAMPLES and Path(SAMPLES).is_dir(), "set LUMA_FIG_SAMPLES to a folder of .fig files")
class SampleFileTests(unittest.TestCase):
    """Openly licensed real files kept in a local cache (see docs/fig-import.md)."""

    def test_samples_import(self):
        for path in sorted(Path(SAMPLES).glob("*.fig")):
            with self.subTest(file=path.name):
                result = import_fig(path)
                result.document.validate()
                self.assertTrue(result.document.pages)


if __name__ == "__main__":
    unittest.main()


class WorkerProcessTests(unittest.TestCase):
    def test_imports_in_a_child_process(self):
        from luma_canvas.fig.worker import import_in_process
        path = write_temp(fig_fixtures.images().data)
        seen = []
        try:
            result = import_in_process(path, progress=lambda fraction, stage: seen.append(fraction))
        finally:
            path.unlink()
        result.document.validate()
        self.assertEqual(by_name(result.document, "Photo").kind, "image")
        self.assertEqual(len(result.document.embedded_assets), 2)
        self.assertTrue(seen)

    def test_pages_reports_and_errors_cross_the_process_boundary(self):
        from luma_canvas.fig.worker import import_in_process
        path = write_temp(fig_fixtures.pages().data)
        try:
            result = import_in_process(path)
        finally:
            path.unlink()
        self.assertEqual([page.name for page in result.document.pages], ["Cover", "Details"])
        self.assertIn("Pinned", [item.name for item in result.document.all_objects()])
        self.assertFalse(result.report.has("auto_layout"))
        self.assertTrue(by_name(result.document,"Auto layout").layout["auto"])
        link = write_temp(b'{"url": "https://www.figma.com/file/abc"}')
        try:
            with self.assertRaises(CloudOnlyFigmaFile) as caught:
                import_in_process(link)
        finally:
            link.unlink()
        self.assertIn("Save local copy", caught.exception.reason)

    def test_cancel_stops_the_child(self):
        from luma_canvas.fig.worker import import_in_process
        path = write_temp(fig_fixtures.stress(20000))
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        try:
            with self.assertRaises(ImportCancelled):
                import_in_process(path, cancel=cancel)
        finally:
            path.unlink()
        self.assertLess(time.monotonic() - started, 3.0)

    def test_frames_cannot_carry_code(self):
        import pickle
        from luma_canvas.fig.worker import _load
        with self.assertRaises(pickle.UnpicklingError):
            _load(pickle.dumps(("layers", 0, [Path("/")])))
        self.assertEqual(_load(pickle.dumps(("asset", "a", b"\x00"))), ("asset", "a", b"\x00"))
