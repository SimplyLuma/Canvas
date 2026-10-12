"""Retained editor zoom/pan and settled rendering on the real imported file."""
import os,time,statistics
from pathlib import Path
from gi.repository import Gtk,GLib,Gdk
from types import SimpleNamespace
from luma_canvas.application import CanvasApplication
from luma_canvas.persistence import DocumentStore
from luma_canvas.viewport import View

def settle(predicate,timeout=15):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
        if predicate():return
        time.sleep(.005)
    raise AssertionError('Editor render did not settle')
app=CanvasApplication();assert app.register(None);app.activate();w=app.get_active_window()
d=DocumentStore().load(Path(os.environ['CANVAS_EDITOR_ACCEPTANCE']))
w.replace_document(d,None);c=w.canvas
frame=next(o for o in d.objects if o.name=='Homepage' and o.parent_id is None)
c._set_view(View(.8,-frame.x*.8+100,-frame.y*.8+40))
settle(lambda:c.get_mapped() and c._scene_node is not None and not c.renderer.stale and not getattr(c,'_zoom_rebuild_source',0))
original=c.renderer.scene;calls=[]
def scene(*args,**kwargs):calls.append(kwargs.get('device_scale'));return original(*args,**kwargs)
c.renderer.scene=scene
samples=[]
for index in range(40):
    z=.8+index*.003;c._set_view(View(z,-frame.x*z+100+index,-frame.y*z+40))
    start=time.perf_counter();c.do_snapshot(Gtk.Snapshot());samples.append((time.perf_counter()-start)*1000)
assert not calls,('Zoom rebuilt geometry during gesture',calls)
settle(lambda:not getattr(c,'_zoom_rebuild_source',0) and not c.renderer.stale and abs(c._scene_key[1]-c.view.zoom*c.device_scale())<1e-9)
assert calls,'Zoom never refreshed final pixel alignment'
count=len(calls);v=c.view
for index in range(20):
    c._set_view(View(v.zoom,v.x+index,v.y));c.do_snapshot(Gtk.Snapshot())
assert len(calls)==count,'Small pans rebuilt retained scene'
c.editor.set_property([frame.id],'fill','#ff0000');c.queue_draw();c.do_snapshot(Gtk.Snapshot())
assert len(calls)>count,'Paint editing failed to invalidate the retained scene'
print('PASS retained real-file zoom/pan, settled alignment and immediate paint invalidation; snapshot median_ms',round(statistics.median(samples),3),flush=True)
controller=SimpleNamespace(get_current_event_state=lambda:Gdk.ModifierType(0),get_unit=lambda:Gdk.ScrollUnit.SURFACE)
motion=[];scroll=[]
for index in range(100):
    start=time.perf_counter();c._motion(controller,110+index%20*10,110+index%30*10);motion.append((time.perf_counter()-start)*1000)
    start=time.perf_counter();assert c._scroll(controller,.5,.5);scroll.append((time.perf_counter()-start)*1000)
print('PASS actual pointer/scroll handlers; pointer median_ms',round(statistics.median(motion),3),'max_ms',round(max(motion),3),'scroll median_ms',round(statistics.median(scroll),3),'max_ms',round(max(scroll),3),flush=True)
w.close();app.quit()
