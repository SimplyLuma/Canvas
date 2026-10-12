# SPDX-License-Identifier: MPL-2.0
"""Contextual prototype authoring using shared Luma creative controls."""
from dataclasses import asdict
from gi.repository import Gtk
from .prototype import connect,disconnect,connections_from,owning_frame,start_frames,transition_parts,TRIGGERS,ACTIONS,TRANSITION_FAMILIES,DIRECTIONS,EASINGS
from .prototype_preview import PrototypePreview

TRIGGER_LABELS={'click':'Click / tap','hover':'Hover once','mouse-enter':'Mouse enter','mouse-leave':'Mouse leave',
                'while-hover':'While hovering','press':'Press down','release':'Release','while-press':'While pressing',
                'drag':'Drag / swipe','keyboard':'Keyboard','timed':'After delay'}
ACTION_LABELS={'navigate':'Navigate to','back':'Go back','open-overlay':'Open overlay','close-overlay':'Close overlay',
               'swap-overlay':'Swap overlay','scroll-to':'Scroll to','set-variable':'Set variable','conditional':'Conditional navigation','open-link':'Open link','change-variant':'Change variant'}
ANIMATION_LABELS={'none':'Instant','dissolve':'Dissolve','smart-animate':'Smart animate','push':'Push','move-in':'Move in','move-out':'Move out','slide-in':'Slide in','slide-out':'Slide out'}


