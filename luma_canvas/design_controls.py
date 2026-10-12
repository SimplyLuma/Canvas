# SPDX-License-Identifier: Apache-2.0
"""Contextual design workflows composed from shared Luma property controls."""
from dataclasses import replace
from gi.repository import Gtk, GLib
from luma_appkit import BarAction
from luma_appkit.action_center import make_control
from .layout import AutoLayout
from .paint import paint_stack, paint_label, paint_opacity, paint_color, solid, parse_hex, with_paint_opacity


def action(icon, tooltip, callback):
    button=make_control(BarAction(icon, tooltip=tooltip, on_activate=callback), size='tool')
    button.add_css_class('lumaui-creative-section-action')
    return button


def header_actions(section, *controls):
    header = section.get_first_child()
    for control in controls:
        header.append(control)
    return section


def color_field(host, label, value, change):
    from luma_appkit import PropertyColor
    from .color_picker import open_color_popover
    from .paint import to_hex, Color
    state={'value':value}
    def update(color):
        state['value']=color;change(color)
    def pick(anchor):
        def apply(paint, phase):
            if phase=='change' and paint:
                color=paint_color(paint);update(to_hex(color))
                field.set_rgba(host.creative_inspector._rgba(state['value']))
        open_color_popover(anchor,solid(parse_hex(state['value'])),apply,canvas=host.canvas,title=label,allow_gradients=False)
        return True
    field=PropertyColor(label,host.creative_inspector._rgba(value),
        on_change=lambda rgba:update(to_hex(Color(rgba.red,rgba.green,rgba.blue,rgba.alpha))),on_pick=pick)
    return field


def layout_controls(inspector, item):
    host = inspector.host
    parent = host.document.object(item.parent_id) if item.parent_id else None
    def set_property(key, value, label):
        host.edit(label, lambda: host.editor.set_property([item.id], key, value), coalesce=True)
    if parent is not None:
        if AutoLayout.read(parent):
            inspector.append(inspector.PropertySection('Resize in layout', inspector._choice(
                'Main axis', [('fixed', 'Fixed'), ('fill', 'Fill container')],
                'fill' if item.grow else 'fixed', lambda: 'fill' if item.grow else 'fixed',
                lambda value: set_property('grow', 1 if value == 'fill' else 0, 'Change child resizing'))))
        else:
            controls = []
            for axis, choices in (('horizontal', [('left','Left'),('right','Right'),('left-right','Left and right'),('centre','Center'),('scale','Scale')]),
                                  ('vertical', [('top','Top'),('bottom','Bottom'),('top-bottom','Top and bottom'),('centre','Center'),('scale','Scale')])):
                default = choices[0][0]
                controls.append(inspector._choice(axis.title(), choices, item.constraints.get(axis, default),
                    lambda a=axis, d=default: item.constraints.get(a, d),
                    lambda value, a=axis: set_property('constraints', {**item.constraints, a:value}, 'Change constraints')))
            inspector.append(inspector.PropertySection('Constraints', *controls))
    if item.kind not in {'frame', 'group', 'component', 'section', 'instance'}:
        return
    from .layout_grids import controls as grid_controls
    grid_controls(inspector,item)
    rule = AutoLayout.read(item)
    if rule is None:
        inspector.append(header_actions(inspector.PropertySection('Auto layout'), action('plus', 'Add auto layout',
            lambda: set_property('layout', {**item.layout, **AutoLayout().write()}, 'Add auto layout'))))
        return
    def change(**values):
        current = AutoLayout.read(item)
        set_property('layout', {**item.layout, **replace(current, **values).write()}, 'Change auto layout')
    controls = [inspector._choice('Direction', [('horizontal','Horizontal'),('vertical','Vertical')], rule.direction,
        lambda: AutoLayout.read(item).direction, lambda value: change(direction=value), segments=True),
        inspector._choice('Alignment', [('start','Start'),('centre','Center'),('end','End'),('stretch','Stretch')], rule.align,
        lambda: AutoLayout.read(item).align, lambda value: change(align=value)),
        inspector._number('Gap', rule.gap, lambda: AutoLayout.read(item).gap, lambda value: change(gap=value), minimum=0)]
    for axis in ('width','height'):
        def sizing(axis=axis):
            current=AutoLayout.read(item)
            main=(axis=='width')==(current.direction=='horizontal')
            return (current.main_sizing if main else current.cross_sizing) or current.sizing
        def set_sizing(value,axis=axis):
            current=AutoLayout.read(item);main=(axis=='width')==(current.direction=='horizontal')
            change(**{'main_sizing' if main else 'cross_sizing':value})
        controls.append(inspector._choice(axis.title(),[('fixed','Fixed'),('hug','Hug contents')],sizing(),sizing,set_sizing))
    controls.append(inspector._choice('Distribution', [('start','Start'),('centre','Center'),('end','End'),('space-between','Space between')],rule.primary_align,
        lambda:AutoLayout.read(item).primary_align,lambda value:change(primary_align=value)))
    for first, second in (('top','bottom'), ('left','right')):
        controls.append(tuple(inspector._number(side.title(), getattr(rule, 'padding_'+side),
            lambda s=side: getattr(AutoLayout.read(item), 'padding_'+s),
            lambda value, s=side: change(**{'padding_'+s:value}), minimum=0) for side in (first,second)))
    inspector.append(header_actions(inspector.PropertySection('Auto layout', *controls), action('minus', 'Remove auto layout',
        lambda: set_property('layout', {key:value for key,value in item.layout.items() if key != 'auto'}, 'Remove auto layout'))))


