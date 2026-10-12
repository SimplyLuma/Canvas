"""Real exported prototype acceptance: native render nodes and actual hit regions."""
import os,time,json,copy,hashlib
from pathlib import Path
import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gtk,GLib
from luma_canvas.fig import import_fig
from luma_canvas.application import CanvasApplication
from luma_canvas.prototype_preview import PrototypePreview
from luma_canvas.scene import rect


def settle(predicate,timeout=12):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
        if predicate():return
        time.sleep(.005)
    raise AssertionError('Real prototype did not settle')


def pause():
    done=[];GLib.timeout_add(180,lambda:(done.append(True),False)[1]);settle(lambda:done)

result=import_fig(Path(os.environ['CANVAS_REAL_FIG']))
assert result.document.connections,'No functional prototype connections imported'
app=CanvasApplication();assert app.register(None);app.activate();w=app.get_active_window()
w._new();w=app.get_active_window()
w.import_file(Path(os.environ['CANVAS_REAL_FIG']))
settle(lambda:len(w.document.connections)==len(result.document.connections),timeout=30)
assert w.document.selection and w.document.selection[0].metadata.get('prototype_start')
if hasattr(w,'get_visible_dialog') and w.get_visible_dialog():w.get_visible_dialog().close()
print('PASS actual asynchronous Import UI retains connections and opens the flow page',flush=True)
import_window=w
assert app.open_path(Path(os.environ['CANVAS_REAL_FIG']))
settle(lambda:any(candidate is not import_window and len(candidate.document.connections)==len(result.document.connections) for candidate in app.get_windows()),timeout=30)
w=next(candidate for candidate in app.get_windows() if candidate is not import_window and len(candidate.document.connections)==len(result.document.connections))
assert w.document.selection and w.document.selection[0].metadata.get('prototype_start')
if hasattr(w,'get_visible_dialog') and w.get_visible_dialog():w.get_visible_dialog().close()
print('PASS actual Open .fig retains interactions and focuses the starting frame',flush=True)
import_window.close()
settle(lambda:w.get_mapped());pause();settle(lambda:w.canvas._animation is None)
original=w.document.to_dict();preview=PrototypePreview(w);surface=preview.canvas
settle(lambda:preview.window.get_mapped() and surface.get_width()>0);pause()
assert surface.presentation.frame.metadata.get('prototype_start'),surface.presentation.frame.name
start=surface.presentation.frame.name
artifact=Path(os.environ['DEV_ARTIFACT_DIR']);frames=[]

def render(node,name):
    texture=preview.window.get_renderer().render_texture(node,rect(0,0,*surface.viewport))
    destination=artifact/(name+'.png');texture.save_to_png(str(destination))
    return hashlib.sha256(destination.read_bytes()).hexdigest()

render(surface.screen_node(),'real-flow-start')
# Follow the user route without teleporting to each source frame. Use the
# preview's actual press/release path, then prove inherited Home works again.
def click_named(name,destination):
    source,link=next((o,l) for o,l in surface.presentation.hotspots() if o.name==name and l.trigger=='click')
    x,y=surface.hotspot_local(source);v=surface.local_view()
    px,py=v.to_view(x+source.width/2,y+source.height/2)
    preview.pressed(None,1,px,py);preview.released(None,1,px,py)
    settle(lambda:surface.active_link is None)
    assert surface.presentation.frame.name==destination,(name,surface.presentation.frame.name)