class PrototypePanel(Gtk.Box):
    def __init__(self,host):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.host=host;self.signature=None;self.updates=[];self.duration_field=None
        self.refresh()

    def preview(self):
        try:PrototypePreview(self.host)
        except ValueError as error:self.host._toast(str(error))

    def refresh(self):
        from luma_appkit import PropertySection,PropertyChoice,PropertyNumber,TextField
        from .design_controls import action,header_actions
        from .creative_inspector import CreativeInspector
        host=self.host;document=host.document;selection=document.selection
        item=selection[0] if len(selection)==1 else None
        links=connections_from(document,item.id) if item else []
        frames=[frame for frame in document.objects if frame.kind in {'frame','page','component','instance'}]
        signature=(tuple((obj.id,id(obj)) for obj in selection),
                   tuple((link.id,link.action,link.trigger,transition_parts(link.transition)[0],link.variable,link.overlay_position,link.close_outside,link.preserve_scroll,transition_parts(link.transition)[1],link.else_destination_id) for link in links),
                   tuple((frame.id,frame.name) for frame in frames),
                   tuple((obj.id,obj.metadata.get("prototype_start"),obj.metadata.get("prototype_scroll"),obj.metadata.get("prototype_fixed")) for obj in selection))
        if signature==self.signature:
            for field,update in self.updates:
                if not CreativeInspector._focused(field):update()
            return
        self.signature=signature;self.updates=[];self.duration_field=None
        while (child:=self.get_first_child()) is not None:self.remove(child)
        if item is None:
            label=Gtk.Label(label='Select a layer to add interactions, or a frame to set up a flow.',wrap=True,xalign=0)
            label.add_css_class('dim-label');self.append(label);return
        owner=owning_frame(document,item)
        if item.kind in {'frame','page','component','instance'}:
            def metadata(**values):
                host.edit('Change prototype settings',lambda:host.editor.set_property([item.id],'metadata',{**item.metadata,**values}))
            flow=PropertySection('Flow')
            flow.append(PropertyChoice('Starting point',[('no','Off'),('yes','Start here')],current='yes' if item.metadata.get('prototype_start') else 'no',
                on_change=lambda value:metadata(prototype_start=value=='yes')))
            flow.append(PropertyChoice('Scrolling',[(k,k.title()) for k in ('none','vertical','horizontal','both')],
                current=item.metadata.get('prototype_scroll','none'),on_change=lambda value:metadata(prototype_scroll=value)))
            viewport=item.metadata.get('prototype_viewport',(item.width,item.height))
            def viewport_change(index,value):
                dimensions=list(item.metadata.get('prototype_viewport',(item.width,item.height)));dimensions[index]=value
                metadata(prototype_viewport=dimensions)
            row=Gtk.Box(spacing=6)
            for index,label in enumerate(('Viewport W','Viewport H')):
                field=PropertyNumber(label,viewport[index],minimum=1,maximum=100000,on_change=lambda value,index=index:viewport_change(index,value))
                field.set_hexpand(True);row.append(field)
                self.updates.append((field,lambda field=field,index=index:field.set_value(item.metadata.get('prototype_viewport',(item.width,item.height))[index])))
            flow.append(row);self.append(flow)
        elif owner is not None:
            self.append(PropertySection('Scroll position',PropertyChoice('Position', [('normal','Scrolls with frame'),('fixed','Fixed in viewport')],
                current='fixed' if item.metadata.get('prototype_fixed') else 'normal',
                on_change=lambda value:host.edit('Change scrolling position',lambda:host.editor.set_property([item.id],'metadata',{**item.metadata,'prototype_fixed':value=='fixed'})))))
        def add():
            used={link.trigger for link in links}
            trigger=next((key for key in TRIGGERS if key not in used),None)
            if trigger is None:host._toast('This layer already uses every trigger');return
            destination=next((frame for frame in frames if frame.id!=(owner.id if owner else item.id)),None)
            host.edit('Add interaction',lambda:connect(document,item,destination or owner or item,trigger=trigger,
                action='navigate' if destination else 'back'))
        interactions=header_actions(PropertySection('Interactions'),action('plus','Add interaction',add))
        self.append(interactions)
        if not links:
            label=Gtk.Label(label='Add an interaction to choose its trigger, action and animation.',wrap=True,xalign=0)
            label.add_css_class('dim-label');interactions.append(label)
        for link in links:self.interaction(item,link,frames)

    def interaction(self,item,link,frames):
        from luma_appkit import PropertySection,PropertyChoice,PropertyNumber,TextField
        from .design_controls import action,header_actions
        from .resources import Resources
        host=self.host;document=host.document;link_id=link.id
        def current():return next((value for value in document.connections if value.id==link_id),link)
        def change(**values):
            previous=current();data=asdict(previous)
            for key in ('id','source_id','destination_id'):data.pop(key,None)
            destination=values.pop('destination_id',previous.destination_id)
            data.update(values)
            if 'trigger' in values and any(other.id!=link_id and other.source_id==item.id and other.trigger==values['trigger'] for other in document.connections):
                host._toast('This layer already has an interaction for that trigger');return
            host.edit('Change interaction',lambda:connect(document,document.object(item.id),document.object(destination),connection_id=link_id,**data))
        def number(label,key,minimum=0,maximum=10000,unit=''):
            field=PropertyNumber(label,getattr(link,key),minimum=minimum,maximum=maximum,unit=unit,
                on_change=lambda value:change(**{key:int(value) if key in {'duration_ms','delay_ms'} else value}))
            self.updates.append((field,lambda:field.set_value(getattr(current(),key))))
            return field
        def choice(label,key,choices):
            field=PropertyChoice(label,choices,current=str(getattr(link,key)),on_change=lambda value:change(**{key:value}))
            self.updates.append((field,lambda:field.set_current(str(getattr(current(),key)))))
            return field
        title=TRIGGER_LABELS.get(link.trigger,link.trigger)+' → '+ACTION_LABELS.get(link.action,link.action)
        section=header_actions(PropertySection(title),action('minus','Remove interaction',
            lambda:host.edit('Remove interaction',lambda:disconnect(document,item.id,link_id))))
        section.append(choice('Trigger','trigger',[(key,TRIGGER_LABELS[key]) for key in TRIGGERS]))
        if link.trigger=='timed':section.append(number('Delay','delay_ms',maximum=60000,unit='ms'))
        if link.trigger=='keyboard':
            field=TextField('Key',value=link.key)
            field.entry.connect('activate',lambda *_:change(key=field.entry.get_text()))
            focus=Gtk.EventControllerFocus();focus.connect('leave',lambda *_:change(key=field.entry.get_text()));field.add_controller(focus)
            section.append(field)
        section.append(choice('Action','action',[(key,ACTION_LABELS[key]) for key in ACTIONS]))
        destinations=[(frame.id,frame.name) for frame in frames]
        if link.action=='scroll-to':
            owner=owning_frame(document,item)
            destinations=[(obj.id,obj.name) for obj in document.descendants(owner.id)] if owner else []
        if link.action in {'navigate','open-overlay','swap-overlay','scroll-to','conditional','change-variant'}:
            if not any(key==link.destination_id for key,_ in destinations):
                destination=document.object(link.destination_id)
                if destination:destinations.insert(0,(destination.id,destination.name))
            section.append(choice('Destination','destination_id',destinations))
        if link.action=='open-link':
            field=TextField('URL',value=link.url)
            field.entry.connect('activate',lambda *_:change(url=field.entry.get_text()))
            focus=Gtk.EventControllerFocus();focus.connect('leave',lambda *_:change(url=field.entry.get_text()));field.add_controller(focus);section.append(field)
        if link.action in {'set-variable','conditional'}:
            variables=Resources(host.editor).variables()
            section.append(choice('Variable','variable',[('', 'Choose variable'),*[(name,name) for name in variables]]))
            value=TextField('Value',value=link.value)
            value.entry.connect('activate',lambda *_:change(value=value.entry.get_text()))
            focus=Gtk.EventControllerFocus();focus.connect('leave',lambda *_:change(value=value.entry.get_text()));value.add_controller(focus);section.append(value)
            if link.action=='conditional':
                section.append(choice('Condition','condition',[('equals','Equals'),('not-equals','Does not equal')]))
                choices=[('', 'Stay on this screen'),*[(frame.id,frame.name) for frame in frames]]
                field=PropertyChoice('Otherwise',choices,current=link.else_destination_id or '',on_change=lambda value:change(else_destination_id=value or None));section.append(field)
        if link.action in {'open-overlay','swap-overlay'}:
            section.append(choice('Position','overlay_position',[(key,key.replace('-',' ').title()) for key in ('center','top-left','top','top-right','left','right','bottom-left','bottom','bottom-right','manual')]))
            # Numeric placement stays available without resetting the selected mode.
            row=Gtk.Box(spacing=6);row.append(number('X','overlay_x',minimum=-100000,maximum=100000));row.append(number('Y','overlay_y',minimum=-100000,maximum=100000));section.append(row)
            opacity=PropertyNumber('Backdrop',link.scrim_opacity*100,minimum=0,maximum=100,unit='%',on_change=lambda value:change(scrim_opacity=value/100));section.append(opacity)
            self.updates.append((opacity,lambda:opacity.set_value(current().scrim_opacity*100)))
            field=PropertyChoice('Outside click',[('yes','Close overlay'),('no','Keep open')],current='yes' if link.close_outside else 'no',on_change=lambda value:change(close_outside=value=='yes'));section.append(field)
        if link.action=='navigate':
            section.append(PropertyChoice('Scroll position',[('reset','Reset'),('preserve','Remember')],current='preserve' if link.preserve_scroll else 'reset',on_change=lambda value:change(preserve_scroll=value=='preserve')))
        family,direction=transition_parts(link.transition)
        if link.action not in {'set-variable','open-link'}:
            animation=PropertyChoice('Animation',[(key,ANIMATION_LABELS[key]) for key in TRANSITION_FAMILIES],current=family,
                on_change=lambda value:change(transition=value+'-'+transition_parts(current().transition)[1] if value in {'push','move-in','move-out','slide-in','slide-out'} else value))
            section.append(animation)
            if family in {'push','move-in','move-out','slide-in','slide-out'}:
                section.append(PropertyChoice('Direction',[(key,key.title()) for key in DIRECTIONS],current=direction,on_change=lambda value:change(transition=transition_parts(current().transition)[0]+'-'+value)))
            if family!='none':
                section.append(choice('Easing','easing',[(key,key.replace('-',' ').title()) for key in EASINGS]))
                field=number('Duration','duration_ms',unit='ms');self.duration_field=field;section.append(field)
        self.append(section)