def component_controls(inspector, item):
    from .components import make_component, detach
    host = inspector.host
    def create():
        def mutate():
            instance = make_component(host.document, item)
            # Keep definition in place and the first instance alongside it.
            host.editor.move([instance.id], item.width + 40, 0)
            host.document.select([instance.component_source_id])
        host.edit('Create component', mutate)
    if item.kind == 'instance':
        source = host.document.object_on_any_page(item.component_source_id)
        def detach_instance():
            host.edit('Detach instance', lambda: detach(host.document, item))
        def select_source():
            if source:
                page=next((p for p in host.document.pages if p.id!=host.document.current_page_id and any(o["id"]==source.id for o in p.objects)),None)
                if page:host.document.show_page(page.id)
                host.document.select([source.id]); host._refresh_all(); host._zoom_to_selection()
        controls = Gtk.Box(spacing=4)
        controls.append(action('component', 'Go to main component', select_source))
        controls.append(action('rotate-ccw', 'Reset overrides', lambda: host.edit('Reset overrides',
            lambda: host.editor.reset_instance_overrides(item.id))))
        controls.append(action('link-2', 'Detach instance', detach_instance))
        inspector.append(inspector.PropertySection(source.name if source else 'Instance', controls))
    elif item.kind == 'component':
        def insert():
            def mutate():
                instance = host.editor.create_instance(item.id, item.x+item.width+40, item.y, item.parent_id)
                host.document.select([instance.id])
            host.edit('Insert instance', mutate)
        inspector.append(header_actions(inspector.PropertySection('Main component'), action('copy', 'Insert instance', insert)))
    elif item.kind not in {'guide','comment','page'}:
        inspector.append(header_actions(inspector.PropertySection('Component'), action('component', 'Create component', create)))


