"""Persistent interactions, history, overlays and easing contracts."""
import math
import unittest
from luma_canvas.model import Document,LayoutObject,new_id
from luma_canvas.prototype import connect,disconnect,Presentation,ease,TRANSITIONS,transition_parts

class PrototypeExtendedTests(unittest.TestCase):
    def setUp(self):
        self.document=Document.empty('Prototype')
        self.a=LayoutObject(new_id('frame'),'frame','A',width=400,height=800)
        self.b=LayoutObject(new_id('frame'),'frame','B',x=500,width=400,height=800)
        self.c=LayoutObject(new_id('frame'),'frame','Modal',x=1000,width=200,height=200)
        self.button=LayoutObject(new_id('rectangle'),'rectangle','Go',parent_id=self.a.id,width=100,height=40)
        self.document.objects.extend([self.a,self.b,self.c,self.button])

    def test_multiple_triggers_and_exact_update_remove(self):
        first=connect(self.document,self.button,self.b,trigger='click')
        second=connect(self.document,self.button,self.c,trigger='keyboard',key='<Control>k')
        connect(self.document,self.button,self.c,connection_id=first.id,trigger='click',delay_ms=1200,easing='ease-out')
        self.assertEqual(len(self.document.connections),2)
        loaded=Document.from_dict(self.document.to_dict())
        self.assertEqual(loaded.connections[1].key,'<Control>k')
        self.assertEqual(loaded.connections[0].delay_ms,1200)
        disconnect(self.document,self.button.id,second.id)
        self.assertEqual(len(self.document.connections),1)

    def test_overlay_swap_close_and_back_leave_screen_history_intact(self):
        link=connect(self.document,self.button,self.c,action='open-overlay',overlay_position='bottom',scrim_opacity=.4)
        run=Presentation.begin(self.document,self.a);run.activate(self.button.id)
        self.assertEqual(run.current_id,self.a.id);self.assertEqual(run.history,[])
        swap=connect(self.document,self.c,self.b,action='swap-overlay',overlay_position='top')
        run.activate(self.c.id)
        self.assertEqual(run.overlays[-1][0],self.b.id)
        self.assertEqual(run.overlays[-1][1].overlay_position,'bottom')
        self.assertTrue(run.back());self.assertEqual(run.overlays,[])
        self.assertFalse(run.back())

    def test_actions_back_and_conditional_use_isolated_runtime_variables(self):
        from luma_canvas.editing import Editor
        from luma_canvas.resources import Resources
        resources=Resources(Editor(self.document));resources.set_variable('Step',0,'number')
        connect(self.document,self.button,None,action='set-variable',variable='Step',value='2')
        run=Presentation.begin(self.document,self.a);run.activate(self.button.id)
        self.assertEqual(resources.variables()['Step']['value'],2)
        connect(self.document,self.button,self.b,action='conditional',variable='Step',value='2',else_destination_id=self.c.id)
        run.activate(self.button.id);self.assertEqual(run.current_id,self.b.id)
        connect(self.document,self.b,None,action='back');run.activate(self.b.id)
        self.assertEqual(run.current_id,self.a.id)
        resources.set_variable('Step',3,'number');run.activate(self.button.id)
        self.assertEqual(run.current_id,self.c.id)

    def test_delay_duration_easing_positions_reject_nonfinite_and_invalid(self):
        for values in ({'duration_ms':-1},{'delay_ms':-1},{'scrim_opacity':1.1},{'overlay_x':math.inf},{'easing':'random'}):
            with self.assertRaises((ValueError,OverflowError)):connect(self.document,self.button,self.b,**values)
        self.assertEqual(self.document.connections,[])

    def test_easing_exact_endpoints_and_distinct_intermediate_values(self):
        for curve in ('linear','ease-in','ease-out','ease-in-out','spring'):
            self.assertEqual(ease(0,curve),0);self.assertEqual(ease(1,curve),1)
        self.assertLess(ease(.25,'ease-in'),ease(.25,'linear'))
        self.assertGreater(ease(.25,'ease-out'),ease(.25,'linear'))
        for value in TRANSITIONS:
            family,direction=transition_parts(value)
            self.assertIn(direction,('up','down','left','right'))
            self.assertTrue(family)

    def test_delete_conditional_destination_does_not_leave_dangling_reference(self):
        from luma_canvas.editing import Editor
        connect(self.document,self.button,self.b,action='conditional',variable='Step',value='2',else_destination_id=self.c.id)
        Editor(self.document).delete([self.c.id])
        self.assertEqual(self.document.connections,[])
        self.document.validate()

    def test_external_link_round_trip(self):
        link=connect(self.document,self.button,None,action='open-link',url='https://example.com')
        loaded=Document.from_dict(self.document.to_dict())
        self.assertEqual(loaded.connections[0].url,link.url)
        self.assertEqual(loaded.connections[0].action,'open-link')
