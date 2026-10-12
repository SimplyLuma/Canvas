"""Selection must retain the real imported layer model and native GTK rows."""
import os,time,statistics
from pathlib import Path
from gi.repository import Gtk,GLib
from luma_canvas.application import CanvasApplication
from luma_canvas.persistence import DocumentStore
from luma_appkit import Layer,LayerTree
app=CanvasApplication();assert app.register(None);app.activate();w=app.get_active_window()
d=DocumentStore().load(Path(os.environ['CANVAS_EDITOR_ACCEPTANCE']));w.replace_document(d,None)
for _ in range(100):
 if not GLib.MainContext.default().pending():break
 GLib.MainContext.default().iteration(False)
# Complete the initial document paint before measuring subsequent clicks.
# A newly loaded 4k-layer document may still have future frame-clock work after
# the immediate event queue empties; keep cold readiness separate from clicks.
c=w.canvas;ready_start=time.perf_counter();deadline=time.monotonic()+15
while not (c.get_mapped() and c._scene_node is not None and not c.renderer.stale and not getattr(c,'_zoom_rebuild_source',0)):
 assert time.monotonic()<deadline,'Imported editor did not finish its initial paint'
 while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
 time.sleep(.005)
print('Initial imported editor readiness_ms',round((time.perf_counter()-ready_start)*1000,2),flush=True)
model=w.creative_tree.get_layers();samples=[]
profile=None
if os.environ.get('CANVAS_SELECTION_PROFILE'):
 import cProfile
 profile=cProfile.Profile();profile.enable()
items=[o for o in d.objects if o.name in ('Homepage','category-card','Buttton','exercise icon')][:12]
for item in items:
 start=time.perf_counter();d.select([item.id]);w.canvas.emit('selection-changed')
 for _ in range(100):
  if not GLib.MainContext.default().pending():break
  GLib.MainContext.default().iteration(False)
 samples.append((time.perf_counter()-start)*1000)
 assert w.creative_tree.get_layers()==model,'Selection recreated the imported layer model'
 assert item.id in w.creative_tree.get_selected()
if profile:
 profile.disable();profile.dump_stats(str(Path(os.environ['DEV_ARTIFACT_DIR'])/'selection.prof'))
 import pstats
 pstats.Stats(profile).strip_dirs().sort_stats('cumulative').print_stats(18)
# Deliberately expanded native trees must retain row widgets on selection.
tree=LayerTree([Layer(str(i),f'Layer {i}','square') for i in range(500)])
def find_list(widget):
 if isinstance(widget,Gtk.ListBox):return widget
 child=widget.get_first_child()
 while child:
  found=find_list(child)
  if found:return found
  child=child.get_next_sibling()
listing=find_list(tree);assert listing
first=listing.get_first_child()
for i in range(20):tree.set_selected([str(i)])
assert listing.get_first_child()==first,'Native selection recreated GTK rows'
assert tree.get_selected()==['19']
d.select([]);w.canvas.emit('selection-changed')
assert w.creative_tree.get_layers()==model
# Cross-page synchronization replaces the list while keeping row data intact.
d.objects=list(d.objects);w._refresh_creative()
assert w.creative_tree.get_layers()==model,'Equivalent component graph rebuilt layer rows'
frame=next(o for o in d.objects if o.name=='Homepage' and o.parent_id is None)
w.edit('Rename frame',lambda:w.editor.set_property([frame.id],'name','Home renamed'))
assert w.creative_tree.find(frame.id).get_name()=='Home renamed','Cached tree missed rename'
w.editor.undo();w._refresh_all()
assert w.creative_tree.find(frame.id).get_name()=='Homepage','Cached tree missed Undo'
print('PASS real-file selection retains model; expanded native row identity, rename/Undo; selection median_ms',round(statistics.median(samples),2),'max_ms',round(max(samples),2),flush=True)
w.close();app.quit()
