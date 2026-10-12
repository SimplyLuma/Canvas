"""Import rules must survive editing and cross-page component propagation."""
import unittest
from dataclasses import asdict
import fig_fixtures
from luma_canvas.fig import import_fig
import tempfile
from pathlib import Path
def import_fig_bytes(data):
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/"fixture.fig";path.write_bytes(data);return import_fig(path)
from luma_canvas.fig.layouts import auto_layout
from luma_canvas.layout import apply_auto_layout
from luma_canvas.layout_grids import grid_bands
from luma_canvas.model import Document,LayoutObject,Page,new_id
from luma_canvas.editing import Editor

class EditableImportTests(unittest.TestCase):
    def test_fixed_width_hug_height_alignment_and_nested_translation(self):
        rule=auto_layout({'stackMode':'HORIZONTAL','stackPrimarySizing':'FIXED','stackCounterSizing':'RESIZE_TO_FIT_WITH_IMPLICIT_SIZE','stackPrimaryAlignItems':'CENTER','stackCounterAlignItems':'CENTER','stackHorizontalPadding':10,'stackVerticalPadding':5})
        frame=LayoutObject('f','frame','Button',x=100,y=100,width=100,height=90,layout=rule)
        child=LayoutObject('c','frame','Content',parent_id='f',x=110,y=105,width=20,height=20)
        nested=LayoutObject('n','rectangle','Nested',parent_id='c',x=112,y=107,width=5,height=5)
        document=Document.empty();document.objects=[frame,child,nested]
        apply_auto_layout(document,frame)
        self.assertEqual((frame.width,frame.height),(100,30))
        self.assertEqual((child.x,child.y),(140,105));self.assertEqual((nested.x,nested.y),(142,107))
        frame.width=200;apply_auto_layout(document,frame)
        self.assertEqual((child.x,nested.x),(190,192))

    def test_grid_stretch_fixed_rows_and_visibility(self):
        grid={'pattern':'STRIPES','type':'STRETCH','axis':'X','numSections':5,'offset':20,'gutterSize':10}
        bands=grid_bands(grid,414,896);self.assertEqual(len(bands),5)
        self.assertAlmostEqual(bands[0][0],20);self.assertAlmostEqual(bands[-1][0]+bands[-1][2],394)
        self.assertEqual(grid_bands({**grid,'visible':False},414,896),[])
        centered=grid_bands({**grid,'axis':'Y','type':'CENTER','sectionSize':20},100,200)
        self.assertEqual(centered[0],(0,30,100,20));self.assertEqual(centered[-1],(0,150,100,20))

    def test_imported_component_edits_keep_overrides_and_cross_pages(self):
        result=import_fig_bytes(fig_fixtures.components().data)
        document=result.document
        definition=next(o for o in document.objects if o.kind=='component' and o.name=='Button')
        label=next(o for o in document.children(definition.id) if o.name=='Label')
        plain=next(o for o in document.objects if o.name=='Plain')
        overridden=next(o for o in document.objects if o.name=='Overridden')
        plain_label=next(o for o in document.children(plain.id) if o.name=='Label')
        custom_label=next(o for o in document.children(overridden.id) if o.name=='Label')
        family=[definition,*document.descendants(definition.id)];ids={o.id for o in family}
        definition.parent_id=None
        source_page=Page(new_id('page'),'Definitions',objects=[asdict(o) for o in family])
        document.pages.append(source_page);document.objects=[o for o in document.objects if o.id not in ids]
        main_page=document.current_page_id;document.show_page(source_page.id)
        editor=Editor(document);editor.set_property([label.id],'text','Updated master')
        document=editor.document;document.show_page(main_page)
        self.assertEqual(document.object(plain_label.id).text,'Updated master')
        self.assertEqual(document.object(custom_label.id).text,'Buy now')
        document.validate();restored=Document.from_dict(document.to_dict());restored.validate()
        self.assertEqual(restored.object(plain.id).component_source_id,definition.id)
        editor=Editor(restored);clone=editor.create_instance(definition.id,500,500)
        self.assertEqual(editor.document.children(clone.id)[0].text,'Updated master')
        editor.document.validate()

    def test_master_layout_and_instance_resize_override_survive_sync(self):
        from luma_canvas.components import make_component
        document=Document.empty()
        definition=LayoutObject('master','frame','Button',width=100,height=30)
        label=LayoutObject('label','text','Label',parent_id='master',width=20,height=20)
        document.objects=[definition,label]
        instance=make_component(document,definition);editor=Editor(document)
        editor.set_geometry(instance.id,width=180)
        rule={'auto':True,'direction':'horizontal','gap':8,'padding':10,'primary_align':'centre','main_sizing':'fixed','cross_sizing':'fixed'}
        editor.set_property([definition.id],'layout',rule)
        self.assertEqual(instance.width,180)
        self.assertEqual(instance.layout,rule)
        self.assertEqual(document.children(instance.id)[0].metadata['component_origin_id'],'label')
        document.validate()

    def test_grid_is_visible_editor_guide_and_absent_from_exports(self):
        from gi.repository import Gtk
        from types import SimpleNamespace
        from luma_canvas.layout_grids import draw
        from luma_canvas.viewport import View
        from luma_canvas.export import export_png
        document=Document.empty()
        frame=LayoutObject('frame','frame','Frame',width=100,height=80,fill='#ffffff')
        document.objects=[frame]
        with tempfile.TemporaryDirectory() as folder:
            before=Path(folder)/'before.png';after=Path(folder)/'after.png'
            export_png(document,before)
            frame.metadata={'layout_grids':[{'pattern':'STRIPES','axis':'X','type':'STRETCH','numSections':5,'offset':10,'gutterSize':5,'color':{'r':1,'g':0,'b':0,'a':1}}]}
            snapshot=Gtk.Snapshot()
            draw(SimpleNamespace(document=document,get_width=lambda:100,get_height=lambda:80),snapshot,View(1,0,0))
            self.assertIsNotNone(snapshot.to_node(),'Grid did not produce an editor overlay')
            export_png(document,after)
            self.assertEqual(before.read_bytes(),after.read_bytes(),'Grid guides leaked into exported artwork')
