"""Server-only narrow-window workflow checks and review screenshots."""
import os,time
from pathlib import Path
import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gtk,GLib
from luma_canvas.application import CanvasApplication


def settle(predicate):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
        if predicate():return
        time.sleep(.02)
    raise AssertionError('Phone window did not settle')


def pause():
    done=[];GLib.timeout_add(250,lambda:(done.append(True),False)[1]);settle(lambda:bool(done))


def capture(window,name):
    pause();snapshot=Gtk.Snapshot();Gtk.WidgetPaintable.new(window).snapshot(snapshot,window.get_width(),window.get_height())
    node=snapshot.to_node()
    if node:window.get_renderer().render_texture(node,None).save_to_png(str(Path(os.environ['DEV_ARTIFACT_DIR'])/name))

app=CanvasApplication();assert app.register(None)
import sys,traceback
callback_errors=[]
def callback_error(kind,error,trace):
    detail=''.join(traceback.format_exception(kind,error,trace));callback_errors.append(detail)
    print(detail,flush=True)
    (Path(os.environ['DEV_ARTIFACT_DIR'])/'callback-exception.txt').write_text(detail)
sys.excepthook=callback_error
app.activate()
w=app.get_active_window();w._new();w=app.get_active_window()
settle(lambda:w.canvas.get_width()>0)
for width,height in ((390,844),(320,640)):
    w.set_default_size(width,height)
    settle(lambda:getattr(w,'_phone_ui',False))
    pause()
    print('PHONE SIZE',width,w.get_width(),w.get_height(),flush=True)
    if w.get_width()>width+4:
        capture(w,f'phone-{width}-overflow.png')
    assert w.get_width()<=width+4
    frame=next(item for item in w.document.objects if item.kind=='frame')
    w.document.select([frame.id]);w._refresh_all()
    w._phone_grow('design');pause()
    capture(w,f'phone-{width}-design.png')
    assert w.creative_inspector.get_mapped()
    assert w.phone_actions.bar.get_height()<w.get_height()*.48
    assert w.canvas.get_height()>w.get_height()*.28
    assert not w.phone_export.get_visible()
    assert w.phone_actions.head_row.get_visible() is False
    assert w._phone_expand.get_width()>=44 and w._phone_expand.get_height()>=44
    collapsed=w.phone_actions.bar.get_height()
    w._phone_expand_panel();pause()
    assert w.phone_actions.bar.get_height()>collapsed
    print('EXPANDED GEOMETRY',width,w.get_height(),w.canvas.get_height(),w.canvas.get_margin_bottom(),w.creative_workspace.get_height(),w.phone_actions.bar.get_height(),w._phone_scroller.get_max_content_height(),flush=True)
    capture(w,f'phone-{width}-expanded.png')
    assert w.canvas.get_height()>w.get_height()*.25
    w._phone_expand_panel();pause()
    field=w.creative_inspector._updates[0][0]
    assert field.get_height()>=44,field.get_height()
    def focus_entry(widget):
        if isinstance(widget,Gtk.Editable):
            widget.grab_focus();return True
        child=widget.get_first_child()
        while child:
            if focus_entry(child):return True
            child=child.get_next_sibling()
        return False
    assert focus_entry(field)
    assert w.creative_inspector._focused(field)
    parent=w.creative_inspector.get_parent()
    w.edit('Phone field edit',lambda:w.editor.set_geometry(frame.id,x=frame.x+1))
    assert w.creative_inspector.get_parent() is parent
    assert w.creative_inspector._focused(field)
    from luma_canvas.color_picker import open_color_popover
    from luma_canvas.paint import solid,parse_hex
    picker=open_color_popover(field,solid(parse_hex('#66aaff')),lambda *_:None,canvas=w.canvas)
    pause()
    assert picker.get_mapped()
    assert w._phone_color is picker and w._phone_panel=='color'
    print('COLOR GEOMETRY',width,w.canvas.get_height(),w.phone_actions.bar.get_height(),w._phone_scroller.get_height(),flush=True)
    assert w.canvas.get_height()>w.get_height()*.28
    assert w.phone_actions.bar.get_height()<w.get_height()*.60
    assert picker.picker.hue_slider.get_height()>=44
    assert picker.picker.alpha_slider.get_height()>=44
    assert picker.picker.get_width()<=w.get_width()-16
    assert picker.picker.get_height()<=w.get_height()-48
    capture(w,f'phone-{width}-picker.png')
    picker.popdown();pause()
    w._phone_grow('prototype');pause()
    assert w.prototype_panel.get_mapped()
    capture(w,f'phone-{width}-prototype.png')
    def descendants(widget):
        yield widget
        child=widget.get_first_child()
        while child:
            yield from descendants(child);child=child.get_next_sibling()
    play=next(widget for widget in descendants(w.phone_actions) if isinstance(widget,Gtk.Button) and widget.get_tooltip_text()=='Preview selected frame')
    assert play.get_width()>=44 and play.get_height()>=44
    scroll=w._phone_scroller.get_vadjustment()
    scroll.set_value(scroll.get_upper()-scroll.get_page_size());pause()
    capture(w,f'phone-{width}-prototype-bottom.png')
    w._phone_close_panel();pause()
    assert w.document.selection and w.document.selection[0].id==frame.id
    assert w.phone_actions.bar.get_height()<100
    w._phone_grow('prototype')
    from luma_canvas.design_controls import PrototypePreview
    preview=PrototypePreview(w)
    settle(lambda:preview.window.get_mapped() and preview.canvas.get_width()>0)
    pause()
    print('PREVIEW SIZE',width,preview.window.get_width(),flush=True)
    assert preview.window.get_width()<=width+4
    view=preview.canvas.view
    assert view.x+frame.width*view.zoom<=preview.canvas.get_width()+1
    assert not preview.canvas.document.selection
    capture(preview.window,f'phone-{width}-preview.png')
    preview.window.close();pause()
    w._phone_grow('tools');pause()
    assert w._phone_panel=='tools'
    assert w.phone_actions.bar.get_height()<w.get_height()*.5
    tools_scroll=w._phone_scroller.get_vadjustment()
    tools_scroll.set_value(tools_scroll.get_upper()-tools_scroll.get_page_size());pause()
    capture(w,f'phone-{width}-tools-bottom.png')
    assert any(widget.get_tooltip_text()=='Close panel' and widget.get_mapped() for widget in descendants(w.phone_actions) if isinstance(widget,Gtk.Button))
    w._phone_close_panel()
    w._phone_deselect();w._phone_grow('layers');w._phone_layers_page('assets');pause()
    assert w.resource_panel.get_mapped()
    w.resource_panel.variable_form();pause()
    assert w.resource_panel.form_handle.is_open
    assert w.get_width()<=width+4
    capture(w,f'phone-{width}-variable.png')
    w.resource_panel.form_handle.close()
    w._phone_grow('layers')
    w._show_documents();pause()
    print('LIBRARY STATE',w.pages.get_visible_child_name(),w.session.state,w.session.error,flush=True)
    assert w.pages.get_visible_child_name()=='documents'
    assert w.get_width()<=width+4
    capture(w,f'phone-{width}-library.png')
    assert w.document_library.get_width()<=w.get_width()
    lib=w.document_library
    found,body=lib.views.compute_bounds(lib.library_host)
    found_bar,bar=lib.actions.bar.compute_bounds(lib.library_host)
    print('LIBRARY VIEWPORT',body.get_y(),body.get_height(),lib.views.get_height(),lib.views.get_margin_bottom(),bar.get_y(),bar.get_height(),flush=True)
    assert found and found_bar
    grid=lib.grid.view.get_parent()
    ok,viewport=grid.compute_bounds(lib.library_host)
    print('GRID VIEWPORT',viewport.get_y(),viewport.get_height(),flush=True)
    assert ok and viewport.get_y()+grid.get_height()<=bar.get_y()+2,(viewport.get_y(),grid.get_height(),bar.get_y())
    more=next(widget for widget in descendants(lib.actions) if isinstance(widget,Gtk.Button) and widget.get_tooltip_text()=='Collections and view')
    more.emit('clicked');pause()
    assert lib.actions.grown=='library-options'
    assert lib.actions.bar.get_height()<w.get_height()*.62
    options_scroll=lib._options_scroller.get_vadjustment()
    options_scroll.set_value(options_scroll.get_upper()-options_scroll.get_page_size());pause()
    capture(w,f'phone-{width}-library-options.png')
    lib.actions.fold();pause()
    w.document_library.view_choice.set_selected(1);pause()
    capture(w,f'phone-{width}-list.png')
    assert w.document_library.get_width()<=w.get_width()
    lib=w.document_library
    found,body=lib.views.compute_bounds(lib.library_host)
    found_bar,bar=lib.actions.bar.compute_bounds(lib.library_host)
    print('LIBRARY VIEWPORT',body.get_y(),body.get_height(),lib.views.get_height(),lib.views.get_margin_bottom(),bar.get_y(),bar.get_height(),flush=True)
    assert found and found_bar
    grid=lib.grid.view.get_parent()
    ok,viewport=grid.compute_bounds(lib.library_host)
    print('GRID VIEWPORT',viewport.get_y(),viewport.get_height(),flush=True)
    assert ok and viewport.get_y()+grid.get_height()<=bar.get_y()+2,(viewport.get_y(),grid.get_height(),bar.get_y())
    w.document_library.view_choice.set_selected(0)
    w._new();w=app.get_active_window()
print('PASS phone 320/390 design/prototype/assets/resource sheets',flush=True)
w.set_default_size(1120,760);settle(lambda:not getattr(w,'_phone_ui',True));pause()
assert w.creative_inspector.get_root() is w
assert w.prototype_panel.get_root() is w
assert w.resource_panel.get_root() is w
print('PASS return to desktop without losing panel ownership',flush=True)
w.close();app.quit();assert not callback_errors,callback_errors
