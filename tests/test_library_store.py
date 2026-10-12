# SPDX-License-Identifier: MPL-2.0
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from luma_canvas.library_store import LibraryOrganization, saved_documents


class LibraryStoreTests(unittest.TestCase):
    def test_thousand_designs_deduplicate_ignore_missing_and_sort(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for index in range(1000):
                path = folder / f'Design {index:04}.canvas'
                path.touch()
                os.utime(path, (index, index))
            (folder / 'not-a-design.txt').touch()
            with patch('luma_canvas.library_store.documents_folder', return_value=folder):
                designs = saved_documents([folder / 'Design 0000.canvas', folder / 'gone.canvas'])
            self.assertEqual(len(designs), 1000)
            self.assertEqual(designs[0].path.name, 'Design 0999.canvas')
            self.assertEqual(designs[-1].path.name, 'Design 0000.canvas')

    def test_project_and_stars_persist_without_moving_documents(self):
        with tempfile.TemporaryDirectory() as temp, patch('luma_canvas.library_store.data_home', return_value=Path(temp)):
            path = Path(temp) / 'design.canvas'
            path.write_bytes(b'original document')
            organization = LibraryOrganization()
            organization.projects['portfolio'] = 'Portfolio'
            organization.assignments[str(path)] = 'portfolio'
            organization.starred.add(str(path))
            organization.save()
            restored = LibraryOrganization()
            self.assertEqual(restored.projects, organization.projects)
            self.assertEqual(restored.assignments, organization.assignments)
            self.assertEqual(restored.starred, organization.starred)
            self.assertEqual(path.read_bytes(), b'original document')
