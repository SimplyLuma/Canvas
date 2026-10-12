"""Responsive fixture startup, tools and one-artboard phone fitting."""
import os,time
from pathlib import Path
os.environ['LUMA_CANVAS_FIXTURE']=str(Path(__file__).parent/'fixtures/canvas-v70.json')
os.environ['LUMA_CANVAS_FIXTURE_ASSETS']='/design/social-luma/photos'
import gi
gi.require_version('Gtk','4.0')
from gi.repository import GLib
from luma_canvas.application import CanvasApplication

def until(test):
 end=time.monotonic()+20
 while time.monotonic()<end:
  while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
  if test():return
  time.sleep(.01)
 raise AssertionError('layout did not settle')
app=CanvasApplication();assert app.register(None);app.activate();w=app.get_active_window()
until(lambda:w._initial_fit_done)
for width in (360,402,720,1180):
 w.set_default_size(width,874)
 until(lambda:w.get_width()==width)
 w._fit_v70_initial()
 for tool in ('move','rectangle','text','hand'):
  w._creative_tool(tool)
 assert w.document.zoom>0
 if width==720 and not w.document.selection:
  assert w.document.zoom>.10, "hidden inspector must not consume fit space"
 if w._phone_ui:
  w.document.select(['n4']);w._selection_changed()
  def labels(root):
   result=[]
   from gi.repository import Gtk
   if isinstance(root,Gtk.Label):result.append(root.get_text())
   child=root.get_first_child()
   while child: result.extend(labels(child));child=child.get_next_sibling()
   return result
  until(lambda:{'Design','Duplicate','Forward','Delete','Done'}.issubset(set(labels(w.phone_actions))))
  w._phone_grow('design');assert w.phone_actions.grown=='design'
  w._duplicate();assert w.document.selection
  w._phone_deselect();w._phone_grow('layers');assert w.phone_actions.grown=='layers'
  w._phone_layers_page('assets');assert w.phone_actions.grown=='layers'
  w._phone_layers_page('layers');assert w.phone_actions.grown=='layers'
  w._phone_grow('layers');assert w.phone_actions.grown is None

 if w.creative_workspace.get_is_phone():
  frame=next(n for n in w.document.objects if n.parent_id is None and n.visible)
  z=w.document.zoom;x=w.document.canvas_x
  assert x+frame.x*z>=15
  assert x+(frame.x+frame.width)*z<=w.canvas.get_width()-15
 if folder:=os.environ.get('LUMA_AUDIT_OUTPUT'):
  from gi.repository import Gtk, Graphene
  paintable=Gtk.WidgetPaintable.new(w)
  settled=time.monotonic()+.5
  until(lambda:time.monotonic()>=settled)
  snapshot=Gtk.Snapshot();paintable.snapshot(snapshot,w.get_width(),w.get_height())
  node=snapshot.to_node()
  assert node is not None, "mapped Canvas capture has no render node"
  if node:
   texture=w.get_renderer().render_texture(node,Graphene.Rect().init(0,0,w.get_width(),w.get_height()))
   texture.save_to_png(str(Path(folder)/f'canvas-{width}.png'))
 print('PASS Canvas controls',width,flush=True)
w.set_default_size(360,874);until(lambda:w.get_width()==360)
w._show_documents();assert w.pages.get_visible_child_name()=='welcome'
assert not w.phone_project.get_visible()
w._new();until(lambda:w.pages.get_visible_child_name()=='editor')
assert w.phone_project.get_visible()
flush=w.flush;w.flush=lambda:False
w._show_documents();assert w.pages.get_visible_child_name()=='editor'
w.flush=flush
print('PASS Canvas Documents navigation and save-failure retention',flush=True)
w.destroy();app.quit()
