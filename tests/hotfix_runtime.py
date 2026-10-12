"""Server-only check of the actual HOTFIX identity and document workflow."""
import os
import time
from pathlib import Path
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gdk, GLib, Gtk, Graphene
from luma_canvas.application import CanvasApplication
from luma_canvas.persistence import APP_ID, DocumentStore
from luma_canvas.export import export


def settle(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('HOTFIX window did not settle')


def children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


assert APP_ID == 'org.projectluma.Canvas.Hotfix'
app = CanvasApplication()
assert app.register(None)
assert app.get_application_id() == APP_ID
app.activate()
w = app.get_active_window()
settle(lambda: w.get_mapped())
w._new()
w = app.get_active_window()
settle(lambda: w.pages.get_visible_child_name() == 'editor' and w.canvas.get_width() > 0)
w.edit('Create preview shape', lambda: w.editor.create('rectangle', 20, 20, 120, 80,
                                                       name='HOTFIX check', fill='#3355cc'))
# Actual editor integration: bottom actions, corner inset, layer-focus clipboard,
# picker activation without any saved palette, and serialized menu icons.
assert w.action_bar.get_parent() is None
assert w.document_header.get_root() is None
assert w.bottom_share.get_parent() is w.creative_toolbar
assert w.creative_workspace.get_overflow() == Gtk.Overflow.HIDDEN
w._open_bottom_more()
menu = next(child for child in children(w.bottom_more) if isinstance(child, Gtk.Popover))
settle(lambda: menu.get_mapped() and menu.get_width() > 0)
assert menu.get_width() < 420, menu.get_width()
assert menu.get_height() < 500, menu.get_height()
assert not any(group.quick_actions for group in menu.registry.groups)
menu.popdown()
settle(lambda: not menu.get_mapped())
print('PASS clipped workspace and compact scrolling More without shortcut tiles')
settle(lambda: w.zoom_pill.get_width() > 0)
ok, corner = w.zoom_pill.compute_bounds(w.creative_workspace)
assert ok
assert w.creative_workspace.get_width() - corner.origin.x - corner.size.width >= 15
assert w.creative_workspace.get_height() - corner.origin.y - corner.size.height >= 19
shape = next(node for node in w.document.objects if node.name == 'HOTFIX check')
w.document.select([shape.id])
w._refresh_all()
assert not w._typing()
ctrl = Gdk.ModifierType.CONTROL_MASK
before = len(w.document.objects)
assert w._key_pressed(None, Gdk.KEY_c, 0, ctrl)
assert w._key_pressed(None, Gdk.KEY_v, 0, ctrl)
settle(lambda: len(w.document.objects) > before)
assert len(w.document.objects) == before + 1
assert w._key_pressed(None, Gdk.KEY_x, 0, ctrl)
assert len(w.document.objects) == before
assert w._key_pressed(None, Gdk.KEY_v, 0, ctrl)
settle(lambda: len(w.document.objects) == before + 1)
w._duplicate()
assert len(w.document.objects) == before + 2
w._delete_selection()
assert len(w.document.objects) == before + 1
# A text entry must keep its own clipboard shortcuts.
entry = Gtk.Entry()
w.bottom_more.set_child(entry)
entry.grab_focus()
assert not w._key_pressed(None, Gdk.KEY_c, 0, ctrl)
w.canvas.grab_focus()
w.document.select([shape.id])
w._refresh_all()
fields = [field for field, _update in w.creative_inspector._updates
          if field.get_first_child().has_css_class('lumaui-creative-swatch')]
assert fields, 'Missing color property'
swatch = fields[0].get_first_child()
swatch.emit('clicked')
settle(lambda: any(isinstance(child, Gtk.Popover) for child in children(w.canvas)))
pop = next(child for child in children(w.canvas) if isinstance(child, Gtk.Popover))
assert pop.get_visible()
from luma_canvas.paint import parse_hex, solid
pop.picker._emit("begin")
pop.picker.set_paint(solid(parse_hex("#ee6633")))
pop.picker._emit("change")
pop.picker._emit("end")
assert w.document.object(shape.id).fill.lower().startswith("#ee6633")
# The same popup follows canvas/layer selection, finishing the old gesture.
parent = pop.get_parent()
position = pop.get_position()
w.edit('Picker second layer', lambda:w.editor.create('rectangle',200,20,120,80,fill='#000000'))
other = w.document.selection[0]
assert pop.picker.current_paint()['color'] == parse_hex('#000000').to_dict()
w._layers_selected([shape.id])
pop.picker._emit('begin')
pop.picker.set_paint(solid(parse_hex('#ff0000')))
pop.picker._emit('change')
w._layers_selected([other.id])
assert not pop.picker._in_gesture
assert pop.get_parent() is parent and pop.get_position()==position and pop.get_visible()
assert pop.picker.current_paint()['color'] == parse_hex('#000000').to_dict()
pop.picker.set_paint(solid(parse_hex('#3366ff')));pop.picker._emit('change');pop.picker._emit('end')
assert w.document.object(shape.id).fill.lower() == '#ff0000'
assert w.document.object(other.id).fill.lower() == '#3366ff'
w._layers_selected([])
assert not pop.picker.get_sensitive()
w._layers_selected([shape.id])
assert pop.picker.get_sensitive()
assert pop.picker.current_paint()['color'] == parse_hex('#ff0000').to_dict()
print('PASS persistent picker follows selection without moving or editing previous layer',flush=True)
pop.popdown()
# Gradient endpoints remain interactive while the picker stays open.
from luma_canvas.paint import gradient, Color
w.edit('Gradient test', lambda: w.editor.set_paint([shape.id], 'fill', gradient('linear',
    [(0, Color(0, .6, 1)), (1, Color(1, 0, 1))])))
w._refresh_all()
fields = [field for field, _ in w.creative_inspector._updates
          if field.get_first_child().has_css_class('lumaui-creative-swatch')]
assert fields[0].get_last_child().get_text() == 'Linear'
fields[0].get_first_child().emit('clicked')
settle(lambda: any(isinstance(child, Gtk.Popover) and child.get_visible() for child in children(w.canvas)))
pop = next(child for child in children(w.canvas) if isinstance(child, Gtk.Popover) and child.get_visible())
assert not pop.get_autohide()
points = w.canvas._gradient_handle_points(w.canvas.view)[0][1]
assert w.canvas._handle_at(*points[0])[0] == 'gradient'
w.canvas._press = {'handle': (shape.id, 0)}
w.canvas.gradient_editor['begin']()
new_point = w.canvas.world_to_view(shape.x + shape.width * .25, shape.y + shape.height * .2)
w.canvas._gradient_to(*new_point, Gdk.ModifierType.ALT_MASK)
w.canvas.gradient_editor['end']()
handles = shape.fill_gradient['handles']
assert abs(handles[0][0] - .25) < .001 and abs(handles[0][1] - .2) < .001
# Corner attraction is bounded in screen space; Alt leaves exact placement free.
near = w.canvas.world_to_view(shape.x + 1 / w.document.zoom,
                              shape.y + 1 / w.document.zoom)
w.canvas._gradient_to(*near, Gdk.ModifierType(0))
assert shape.fill_gradient['handles'][0] == [0, 0]
w.canvas._gradient_to(*near, Gdk.ModifierType.ALT_MASK)
assert shape.fill_gradient['handles'][0][0] > 0
print('PASS gradient endpoint corner snap and Alt bypass')
pop.popdown()
# Actual + action opens the shared chooser (including required anchor width).
w.creative_inspector.effects_add_button.emit("clicked")
assert w.effect_menu is not None
w.effect_menu.close()
# Effect fields address their own stack entry and survive remove/undo.
w.edit('Effects test', lambda: w.editor.set_property([shape.id], 'effects',
    [{'type': 'drop_shadow', 'blur': 12}, {'type': 'layer_blur', 'blur': 8}]))
w._refresh_all()
w.creative_inspector._effect_opacity(shape, 0, 50)
assert shape.effects[0]['color'].endswith('80')
blur_field = next(field for field, _ in w.creative_inspector._updates
                  if isinstance(field.get_first_child(), Gtk.Label)
                  and field.get_first_child().get_label() == 'Blur')
controllers = blur_field.get_first_child().observe_controllers()
drag = next(controllers.get_item(i) for i in range(controllers.get_n_items())
            if isinstance(controllers.get_item(i), Gtk.GestureDrag))
drag.emit('drag-begin', 0., 0.)
drag.emit('drag-update', 10., 0.)
assert shape.effects[0]['blur'] > 12
print('PASS effect opacity and numeric label scrubbing')
w.creative_inspector._effect(shape, 1, blur=20)
assert shape.effects[0]['blur'] > 12 and shape.effects[1]['blur'] == 20
w.creative_inspector._remove_effect(shape, 0)
assert len(shape.effects) == 1 and shape.effects[0]['type'] == 'layer_blur'
old_page = w.document.current_page_id
w._new_page()
assert len(w.document.objects) == 1
assert (w.document.objects[0].width, w.document.objects[0].height) == (1920, 1080)
settle(lambda: w.creative_tree.get_height() < 50)
list_box = w.creative_tree.get_first_child()
row = next(child for child in children(list_box) if isinstance(child, Gtk.ListBoxRow))
ok, row_bounds = row.compute_bounds(w.creative_left)
assert ok
assert w.creative_left.get_height() - row_bounds.origin.y - row_bounds.size.height <= 12
print('PASS one-layer panel fits actual row', flush=True)
w.creative_right.set_page('export')
w._creative_inspector_page('export')
settle(lambda: w.creative_right.get_height() < 300)
for widget in (w.creative_tree, w.creative_export):
    panel = w.creative_left if widget is w.creative_tree else w.creative_right
    ok, bounds = widget.compute_bounds(panel)
    assert ok
    gap = panel.get_height() - bounds.origin.y - bounds.size.height
    assert 0 <= gap <= 12, (widget.get_css_name(), gap)
    print('BOTTOM GAP', widget.get_css_name(), gap, flush=True)
w.creative_right.set_page('design')
empty_frame = w.document.objects[0]
w.edit('Empty page check', lambda: w.editor.delete([empty_frame.id]))
settle(lambda: w.creative_tree.get_height() < 60)
list_box = w.creative_tree.get_first_child()
def descendants(widget):
    for child in children(widget):
        yield child
        yield from descendants(child)
empty = next(child for child in descendants(list_box) if isinstance(child, Gtk.Label) and child.get_label() == 'No layers yet')
settle(lambda: empty.get_mapped())
assert w.creative_left.get_height() < 160
print('PASS compact empty Layers message', flush=True)
w.document.show_page(old_page)
w._refresh_all()
w.canvas._press = {}
# A chooser must be invoked directly from the Image tool without a canvas click.
from unittest.mock import patch
with patch.object(w, '_choose_image_at') as chooser:
    w._creative_tool('image')
    chooser.assert_called_once()
frame = next(item for item in w.document.objects if item.kind == 'frame')
w.canvas.begin_rename(frame)
w.canvas._rename.set_text('Renamed outside')
root, outside = w.canvas._rename_outside
outside.emit('pressed', 1, 1.0, 1.0)
settle(lambda: w.canvas._rename is None)
assert frame.name == 'Renamed outside'
assert w.canvas._rename_outside is None
print('PASS gradient preview/endpoints, separate effects, default page frame, image chooser, outside rename', flush=True)
from luma_canvas import app_menu
def inspect_menu(menu):
    for i in range(menu.get_n_items()):
        section = menu.get_item_link(i, 'section')
        submenu = menu.get_item_link(i, 'submenu')
        if section:
            inspect_menu(section)
        else:
            assert menu.get_item_attribute_value(i, 'icon', None) is not None
            if submenu:
                inspect_menu(submenu)
inspect_menu(app_menu.build([Path('/example/test.canvas')]))
def walk(widget):
    for child in children(widget):
        yield child
        yield from walk(child)
identity = next(child for child in walk(w.title_bar) if isinstance(child, Gtk.MenuButton)
                and child.has_css_class("luma-identity-button"))
identity.popup()
settle(lambda: identity.get_popover().get_mapped())
images = [child for child in walk(identity.get_popover()) if isinstance(child, Gtk.Image)
          and ((child.get_icon_name() or "").startswith("lumaui-") or child.get_gicon() is not None)]
assert len([image for image in images if image.get_mapped()]) >= 14, "Menu icons hidden"
for image in images:
    names = image.get_gicon().get_names() if image.get_gicon() is not None else [image.get_icon_name()]
    assert any(Gtk.IconTheme.get_for_display(w.get_display()).has_icon(name) for name in names), names
identity.popdown()
print('PASS bottom actions/insets, layer clipboard/cut/duplicate/delete, text focus, picker/menu icons', flush=True)
assert w.flush(), 'HOTFIX save failed'
path = w.session.path
assert path and path.exists()
assert str(path).startswith(os.environ['DEV_ARTIFACT_DIR'])
loaded = DocumentStore.load(path)
assert any(node.name == 'HOTFIX check' for node in loaded.objects)
for fmt in ('png', 'svg', 'pdf'):
    target = Path(os.environ['DEV_ARTIFACT_DIR']) / ('export.' + fmt)
    export(loaded, target, fmt=fmt)
    assert target.stat().st_size > 0
# The library indexes 1,000 files without opening their document packages and
# binds fewer cards than files. Synthetic packages are never delivered to Dell.
from unittest.mock import patch
from luma_canvas.library_store import saved_documents
folder = path.parent
for index in range(1000):
    (folder / f"Library check {index:04}.canvas").touch()
with patch('luma_canvas.document_library.DocumentLibrary._thumbnail'):
    w._open()
    library = w.document_library
    settle(lambda: library._ready and library.store.get_n_items() == 1001)
    assert w.pages.get_visible_child_name() == 'documents'
    assert library.grid.view is not None
    assert library.actions.get_parent() is library.library_host
    assert library.view_choice.segments
    assert library.search is library.search_item.entry
    assert len(library.grid._tiles) < 1000, len(library.grid._tiles)
    library.search.set_text('Library check 0731')
    library._filter()
    assert library.model.get_n_items() == 1
    item = library.model.get_item(0)
    library.organization.starred.add(item.id)
    library._persist()
    library.search.set_text('')
    library.scope.set_selected(2)
    library._filter()
    assert library.model.get_n_items() == 1
    library.organization.projects['test-project'] = 'Test project'
    library._assign(item, 'test-project')
    library._scope = 'test-project'
    library._refresh_scopes()
    library._filter()
    assert library.model.get_n_items() == 1
    library._scope = 'all'
    library._refresh_scopes()
    library.view_choice.set_selected(1)
    assert library.views.get_visible_child_name() == 'list'
    w._open_saved_design(path)
    assert w.pages.get_visible_child_name() == 'editor'
assert path.exists()
current = next(library.store.get_item(index) for index in range(library.store.get_n_items())
               if library.store.get_item(index).data.path == path)
library._thumbnail(current)
settle(lambda: current.picture is not None)
assert Path(current.picture).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
print('PASS actual saved-design thumbnail generated asynchronously', flush=True)
print('PASS 1,001-file virtual library/search/star/project/list/current-document return', flush=True)
# Core design workflows run through the actual native inspector controls.
from luma_canvas.layout import AutoLayout
from luma_canvas.components import make_component
from luma_canvas.resources import Resources
from luma_canvas.prototype import connect
from luma_canvas.design_controls import PrototypePreview
frame = next(item for item in w.document.objects if item.kind == 'frame')
w.edit('Layout integration',lambda:w.editor.set_property([frame.id],'layout',AutoLayout(direction='horizontal').write()))
w.document.select([frame.id]);w._refresh_all()
assert w.prototype_panel.get_root() is w
w.creative_right.set_page('design')
w.resource_panel.variable_form()
assert w.resource_panel.form_handle.is_open
w.resource_panel.form_handle.close()
resources=Resources(w.editor)
w.edit('Variable integration',lambda:resources.set_variable('Canvas width',2048,'number'))
w.edit('Bind width',lambda:resources.bind_variable(frame,'width','Canvas width'))
assert frame.width==2048
w.edit('Component integration',lambda:make_component(w.document,frame))
instance=next(item for item in w.document.objects if item.component_source_id==frame.id)
w.document.select([instance.id]);w._refresh_all()
style=resources.create_style(instance,'Preview accent','color')
resources.update_style(style.id,'Preview accent',{'value':'#336699'})
assert instance.fill=='#336699'
destination=w.editor.create('frame',frame.x+frame.width+100,frame.y,1920,1080,name='Destination')
w.edit('Link integration',lambda:connect(w.document,instance,destination,transition='none'))
w.prototype_panel.refresh();w.creative_right.set_page('prototype')
preview=PrototypePreview(w)
settle(lambda:preview.window.get_mapped() and preview.canvas.get_width()>0)
original=w.document.to_dict()
preview.navigate(instance.id)
assert preview.presentation.current_id==destination.id
assert w.document.to_dict()==original
preview.window.close()
w.document.validate()
assert w.flush()
w.creative_right.set_page('design')
w.document.select([frame.id]);w._refresh_all()
settle(lambda:w.creative_right.get_mapped())
if os.environ.get('DEV_ARTIFACT_DIR'):
    ready=[]
    GLib.timeout_add(200,lambda:(ready.append(True),False)[1])
    settle(lambda:bool(ready))
    for control in descendants(w.creative_inspector):
        if control.get_tooltip_text() in {'Remove auto layout','Insert instance','Add fill'}:
            print('CONTROL',control.get_tooltip_text(),control.get_width(),control.get_height(),control.get_mapped(),flush=True)
    snapshot=Gtk.Snapshot()
    Gtk.WidgetPaintable.new(w).snapshot(snapshot,w.get_width(),w.get_height())
    node=snapshot.to_node()
    if node:w.get_renderer().render_texture(node,None).save_to_png(os.path.join(os.environ['DEV_ARTIFACT_DIR'],'core-design.png'))
print('PASS core native layout/component/paint/resource/prototype integration',flush=True)
assert w.get_renderer() is not None
assert w.flush()
w.close()
app.quit()
print('PASS HOTFIX native launch/create/save/reopen/PNG/SVG/PDF export and profile isolation', flush=True)
