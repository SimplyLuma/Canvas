# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from luma_canvas.export import export_svg
from luma_canvas.importers import import_svg
from luma_canvas.model import Asset, Document, new_id
from luma_canvas.persistence import DocumentStore, documents_folder, data_home, APP_ID


class PackageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.document = Document.demonstration()
        self.store = DocumentStore(self.root / "recovery")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_flatpak_autosaves_use_persistent_private_data(self):
        with patch.dict("os.environ", {"FLATPAK_ID": APP_ID,
                        "XDG_DATA_HOME": str(self.root / "data")}, clear=False):
            folder = documents_folder()
            self.assertEqual(folder, data_home() / "Documents")
            folder.mkdir(parents=True)
            path = folder / "Untitled.canvas"
            self.store.save(self.document, path)
            reopened = DocumentStore(self.root / "new-session").load(path)
            self.assertEqual(reopened.id, self.document.id)

    def test_package_is_readable_versioned_and_atomic(self) -> None:
        path = self.root / "demo.canvas"
        self.store.save(self.document, path)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), {"document.json", "manifest.json"})
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format"], "Luma Canvas")
        loaded = self.store.load(path)
        self.assertEqual(loaded.id, self.document.id)
        self.assertEqual(list(self.root.glob(".demo.canvas.*.tmp")), [])

    def test_embedded_assets_are_stored_separately(self) -> None:
        asset = Asset(new_id("asset"), "Tiny image", "image/png", embedded_name="tiny.png")
        self.document.assets.append(asset)
        self.document.embedded_assets[asset.id] = b"\x89PNG fixture"
        path = self.root / "assets.canvas"
        self.store.save(self.document, path)
        loaded = self.store.load(path)
        self.assertEqual(loaded.embedded_assets[asset.id], b"\x89PNG fixture")

    def test_recovery_can_be_discovered_and_discarded(self) -> None:
        recovery = self.store.autosave(self.document, None)
        found = self.store.recoverable()
        self.assertEqual(found[0][1].id, self.document.id)
        self.assertTrue(recovery.exists())
        self.store.discard_recovery(self.document.id)
        self.assertFalse(recovery.exists())

    def test_svg_export_preserves_object_structure(self) -> None:
        path_object = next(item for item in self.document.objects if item.kind == "path")
        destination = export_svg(self.document, self.root / "icon.svg", [path_object.id])
        payload = destination.read_text("utf-8")
        self.assertIn(f'id="{path_object.id}"', payload)
        self.assertIn('data-canvas-kind="path"', payload)
        self.assertIn("<path", payload)

    def test_full_svg_uses_frames_and_masks_as_clip_definitions(self) -> None:
        destination = export_svg(self.document, self.root / "full.svg")
        payload = destination.read_text("utf-8")
        mask = next(item for item in self.document.objects if item.kind == "mask")
        linked_text = next(item for item in self.document.objects if item.text_flow_next_id)
        page = self.document.object(linked_text.parent_id)
        self.assertIn(f'<clipPath id="clip-{mask.id}"', payload)
        self.assertNotIn(f'data-canvas-kind="mask"', payload)
        self.assertIn(f'clip-path="url(#clip-{page.id})"', payload)

    def test_svg_import_preserves_common_editable_elements(self) -> None:
        source = self.root / "source.svg"
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80"><rect id="card" x="4" y="6" width="80" height="40" rx="8" fill="#4878b8"/><text x="8" y="30">Hello</text><path d="M 4 60 L 100 60" stroke="#111"/></svg>', encoding="utf-8")
        imported = import_svg(source)
        self.assertEqual(imported.name, "source")
        self.assertTrue(any(item.kind == "rectangle" and item.name == "card" for item in imported.objects))
        self.assertTrue(any(item.kind == "text" and item.text == "Hello" for item in imported.objects))
        self.assertTrue(any(item.kind == "path" for item in imported.objects))


if __name__ == "__main__":
    unittest.main()