click_named('Buttton','Homepage')
cards=next(o for o in surface.document.descendants(surface.presentation.frame.id) if o.name=='cards' and o.metadata.get('prototype_scroll')=='horizontal')
v=surface.local_view();cx,cy=surface.hotspot_local(cards);px,py=v.to_view(cx+50,cy+80)
initial=render(surface.screen_node(),'real-cards-before-scroll')
preview.motion(None,px,py)
preview.scrolled(None,0,5)
assert surface.scroll_positions[cards.id][0]>0
assert render(surface.screen_node(),'real-cards-after-scroll')!=initial
# A fully clipped card cannot be clicked through the frame's scrolled viewport.
source,link=next((o,l) for o,l in surface.presentation.hotspots() if o.name=='category-card')
sx,sy=surface.hotspot_local(source)
assert surface.hit(*v.to_view(sx+1,sy+source.height/2),'click') is None
surface.scroll_by(-10000,0,px,py)
click_named('category-card','Exercise page')
details=next(o for o in surface.document.descendants(surface.presentation.frame.id) if o.metadata.get('prototype_scroll')=='vertical' and o.id!=surface.presentation.frame.id)
v=surface.local_view();cx,cy=surface.hotspot_local(details);px,py=v.to_view(cx+details.width/2,cy+details.height/2)
preview.pressed(None,1,px,py)
preview.drag_begin(None,px,py)
preview.drag_update(None,0,-140*v.zoom)
preview.drag_end()
preview.released(None,1,px,py-140*v.zoom)
assert surface.scroll_positions.get(details.id,(0,0))[1]>0,details.name
click_named('home icon','Homepage')
click_named('exercise icon','Activity Page')
click_named('home icon','Homepage')
print('PASS continuous real clicks onboarding/home/exercise/activity/home, nested card/detail scrolling and clipped hit regions',flush=True)
surface.presentation.current_id=surface.presentation.start_id;surface.presentation.history=[];surface.scroll_positions.clear();surface.invalidate()
encoded_links=list(surface.document.connections)
for index,link in enumerate(encoded_links):
    source=surface.document.object(link.source_id)
    roots=[item for item in [source,*surface.document.ancestors(source.id)] if item.kind in {'frame','component','instance','page'}]
    frame=next((item for item in reversed(roots) if item.parent_id is None),roots[-1] if roots else None)
    assert frame is not None,(source.name,link.id)
    surface.finish();preview.cancel_timers();surface.presentation.current_id=frame.id
    surface.presentation.history=[];surface.presentation.overlays=[]
    before=render(surface.screen_node(),'real-'+str(index)+'-before')
    allocation=(surface.get_width(),surface.get_height(),surface.local_view())
    x,y=surface.hotspot_local(source);view=surface.local_view();center=view.to_view(x+source.width/2,y+source.height/2)
    hit=surface.hit(*center,link.trigger)
    # Click centers must resolve a functional hotspot; the deepest valid child wins.
    assert hit is not None,(source.name,frame.name,center,source.x,source.y)
    assert hit[1].destination_id==link.destination_id,(source.name,hit[1].destination_id,link.destination_id)
    started=time.monotonic();active=surface.navigate(source.id,link.trigger,link.id)
    assert active is not None and active.destination_id==link.destination_id
    midpoint=render(surface.transition_node(.5),'real-'+str(index)+'-midpoint') if surface.active_link else before
    assert (surface.get_width(),surface.get_height())==allocation[:2]
    settle(lambda:surface.active_link is None)
    after=render(surface.screen_node(),'real-'+str(index)+'-after')
    assert surface.presentation.current_id==link.destination_id
    frames.append({'source':source.name,'frame':frame.name,'destination':surface.presentation.frame.name,
        'trigger':link.trigger,'transition':link.transition,'duration_ms':link.duration_ms,'preparation_and_play_seconds':round(time.monotonic()-started,3),
        'before_sha':before,'midpoint_sha':midpoint,'after_sha':after})
    print('PASS real interaction',json.dumps(frames[-1]),flush=True)
assert w.document.to_dict()==original,'Preview changed imported authoring document'
(artifact/'real-playback.json').write_text(json.dumps({'start':start,'count':len(frames),'interactions':frames},indent=2))
preview.window.close();w.close();app.quit()
print('PASS real exported .fig native rendering/hotspots/all destinations/animations/source isolation',flush=True)
