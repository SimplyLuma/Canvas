# SPDX-License-Identifier: MPL-2.0
"""Pages: add, rename, reorder, delete, switch, per-page layers and view."""

import unittest

from luma_canvas.editing import EditError, Editor
from luma_canvas.model import Document, LayoutObject, new_id


class PageTests(unittest.TestCase):
    def setUp(self):
        self.document = Document.empty("Pages")
        self.editor = Editor(self.document)
        self.first = self.editor.create("rectangle", 0, 0, 10, 10, name="On page 1")

    @property
    def doc(self):
        return self.editor.document

    def test_old_documents_get_one_page(self):
        page = self.doc.ensure_pages()
        self.assertEqual((len(self.doc.pages), page.name), (1, "Page 1"))

    def test_pages_keep_their_own_layers_and_view(self):
        self.doc.zoom = 2.0
        second = self.editor.add_page()
        self.assertEqual(self.doc.objects, [])
        self.editor.create("ellipse", 5, 5, 10, 10, name="On page 2")
        self.doc.zoom = 0.5
        first_id = self.doc.pages[0].id
        self.editor.show_page(first_id)
        self.assertEqual([item.name for item in self.doc.objects], ["On page 1"])
        self.assertEqual(self.doc.zoom, 2.0)
        self.editor.show_page(second)
        self.assertEqual([item.name for item in self.doc.objects], ["On page 2"])
        self.assertEqual(self.doc.zoom, 0.5)
        self.assertEqual(len(self.doc.all_objects()), 2)

    def test_pages_survive_save_and_load(self):
        second = self.editor.add_page("Mobile")
        self.editor.create("ellipse", 5, 5, 10, 10, name="Dot")
        restored = Document.from_dict(self.doc.to_dict())
        self.assertEqual([page.name for page in restored.pages], ["Page 1", "Mobile"])
        self.assertEqual(restored.current_page_id, second)
        restored.show_page(restored.pages[0].id)
        self.assertEqual([item.name for item in restored.objects], ["On page 1"])

    def test_AT_H3_undo_on_another_page_switches_to_it(self):
        self.editor.add_page()
        self.editor.create("ellipse", 5, 5, 10, 10, name="On page 2")
        self.editor.show_page(self.doc.pages[0].id)
        self.editor.undo()
        self.assertEqual(self.doc.current_page_id, self.doc.pages[1].id)
        self.assertEqual(self.doc.objects, [])

    def test_rename_move_and_delete(self):
        second = self.editor.add_page()
        self.editor.rename_page(second, "Desktop")
        self.editor.move_page(second, 0)
        self.assertEqual([page.name for page in self.doc.pages], ["Desktop", "Page 1"])
        self.editor.delete_page(second)
        self.assertEqual([page.name for page in self.doc.pages], ["Page 1"])
        self.assertEqual([item.name for item in self.doc.objects], ["On page 1"])
        with self.assertRaises(EditError):
            self.editor.delete_page(self.doc.pages[0].id)


if __name__ == "__main__":
    unittest.main()