def paint_controls(inspector, item, target):
    from .color_picker import open_paint_picker
    host = inspector.host
    stack = paint_stack(item, target)
    section = inspector.PropertySection('Fills' if target == 'fill' else 'Strokes')
    def edit(label, callback):
        host.edit(label, callback, coalesce=True)
    def add():
        def mutate():
            host.editor.add_stack_paint([item.id], target, solid(parse_hex('#ffffff' if target == 'fill' else '#16161a')))
            if target == 'stroke' and not item.stroke_width:
                host.editor.set_property([item.id], 'stroke_width', 1)
        host.edit('Add '+target, mutate)
    header_actions(section, action('plus', 'Add '+target, add))
    # UI lists top paint first, model stores bottom paint first.
    for index in reversed(range(len(stack))):
        paint = stack[index]
        color = paint_color(paint)
        field = inspector.PropertyColor(paint_label(paint), color=inspector._rgba('#%02x%02x%02x' % color.bytes() if color else '#000000'),
            on_pick=lambda anchor, i=index: open_paint_picker(anchor, host.canvas, target, index=i))
        def update_paint(f=field, i=index):
            current = paint_stack(item,target)[i]
            c = paint_color(current)
            if c:f.set_rgba(inspector._rgba('#%02x%02x%02x' % c.bytes()))
            f.set_word(current['type'].title() if current.get('type') in {'linear','radial'} else paint_label(current))
            if current.get('type') in {'linear','radial'}:
                preview = Gtk.DrawingArea()
                def draw(_area, cr, width, height):
                    import cairo
                    from .paint import Color
                    handles = current.get('handles') or [[.5,0],[.5,1]]
                    pattern = (cairo.LinearGradient(handles[0][0]*width,handles[0][1]*height,handles[1][0]*width,handles[1][1]*height)
                        if current['type']=='linear' else cairo.RadialGradient(width/2,height/2,0,width/2,height/2,max(width,height)/2))
                    for stop in current.get('stops',[]):
                        color = Color.from_dict(stop['color']);pattern.add_color_stop_rgba(stop['position'],color.r,color.g,color.b,color.a)
                    cr.set_source(pattern);cr.paint()
                preview.set_draw_func(draw);f.set_preview(preview)
            else:f.set_preview(None)
        inspector._updates.append((field,update_paint));update_paint()
        controls = Gtk.Box(spacing=2)
        controls.append(action('eye' if paint.get('visible',True) else 'eye-off', 'Toggle paint visibility',
            lambda i=index: edit('Toggle paint visibility', lambda: host.editor.update_stack_paint([item.id], target, i,
                visible=not paint_stack(item,target)[i].get('visible',True)))))
        if index < len(stack)-1:
            controls.append(action('arrow-up', 'Move paint up', lambda i=index: edit('Reorder paint',
                lambda: host.editor.move_stack_paint([item.id],target,i,i+1))))
        if index > 0:
            controls.append(action('arrow-down', 'Move paint down', lambda i=index: edit('Reorder paint',
                lambda: host.editor.move_stack_paint([item.id],target,i,i-1))))
        controls.append(action('minus', 'Remove paint', lambda i=index: edit('Remove paint',
            lambda: host.editor.set_stack_paint([item.id],target,i,None))))
        row = inspector.PropertySection(f'{target.title()} {index+1}', field,
            inspector._number('Opacity', paint_opacity(paint)*100,
                lambda i=index: paint_opacity(paint_stack(item,target)[i])*100,
                lambda value, i=index: edit('Change paint opacity', lambda: host.editor.set_stack_paint([item.id],target,i,with_paint_opacity(paint_stack(item,target)[i],value/100))),
                minimum=0, maximum=100, unit='%'))
        header_actions(row, controls)
        section.append(row)
    if target == 'stroke' and stack:
        section.append(inspector._number('Width', item.stroke_width, lambda:item.stroke_width,
            lambda value: edit('Change stroke width', lambda:host.editor.set_property([item.id],'stroke_width',value)),minimum=0))
        section.append(inspector._choice('Position', [('center','Center'),('inside','Inside'),('outside','Outside')],item.stroke_align,
            lambda:item.stroke_align,lambda value:edit('Change stroke position',lambda:host.editor.set_property([item.id],'stroke_align',value))))
    inspector.append(section)


# Public imports retained for existing callers and task integration checks.
from .prototype_controls import PrototypePanel
from .prototype_preview import PrototypePreview


