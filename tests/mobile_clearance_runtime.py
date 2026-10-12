"""Phone clearance, live selection, last-row access and compact landscape."""
import os,time,sys,traceback
from pathlib import Path
import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gtk,GLib
from luma_canvas.application import CanvasApplication
from luma_canvas.model import LayoutObject
from luma_canvas.color_picker import open_paint_picker


def settle(predicate,timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
        if predicate():return
        time.sleep(.02)
    raise AssertionError('Compact layout did not settle')


def pause():
    done=[];GLib.timeout_add(500,lambda:(done.append(True),False)[1]);settle(lambda:done)


def descendants(widget):
    yield widget
    child=widget.get_first_child()
    while child:
        yield from descendants(child);child=child.get_next_sibling()


def capture(w,name):
    shot=Gtk.Snapshot();Gtk.WidgetPaintable.new(w).snapshot(shot,w.get_width(),w.get_height())
    w.get_renderer().render_texture(shot.to_node(),None).save_to_png(str(Path(os.environ['DEV_ARTIFACT_DIR'])/(name+'.png')))

app=CanvasApplication();assert app.register(None);app.activate();w=app.get_active_window()
w._new();w=app.get_active_window()
errors=[]
def callback_error(kind,error,trace):
    message=''.join(traceback.format_exception(kind,error,trace));errors.append(message);print(message,flush=True)
sys.excepthook=callback_error
red=LayoutObject(id='mobile-red',kind='rectangle',name='Red button',x=60,y=80,width=120,height=80,fill='#ff0000')
black=LayoutObject(id='mobile-black',kind='rectangle',name='Black button',x=260,y=80,width=120,height=80,fill='#000000')
w.edit('Add phone acceptance buttons',lambda:w.document.objects.extend([red,black]))
for width,height in ((320,480),(390,640),(640,360),(320,640)):
    w.set_default_size(width,height);pause()
    assert w._phone_ui,(width,height)
    w.document.select([red.id]);w._refresh_all();w._phone_grow('design');pause()
    assert w.canvas.get_height()>=80 and w.canvas.get_width()>=140,(width,height,w.canvas.get_width(),w.canvas.get_height())
    ok,canvas_bounds=w.canvas.compute_bounds(w.creative_workspace)
    ok_bar,bar_bounds=w.phone_actions.bar.compute_bounds(w.creative_workspace)
    assert ok and ok_bar and canvas_bounds.get_y()+w.canvas.get_height()<=bar_bounds.get_y()+2,(width,height,canvas_bounds,bar_bounds)
    if width>height:
        assert w._phone_side is not None
        ok_side,side_bounds=w._phone_side.compute_bounds(w.creative_workspace)
        assert ok_side and canvas_bounds.get_x()+w.canvas.get_width()<=side_bounds.get_x()+2
    capture(w,f'compact-{width}x{height}-design')
    w._phone_close_panel();pause()
    picker=open_paint_picker(w._phone_tool_buttons['rectangle'],w.canvas)
    pause();assert picker.picker.fields[0].entry.get_text().upper()=='FF0000'
    before=picker.compute_bounds(w)[1]
    w.document.select([black.id]);w.canvas.emit('selection-changed');pause()
    assert w._phone_color is picker
    assert picker.picker.fields[0].entry.get_text().upper()=='000000'
    after=picker.compute_bounds(w)[1]
    assert abs(before.get_x()-after.get_x())<1 and abs(before.get_y()-after.get_y())<1
    picker.picker._single(lambda:picker.picker.set_alpha(.5));pause()
    assert w.document.object(red.id).opacity==red.opacity
    assert w.document.object(black.id).fill!=w.document.object(red.id).fill
    adjustment=picker.scroller.get_vadjustment();adjustment.set_value(adjustment.get_upper()-adjustment.get_page_size());pause()
    assert any(button.get_tooltip_text()=='Close' and button.get_mapped() for button in descendants(picker) if isinstance(button,Gtk.Button))
    capture(w,f'compact-{width}x{height}-picker-bottom')
    picker.popdown();pause()
    w._phone_grow('tools');pause()
    buttons=[item for item in descendants(w._phone_side or w.phone_actions) if isinstance(item,Gtk.Button)]
    duplicate=next(button for button in buttons if any(isinstance(label,Gtk.Label) and label.get_text()=='Duplicate' for label in descendants(button)))
    count=len(w.document.objects);duplicate.emit('clicked');pause()
    assert len(w.document.objects)==count+1
    w._undo();pause();assert len(w.document.objects)==count
    w.document.validate();w._phone_close_panel();pause()
    print('PASS compact clearance/live picker/scroll/actions',width,height,flush=True)
w.set_default_size(1120,760);pause()
assert not w._phone_ui and w.canvas.get_margin_top()==w.canvas.get_margin_bottom()==w.canvas.get_margin_end()==0
assert w.creative_inspector.get_root() is w and w.prototype_panel.get_root() is w
assert not errors,errors
w.close();app.quit()
print('PASS compact rotation and return to desktop restores ownership and camera viewport',flush=True)
