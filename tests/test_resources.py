import unittest
from luma_canvas.model import Document
from luma_canvas.editing import Editor
from luma_canvas.resources import Resources
from luma_canvas.paint import solid, parse_hex

class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.document=Document.empty('Resources')
        self.editor=Editor(self.document)
        self.resources=Resources(self.editor)
        self.item=self.editor.create('rectangle',20,30,100,100,fill='#ff0000')

    def test_color_style_propagates_and_undo_preserves_links(self):
        style=self.resources.create_style(self.item,'Accent','color')
        second=self.editor.create('rectangle',200,30,100,100)
        self.resources.apply_style(second,style.id)
        self.resources.update_style(style.id,'Accent',{'value':'#00ff00'})
        self.assertEqual(self.item.fill,'#00ff00')
        self.assertEqual(second.fill,'#00ff00')
        self.editor.undo()
        self.assertEqual(self.document.object(self.item.id).fill,'#ff0000')
        self.assertIn(style.id,self.document.object(self.item.id).style_ids)
        reopened=Document.from_dict(self.document.to_dict())
        reopened.validate()
        self.assertIn(style.id,reopened.object(second.id).style_ids)

    def test_variable_propagates_geometry_and_detaches_on_remove(self):
        self.resources.set_variable('Width',120,'number')
        self.resources.bind_variable(self.item,'width','Width')
        self.resources.set_variable('Width',180,'number')
        self.assertEqual(self.item.width,180)
        reopened=Document.from_dict(self.document.to_dict())
        self.assertEqual(reopened.object(self.item.id).metadata['variable_bindings']['width'],'Width')
        self.resources.remove_variable('Width')
        self.assertEqual(self.item.width,180)
        self.assertEqual(self.item.metadata['variable_bindings'],{})
        self.editor.undo()
        self.assertIn('Width',self.resources.variables())

    def test_bad_variable_and_type_mismatch_do_not_mutate(self):
        with self.assertRaises(ValueError):self.resources.set_variable('Bad','not color','color')
        self.resources.set_variable('Accent','#123456','color')
        with self.assertRaises(ValueError):self.resources.bind_variable(self.item,'width','Accent')
        self.assertEqual(self.item.width,100)

    def test_manual_edit_detaches_only_changed_binding(self):
        self.resources.set_variable('Width',120,'number')
        self.resources.bind_variable(self.item,'width','Width')
        self.editor.set_geometry(self.item.id,width=150)
        self.resources.set_variable('Width',180,'number')
        self.assertEqual(self.item.width,150)
        style=self.resources.create_style(self.item,'Accent','color')
        self.editor.set_paint([self.item.id],'fill',solid(parse_hex('#abcdef')))
        self.resources.update_style(style.id,'Accent',{'value':'#112233'})
        self.assertEqual(self.item.fill,'#abcdef')