class ResourcePanel(Gtk.Box):
    def __init__(self,host):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.host=host
        self.signature=None
        self.refresh()

    def refresh(self):
        from luma_appkit import PropertySection
        document=self.host.document
        signature=repr(([(s.id,s.name,s.value) for s in document.styles],document.variables,
            [(i.id,i.name) for i in document.objects if i.kind=='component'],document.selected_ids))
        if signature==self.signature:return
        self.signature=signature
        while (child:=self.get_first_child()) is not None:self.remove(child)
        from .resources import Resources
        resources=Resources(self.host.editor)
        selection=document.selection
        subject=selection[0] if len(selection)==1 else None
        components=PropertySection('Components')
        for item in document.objects:
            if item.kind!='component':continue
            row=Gtk.Box(spacing=4)
            row.append(Gtk.Label(label=item.name,xalign=0,hexpand=True,ellipsize=3))
            def insert(source=item):
                def mutate():
                    instance=self.host.editor.create_instance(source.id,source.x+source.width+40,source.y)
                    document.select([instance.id])
                self.host.edit('Insert component',mutate)
            row.append(action('plus','Insert '+item.name,insert));components.append(row)
        if not any(item.kind=='component' for item in document.objects):
            components.append(Gtk.Label(label='Create a component from a selected layer.',wrap=True,xalign=0))
        self.append(components)
        styles=PropertySection('Styles')
        header_actions(styles,action('plus','Create style from selection',lambda:self.style_form(subject=subject)))
        for style in document.styles:
            if style.kind not in {'color','text'}:continue
            row=Gtk.Box(spacing=4);row.append(Gtk.Label(label=style.name,xalign=0,hexpand=True,ellipsize=3))
            apply=action('check','Apply '+style.name,lambda key=style.id:self.host.edit('Apply style',
                lambda:resources.apply_style(self.host.document.selection[0],key)))
            apply.set_sensitive(subject is not None and (style.kind=='color' or subject.kind=='text'))
            row.append(apply)
            row.append(action('pencil','Edit '+style.name,lambda s=style:self.style_form(style=s)))
            row.append(action('minus','Remove '+style.name,lambda key=style.id:self.host.edit('Remove style',lambda:resources.remove_style(key))))
            styles.append(row)
        self.append(styles)
        variables=PropertySection('Variables')
        header_actions(variables,action('plus','Create variable',lambda:self.variable_form()))
        for name,value in resources.variables().items():
            row=Gtk.Box(spacing=4);row.append(Gtk.Label(label=name,xalign=0,hexpand=True,ellipsize=3))
            row.append(action('pencil','Edit '+name,lambda n=name:self.variable_form(n)))
            row.append(action('link','Bind '+name,lambda n=name:self.bind_form(n)))
            row.append(action('minus','Remove '+name,lambda n=name:self.host.edit('Remove variable',lambda:resources.remove_variable(n))))
            variables.append(row)
        if not resources.variables():variables.append(Gtk.Label(label='Reuse colors, numbers and text.',wrap=True,xalign=0))
        self.append(variables)

    def form(self,title,fields,save):
        from luma_appkit import PropertySection,BarAction
        from luma_appkit.bar_frame import BarFrame
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
        if getattr(self.host,'_phone_ui',False):box.add_css_class('lumaui-creative-properties-phone')
        box.append(PropertySection(title,*fields))
        error=Gtk.Label(wrap=True,xalign=0);error.add_css_class('error');box.append(error)
        def apply():
            try:save()
            except (ValueError,KeyError) as exc:error.set_label(str(exc));return
            self.form_handle.close()
        box.append(make_control(BarAction('check','Save',on_activate=apply)))
        self.form_handle=BarFrame.present(self,box,kind='sheet',title=title,anchor_width=300,max_fraction=.5 if getattr(self.host,'_phone_ui',False) else None)

    def style_form(self,subject=None,style=None):
        from luma_appkit import TextField,PropertyChoice,PropertyNumber,PropertyColor
        from .resources import Resources
        if style is None and subject is None:
            self.host._toast('Select a layer to create a style');return
        resources=Resources(self.host.editor)
        kind=style.kind if style else ('text' if subject.kind=='text' else 'color')
        name=TextField('Name',value=style.name if style else subject.name)
        fields=[name]
        value=dict(style.value) if style else ({'value':subject.fill or '#000000'} if kind=='color' else dict(subject.metadata))
        if kind=='color':
            color=color_field(self.host,'Color',value.get('value','#000000'),lambda color:value.update(value=color));fields.append(color)
            def read():return {'value':value.get('value','#000000')}
        else:
            family=TextField('Family',value=value.get('font_family',value.get('family','Figtree')));fields.append(family)
            size=PropertyNumber('Size',value.get('font_size',value.get('size',16)),minimum=1,on_change=lambda v:value.update(font_size=v));fields.append(size)
            weight=PropertyChoice('Weight',[(str(n),str(n)) for n in (300,400,500,600,700,800)],current=str(value.get('font_weight',value.get('weight',400))),on_change=lambda v:value.update(font_weight=int(v)));fields.append(weight)
            def read():return {**value,'font_family':family.entry.get_text()}
        def save():
            def mutate():
                if style:resources.update_style(style.id,name.entry.get_text(),read())
                else:
                    created=resources.create_style(subject,name.entry.get_text(),kind)
                    resources.update_style(created.id,name.entry.get_text(),read())
            self.host.edit('Save style',mutate)
        self.form('Edit style' if style else 'Create style',fields,save)

    def variable_form(self,name=None):
        from luma_appkit import TextField,PropertyChoice
        from .resources import Resources
        resources=Resources(self.host.editor)
        current=resources.variables().get(name,{'type':'color','value':'#ffffff'})
        label=TextField('Name',value=name or '')
        value=TextField('Value',value=str(current['value']))
        state={'kind':current['type']}
        swatch=color_field(self.host,'Color',str(current['value']) if current['type']=='color' else '#ffffff',lambda color:value.entry.set_text(color))
        swatch.set_visible(current['type']=='color')
        value.set_visible(current['type']!='color')
        def choose(kind):
            state['kind']=kind;swatch.set_visible(kind=='color');value.set_visible(kind!='color')
        choice=PropertyChoice('Type',[(k,k.title()) for k in ('color','number','text','boolean')],current=current['type'],on_change=choose)
        def save():
            if name and label.entry.get_text()!=name:
                raise ValueError('Keep the name to preserve existing links')
            self.host.edit('Save variable',lambda:resources.set_variable(label.entry.get_text(),value.entry.get_text(),state['kind']))
        self.form('Edit variable' if name else 'Create variable',[label,choice,value,swatch],save)

    def bind_form(self,name):
        from luma_appkit import PropertyChoice
        from .resources import Resources
        selection=self.host.document.selection
        if len(selection)!=1:
            self.host._toast('Select one layer to bind a variable');return
        item=selection[0];resources=Resources(self.host.editor)
        kind=resources.variables()[name]['type']
        choices={'color':['fill','stroke'],'number':['width','height','radius','opacity','stroke_width'],'text':['text'],'boolean':['visible']}[kind]
        if kind=='text' and item.kind!='text':
            self.host._toast('Select a text layer');return
        state={'field':choices[0]}
        field=PropertyChoice('Property',[(k,'Opacity (0–1)' if k=='opacity' else k.replace('_',' ').title()) for k in choices],current=choices[0],on_change=lambda v:state.update(field=v))
        self.form('Bind '+name,[field],lambda:self.host.edit('Bind variable',lambda:resources.bind_variable(item,state['field'],name)))


def auto_layout_selection(host):
    selection=host.document.selection
    if not selection:return
    def mutate():
        item=selection[0] if len(selection)==1 and selection[0].kind in {'frame','group','component','section'} else host.editor.group([i.id for i in selection],kind='frame',name='Auto layout')
        host.editor.set_property([item.id],'layout',AutoLayout().write())
        host.document.select([item.id])
    host.edit('Add auto layout',mutate)


def component_selection(host):
    from .components import make_component
    selection=host.document.selection
    if not selection:return
    def mutate():
        item=selection[0] if len(selection)==1 else host.editor.group([i.id for i in selection],name='Component')
        instance=make_component(host.document,item)
        host.editor.move([instance.id],item.width+40,0)
        host.document.select([instance.component_source_id])
    host.edit('Create component',mutate)


def linked_controls(inspector,item):
    host=inspector.host
    if not item.style_ids and not item.metadata.get('variable_bindings'):return
    section=inspector.PropertySection('Linked resources')
    for key in item.style_ids:
        style=next((style for style in host.document.styles if style.id==key),None)
        if style is None:continue
        row=Gtk.Box(spacing=4);row.append(Gtk.Label(label=style.name,xalign=0,hexpand=True,ellipsize=3))
        def unlink(style_id=key):
            def mutate():item.style_ids=[value for value in item.style_ids if value!=style_id]
            host.edit('Detach style',mutate)
        row.append(action('link-2','Detach style',unlink));section.append(row)
    for field,name in item.metadata.get('variable_bindings',{}).items():
        row=Gtk.Box(spacing=4);row.append(Gtk.Label(label=field.replace('_',' ').title()+': '+name,xalign=0,hexpand=True,ellipsize=3))
        def unlink_variable(property_name=field):
            host.edit('Detach variable',lambda:host.editor.set_property([item.id],'metadata',
                {**item.metadata,'variable_bindings':{key:value for key,value in item.metadata.get('variable_bindings',{}).items() if key!=property_name}}))
        row.append(action('link-2','Detach variable',unlink_variable));section.append(row)
    inspector.append(section)
