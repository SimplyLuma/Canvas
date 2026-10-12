# SPDX-License-Identifier: MPL-2.0
"""Stable, eagerly rendered prototype screens and frame-clock transitions.

No editor chrome, screenshot allocation, or deferred scene construction occurs
in the presentation surface. A transition composes two complete render nodes
inside the same viewport and uses the window's frame clock.
"""
from __future__ import annotations
import copy
import math
from gi.repository import Gtk, Gdk, Gsk, GLib
from .model import Document
from .scene import SceneRenderer, rect, point, rgba
from .paint import Color, color_of, to_hex
from .prototype import Presentation, owning_frame, transition_parts, ease, start_frames
from .viewport import View


def normalized_family(document, frame_id):
    root=document.object(frame_id)
    if root is None:return []
    family={root.id,*[item.id for item in document.descendants(root.id)]}
    objects=[copy.deepcopy(item) for item in document.objects if item.id in family]
    for item in objects:
        item.x-=root.x;item.y-=root.y
        if item.id==root.id:item.parent_id=None
    return objects


def layer_paths(objects):
    by_id={item.id:item for item in objects};children={}
    for item in objects:children.setdefault(item.parent_id,[]).append(item)
    result={}
    def visit(parent,path):
        counts={}
        for child in children.get(parent,[]):
            label=(child.name,child.kind);number=counts.get(label,0);counts[label]=number+1
            key=path+((child.name,child.kind,number),) if parent is not None else ()
            result[key]=child;visit(child.id,key)
    visit(None,())
    return result


def smart_objects(old,new,progress):
    """Match hierarchy/name/occurrence; unmatched layers dissolve deterministically."""
    t=progress;alpha=max(0.,min(1.,t))
    before,after=layer_paths(old),layer_paths(new)
    result=copy.deepcopy(new);result_by={item.id:item for item in result}
    for key,destination in after.items():
        current=result_by[destination.id];source=before.get(key)
        if source is None:
            # Parent opacity already fades the subtree.
            if current.parent_id is None or any(parent.id==current.parent_id for path,parent in after.items() if path in before):current.opacity*=alpha
            continue
        for field in ('x','y','width','height','radius','stroke_width','opacity'):
            a,b=getattr(source,field),getattr(destination,field)
            value=a+(b-a)*t
            if field in {'width','height','radius','stroke_width'}:value=max(0.001,value)
            if field=='opacity':value=max(0.,min(1.,value))
            setattr(current,field,value)
        delta=(destination.rotation-source.rotation+180)%360-180
        current.rotation=source.rotation+delta*t
        for name in ('font_size','line_height','letter_spacing'):
            if isinstance(source.metadata.get(name),(int,float)) and isinstance(destination.metadata.get(name),(int,float)):
                current.metadata[name]=source.metadata[name]+(destination.metadata[name]-source.metadata[name])*t
        for name in ('fill','stroke'):
            a,b=color_of(getattr(source,name)),color_of(getattr(destination,name))
            if a is not None and b is not None and not getattr(source,name+'_gradient') and not getattr(destination,name+'_gradient'):
                color=Color(*(getattr(a,k)+(getattr(b,k)-getattr(a,k))*alpha for k in ('r','g','b','a')))
                setattr(current,name,to_hex(color))
                paints=getattr(current,name+'s')
                if paints and len(paints)==1 and paints[0].get('type','solid')=='solid':paints[0]['color']=color.to_dict()
        if source.text!=destination.text or source.asset_id!=destination.asset_id:
            current.opacity*=alpha
            ghost=copy.deepcopy(source);ghost.id='old-'+source.id;ghost.parent_id=current.parent_id
            ghost.x,ghost.y,ghost.width,ghost.height=current.x,current.y,current.width,current.height
            ghost.opacity*=1-alpha;result.append(ghost)
    new_keys=set(after)
    for key,source in before.items():
        if key in new_keys:continue
        ghost=copy.deepcopy(source);ghost.id='old-'+source.id
        parent_key=key[:-1]
        parent=after.get(parent_key)
        ghost.parent_id=parent.id if parent else ('old-'+source.parent_id if source.parent_id else None)
        if parent_key in new_keys or ghost.parent_id is None:ghost.opacity*=1-alpha
        result.append(ghost)
    return result


class PrototypeSurface(Gtk.Widget):
    __gtype_name__='CanvasPrototypeSurface'
    def __init__(self,document,start):
        super().__init__(hexpand=True,vexpand=True)
        self.document=document
        self.document.select([])
        self.initial_document=copy.deepcopy(document)
        self.presentation=Presentation.begin(document,start)
        self.viewport=tuple(start.metadata.get('prototype_viewport',(start.width,start.height)))
        if len(self.viewport)!=2 or not all(isinstance(n,(int,float)) and math.isfinite(n) and n>0 for n in self.viewport):self.viewport=(start.width,start.height)
        self.fit=True;self.show_hotspots=False
        self.scroll_positions={}
        self.cache={};self.families={};self.fixed_nodes={}
        self.renderer=SceneRenderer(None)
        self.last_link=None;self.overlay_motion=None
        self.previous=None;self.target_node=None;self.active_link=None
        self.progress=1.;self.started=None;self.start_progress=0.;self.tick=0;self.reduced_motion=False
        self.on_complete=lambda:None
        self.connect('unmap',lambda *_:self.finish())
        self.set_focusable(True)
        self.update_property([Gtk.AccessibleProperty.LABEL],['Interactive prototype'])

    def do_measure(self,orientation,for_size):return 0,320 if orientation==Gtk.Orientation.HORIZONTAL else 240,-1,-1

    def local_view(self):
        width,height=self.viewport
        zoom=min(max(1,self.get_width()-32)/width,max(1,self.get_height()-112)/height)
        if not self.fit:zoom=min(1.,zoom) if self.get_width()<600 else 1.
        x=(self.get_width()-width*zoom)/2;y=16+(max(1,self.get_height()-112)-height*zoom)/2
        return View(max(.001,zoom),x,y)

    @property
    def view(self):
        local=self.local_view();frame=self.presentation.frame
        sx,sy=self.scroll_positions.get(frame.id,(0.,0.))
        return View(local.zoom,local.x-(frame.x+sx)*local.zoom,local.y-(frame.y+sy)*local.zoom)

    def view_to_world(self,x,y):return self.view.to_document(x,y)

    def prepare(self,frame_id):
        if frame_id not in self.cache:
            objects=normalized_family(self.document,frame_id)
            if not objects:return None
            self.families[frame_id]=copy.deepcopy(objects)
            for item in objects:
                for parent in self.document.ancestors(item.id):
                    if parent.id==frame_id:break
                    if item.metadata.get('prototype_fixed'):break
                    sx,sy=self.scroll_positions.get(parent.id,(0.,0.))
                    item.x-=sx;item.y-=sy
            subset=copy.copy(self.document);subset.objects=objects
            by_id={item.id:item for item in objects}
            fixed=[item for item in objects if item.metadata.get('prototype_fixed') and item.parent_id is not None
                   and not any(parent.metadata.get('prototype_fixed') for parent in self.document.ancestors(item.id))]
            nodes=[]
            for fixed_root in fixed:
                included={fixed_root.id,*[item.id for item in self.document.descendants(fixed_root.id)]}
                family=copy.deepcopy([item for item in objects if item.id in included])
                next(item for item in family if item.id==fixed_root.id).parent_id=None
                isolated=copy.copy(self.document);isolated.objects=family
                node=SceneRenderer(None).scene(isolated)
                if node:nodes.append(node)
                by_id[fixed_root.id].visible=False
            self.fixed_nodes[frame_id]=nodes
            self.cache[frame_id]=self.renderer.scene(subset)
        return self.cache[frame_id]

    def invalidate(self):
        self.cache.clear();self.families.clear();self.fixed_nodes.clear();self.renderer.invalidate()

    def overlay_rect(self,frame,settings):
        w,h=self.viewport;ow,oh=frame.width,frame.height
        position=settings.overlay_position
        x=(w-ow)/2;y=(h-oh)/2
        if 'left' in position or position=='left':x=0
        if 'right' in position or position=='right':x=w-ow
        if 'top' in position or position=='top':y=0
        if 'bottom' in position or position=='bottom':y=h-oh
        if position=='manual':x,y=settings.overlay_x,settings.overlay_y
        return x,y,ow,oh

    def screen_node(self,overlays=None):
        snapshot=Gtk.Snapshot();w,h=self.viewport
        snapshot.push_clip(rect(0,0,w,h))
        frame=self.presentation.frame
        node=self.prepare(frame.id)
        sx,sy=self.scroll_positions.get(frame.id,(0.,0.))
        snapshot.save();snapshot.translate(point(-sx,-sy))
        if node:snapshot.append_node(node)
        snapshot.restore()
        for fixed in self.fixed_nodes.get(frame.id,[]):snapshot.append_node(fixed)
        for object_id,settings in (self.presentation.overlays if overlays is None else overlays):
            overlay=self.document.object(object_id)
            if overlay is None:continue
            snapshot.append_color(rgba(Color(0,0,0,settings.scrim_opacity)),rect(0,0,w,h))
            x,y,ow,oh=self.overlay_rect(overlay,settings)
            snapshot.save();snapshot.translate(point(x,y));snapshot.push_clip(rect(0,0,ow,oh))
            node=self.prepare(object_id)
            if node:snapshot.append_node(node)
            for fixed in self.fixed_nodes.get(object_id,[]):snapshot.append_node(fixed)
            snapshot.pop();snapshot.restore()
        snapshot.pop()
        return snapshot.to_node()

    def overlay_layer(self,entry):
        frame_id,settings=entry;frame=self.document.object(frame_id)
        x,y,w,h=self.overlay_rect(frame,settings)
        snapshot=Gtk.Snapshot();snapshot.save();snapshot.translate(point(x,y));snapshot.push_clip(rect(0,0,w,h))
        node=self.prepare(frame_id)
        if node:snapshot.append_node(node)
        for fixed in self.fixed_nodes.get(frame_id,[]):snapshot.append_node(fixed)
        snapshot.pop();snapshot.restore();return snapshot.to_node()

    def overlay_transition(self,t,opacity,family,direction):
        base,old,new,settings=self.overlay_motion
        w,h=self.viewport;dx,dy={'left':(-w,0),'right':(w,0),'up':(0,-h),'down':(0,h)}[direction]
        snapshot=Gtk.Snapshot()
        if base:snapshot.append_node(base)
        scrim=1. if old is not None and new is not None else opacity if new is not None else 1-opacity
        snapshot.append_color(rgba(Color(0,0,0,settings.scrim_opacity*scrim)),rect(0,0,w,h))
        def draw(node,x,y,alpha):
            if node is None:return
            snapshot.save();snapshot.translate(point(x,y));snapshot.push_opacity(max(0.,min(1.,alpha)))
            snapshot.append_node(node);snapshot.pop();snapshot.restore()
        moving=family in {'push','move-in','move-out','slide-in','slide-out'}
        fraction=.25 if family in {'slide-in','slide-out'} else 1.
        fade=family in {'dissolve','smart-animate','slide-in','slide-out'}
        draw(old,dx*t*fraction if moving else 0,dy*t*fraction if moving else 0,1-opacity if fade or new else 1)
        draw(new,-dx*(1-t)*fraction if moving else 0,-dy*(1-t)*fraction if moving else 0,opacity if fade or old else 1)
        return snapshot.to_node()

    def transition_node(self,progress=None):
        if self.active_link is None or self.previous is None:return self.screen_node()
        raw=self.progress if progress is None else progress
        t=ease(raw,self.active_link.easing);opacity=max(0.,min(1.,t))
        old,new=self.previous,self.target_node
        family,direction=transition_parts(self.active_link.transition)
        if self.overlay_motion is not None:return self.overlay_transition(t,opacity,family,direction)
        if family=='dissolve':return Gsk.CrossFadeNode.new(old,new,opacity)
        if family=='smart-animate' and self.smart_pair:
            objects=smart_objects(*self.smart_pair,t)
            subset=copy.copy(self.document);subset.objects=objects
            return SceneRenderer(None).scene(subset)
        w,h=self.viewport
        dx,dy={'left':(-w,0),'right':(w,0),'up':(0,-h),'down':(0,h)}[direction]
        snapshot=Gtk.Snapshot()
        def draw(node,x,y,alpha=1.):
            snapshot.save();snapshot.translate(point(x,y));snapshot.push_opacity(max(0.,min(1.,alpha)))
            snapshot.append_node(node);snapshot.pop();snapshot.restore()
        if family=='push':draw(old,dx*t,dy*t);draw(new,-dx*(1-t),-dy*(1-t))
        elif family=='move-in':draw(old,0,0);draw(new,-dx*(1-t),-dy*(1-t))
        elif family=='move-out':draw(new,0,0);draw(old,dx*t,dy*t)
        elif family=='slide-in':draw(old,0,0);draw(new,-dx*.25*(1-t),-dy*.25*(1-t),opacity)
        elif family=='slide-out':draw(new,0,0);draw(old,dx*.25*t,dy*.25*t,1-opacity)
        else:return Gsk.CrossFadeNode.new(old,new,opacity)
        return snapshot.to_node()

    def do_snapshot(self,snapshot):
        if self.get_width()<=0 or self.get_height()<=0:return
        view=self.local_view();w,h=self.viewport
        snapshot.save();snapshot.translate(point(view.x,view.y));snapshot.scale(view.zoom,view.zoom)
        snapshot.push_clip(rect(0,0,w,h))
        node=self.transition_node()
        if node:snapshot.append_node(Gsk.OpacityNode.new(node,1.))
        if self.show_hotspots and not self.active_link:
            for source,link in self.presentation.hotspots():
                if not source.visible:continue
                x,y=self.hotspot_local(source)
                snapshot.append_color(rgba(Color(.24,.52,1,.15)),rect(x,y,source.width,source.height))
        snapshot.pop();snapshot.restore()

    def hotspot_local(self,source):
        if self.presentation.overlays:
            frame=self.document.object(self.presentation.overlays[-1][0]);settings=self.presentation.overlays[-1][1]
            x,y,*_=self.overlay_rect(frame,settings)
            return x+source.x-frame.x,y+source.y-frame.y
        frame=self.presentation.frame;sx,sy=self.scroll_positions.get(frame.id,(0,0))
        if any(item.metadata.get('prototype_fixed') for item in [source,*self.document.ancestors(source.id)]):sx=sy=0
        for parent in self.document.ancestors(source.id):
            if parent.id==frame.id:break
            px,py=self.scroll_positions.get(parent.id,(0.,0.));sx+=px;sy+=py
        return source.x-frame.x-sx,source.y-frame.y-sy

    def hit(self,x,y,trigger):
        from .geometry import contains
        v=self.local_view();lx,ly=v.to_document(x,y)
        if not 0<=lx<=self.viewport[0] or not 0<=ly<=self.viewport[1]:return None
        candidates=self.presentation.hotspots()
        order={item.id:i for i,item in enumerate(self.document.objects)}
        candidates.sort(key=lambda pair:(len(self.document.ancestors(pair[0].id)),order.get(pair[0].id,0)),reverse=True)
        for source,link in candidates:
            if not source.visible or link.trigger!=trigger:continue
            ox,oy=self.hotspot_local(source)
            clipped=False
            for parent in self.document.ancestors(source.id):
                if parent.id==self.presentation.frame.id:break
                if parent.clip_content:
                    px,py=self.hotspot_local(parent)
                    if not px<=lx<=px+parent.width or not py<=ly<=py+parent.height:
                        clipped=True;break
            if not clipped and contains(source,source.x+lx-ox,source.y+ly-oy):return source,link
        return None

    def navigate(self,source_id,trigger=None,connection_id=None):
        if self.active_link:return None
        old_frame=self.presentation.current_id;old_overlay_entries=list(self.presentation.overlays);old_overlays=bool(old_overlay_entries)
        old=self.screen_node()
        old_family=copy.deepcopy(self.families.get(old_frame,[]))
        revision=self.document.revision
        link=self.presentation.activate(source_id,trigger,connection_id)
        if link is None:return None
        self.last_link=link
        if self.document.revision!=revision:self.invalidate()
        if link.action=='scroll-to':self.scroll_to(link.destination_id)
        if self.presentation.current_id!=old_frame and not link.preserve_scroll:
            reset={self.presentation.current_id,*[item.id for item in self.document.descendants(self.presentation.current_id)]}
            if any(identity in self.scroll_positions for identity in reset):
                for identity in reset:self.scroll_positions.pop(identity,None)
                self.invalidate()
        new=self.screen_node()
        self.smart_pair=None;self.overlay_motion=None
        new_entries=self.presentation.overlays
        if self.presentation.current_id==old_frame:
            if len(new_entries)>len(old_overlay_entries):
                entry=new_entries[-1];self.overlay_motion=(old,None,self.overlay_layer(entry),entry[1])
            elif len(new_entries)<len(old_overlay_entries):
                entry=old_overlay_entries[-1];self.overlay_motion=(new,self.overlay_layer(entry),None,entry[1])
            elif new_entries and new_entries[-1][0]!=old_overlay_entries[-1][0]:
                entry=new_entries[-1];self.overlay_motion=(self.screen_node(new_entries[:-1]),self.overlay_layer(old_overlay_entries[-1]),self.overlay_layer(entry),entry[1])
        if link.transition=='smart-animate' and not old_overlays and not self.presentation.overlays and (self.presentation.current_id!=old_frame or self.document.revision!=revision):
            self.smart_pair=(old_family,self.families[self.presentation.current_id])
        if old is not None and new is not None and link.transition!='none' and link.duration_ms>0 and not self.reduced_motion:
            self.previous=old;self.target_node=new;self.active_link=link;self.progress=0.;self.started=None;self.start_progress=0.
            self.tick=self.add_tick_callback(self.advance)
        else:self.on_complete()
        self.queue_draw();return link

    def advance(self,widget,clock):
        now=clock.get_frame_time()
        if self.started is None:self.started=now
        self.progress=min(1.,self.start_progress+(now-self.started)/(max(1,self.active_link.duration_ms)*1000))
        self.queue_draw()
        if self.progress>=1:
            self.tick=0;self.previous=self.target_node=self.active_link=None
            self.on_complete();return False
        return True

    def animate_state(self,state):
        from .model import PrototypeConnection
        old=self.transition_node() if self.active_link else self.screen_node()
        old_overlays=list(self.presentation.overlays);was_active=self.active_link is not None
        self.finish()
        self.presentation.current_id,self.presentation.history,self.presentation.overlays=state[:3]
        if len(state)>3:
            self.document=state[3];self.presentation.document=self.document
            self.invalidate()
        new=self.screen_node();self.overlay_motion=None
        if not was_active and len(old_overlays)>len(self.presentation.overlays):
            entry=old_overlays[-1];self.overlay_motion=(new,self.overlay_layer(entry),None,entry[1])
        if old is None or new is None or self.reduced_motion:
            self.queue_draw();self.on_complete();return
        link=copy.copy(self.last_link) if self.last_link else PrototypeConnection('back','','',duration_ms=180)
        family,direction=transition_parts(link.transition)
        inverse={'left':'right','right':'left','up':'down','down':'up'}[direction]
        if family in {'push','move-in','move-out','slide-in','slide-out'}:
            family={'move-in':'move-out','move-out':'move-in','slide-in':'slide-out','slide-out':'slide-in'}.get(family,family)
            link.transition=family+'-'+inverse
        if family=='smart-animate':link.transition='dissolve'
        if link.transition=='none' or link.duration_ms<=0:
            self.queue_draw();self.on_complete();return
        self.smart_pair=None;self.previous=old;self.target_node=new;self.active_link=link
        self.progress=0.;self.start_progress=0.;self.started=None
        self.tick=self.add_tick_callback(self.advance);self.queue_draw()

    def finish(self):
        if self.tick:self.remove_tick_callback(self.tick)
        self.tick=0;self.previous=self.target_node=self.active_link=None;self.overlay_motion=None;self.progress=1.;self.queue_draw()

    def scroll_extent(self,frame):
        # Clipped children contribute their viewport, not hidden image overflow.
        right=frame.x+frame.width;bottom=frame.y+frame.height
        def visit(parent):
            nonlocal right,bottom
            for child in self.document.children(parent.id):
                if not child.visible or child.metadata.get('prototype_fixed'):continue
                right=max(right,child.x+child.width);bottom=max(bottom,child.y+child.height)
                if not child.clip_content:visit(child)
        visit(frame)
        return right-frame.x,bottom-frame.y

    def scroll_by(self,dx,dy,x=None,y=None):
        if self.active_link or self.presentation.overlays:return
        frame=self.presentation.frame
        candidates=[]
        if x is not None and y is not None:
            lx,ly=self.local_view().to_document(x,y)
            for item in self.document.descendants(frame.id):
                if not item.visible or item.metadata.get('prototype_scroll','none')=='none':continue
                ox,oy=self.hotspot_local(item)
                if ox<=lx<=ox+item.width and oy<=ly<=oy+item.height:candidates.append(item)
            candidates.sort(key=lambda item:len(self.document.ancestors(item.id)),reverse=True)
        candidates.append(frame)
        for target in candidates:
            mode=target.metadata.get('prototype_scroll','none')
            sx,sy=self.scroll_positions.get(target.id,(0.,0.))
            w,h=self.viewport if target.id==frame.id else (target.width,target.height)
            extent_w,extent_h=self.scroll_extent(target)
            horizontal=dy if mode=='horizontal' and not dx else dx
            nx=max(0.,min(max(0.,extent_w-w),sx+horizontal)) if mode in {'horizontal','both'} else sx
            ny=max(0.,min(max(0.,extent_h-h),sy+dy)) if mode in {'vertical','both'} else sy
            if (nx,ny)==(sx,sy):continue
            self.scroll_positions[target.id]=(nx,ny)
            self.invalidate();self.queue_draw();return

    def scroll_to(self,object_id):
        target=self.document.object(object_id);frame=self.presentation.frame
        if target and (target.id==frame.id or frame in self.document.ancestors(target.id)):
            sx,sy=self.scroll_positions.get(frame.id,(0,0))
            self.scroll_by(target.x-frame.x-sx,target.y-frame.y-sy)


class PrototypePreview:
    def __init__(self,host):
        from luma_appkit import AppWindow,CommandRegistry,ActionCenter,BarAction
        from .persistence import APP_ID
        self.host=host
        document=Document.from_dict(host.document.to_dict())
        document.embedded_assets=dict(host.document.embedded_assets)
        # Presentations can navigate between pages without changing the editor page.
        document.objects=document.all_objects()
        page=document.current_page()
        document.pages=[page] if page else []
        if page:page.objects=[]
        selected=host.document.selection
        start=owning_frame(document,document.object(selected[0].id)) if selected else None
        frames=start_frames(document)
        if start is None and not frames:raise ValueError('Add a frame to preview this prototype')
        self.canvas=PrototypeSurface(document,start or frames[0])
        self.presentation=self.canvas.presentation
        phone=getattr(host,'_phone_ui',False)
        self.window=AppWindow(application=host.get_application(),app_id=APP_ID,title='Canvas prototype',icon_name=APP_ID,
            default_width=max(320,host.get_width()) if phone else 1000,default_height=host.get_height() if phone else 720,
            minimum_width=320,minimum_height=294,geometry_scope='prototype',commands=CommandRegistry())
        self.window.set_transient_for(host)
        self.overlay=Gtk.Overlay(child=self.canvas);self.window.set_body(self.overlay)
        self.actions=ActionCenter().attach(self.overlay)
        self.actions.show_bar([BarAction('chevron-left',tooltip='Back',on_activate=self.back),
            BarAction('rotate-ccw',tooltip='Restart',on_activate=self.restart),
            BarAction('scan',tooltip='Fit / actual size',on_activate=self.toggle_fit),
            BarAction('mouse-pointer-2',tooltip='Show interaction hotspots',on_activate=self.toggle_hotspots),
            BarAction('x',tooltip='Close preview',on_activate=self.window.close)],fill=phone)
        self.timers=[];self.temporary=None;self.hovered=set();self.press_origin=None;self.drag_start=None
        self.canvas.on_complete=self.ready
        settings=Gtk.Settings.get_default()
        self.canvas.reduced_motion=not settings.get_property('gtk-enable-animations') if settings else False
        click=Gtk.GestureClick(button=0);click.connect('pressed',self.pressed);click.connect('released',self.released);self.canvas.add_controller(click)
        motion=Gtk.EventControllerMotion();motion.connect('motion',self.motion);motion.connect('leave',self.leave);self.canvas.add_controller(motion)
        drag=Gtk.GestureDrag(button=0);drag.connect('drag-begin',self.drag_begin);drag.connect('drag-update',self.drag_update);drag.connect('drag-end',self.drag_end);self.canvas.add_controller(drag)
        scroll=Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect('scroll',self.scrolled);self.canvas.add_controller(scroll)
        keys=Gtk.EventControllerKey();keys.connect('key-pressed',self.key);self.window.add_controller(keys)
        self.window.connect('close-request',self.closed)
        self.window.connect('map',lambda *_:self.ready())
        if phone:self.window.set_default_size(max(320,host.get_width()),host.get_height())
        self.window.present();self.canvas.grab_focus()

    def scrolled(self,controller,dx,dy):
        xy=getattr(self,'pointer_position',(None,None))
        zoom=self.canvas.local_view().zoom
        self.canvas.scroll_by(dx*32/zoom,dy*32/zoom,*xy)
        return True

    def cancel_timers(self):
        for timer in self.timers:GLib.source_remove(timer)
        self.timers=[]

    def ready(self):
        self.cancel_timers();self.window.set_title('Canvas prototype · '+self.presentation.frame.name)
        # Delay is separate from animation duration. Restart only after transition completion.
        for source,link in self.presentation.hotspots():
            if link.trigger=='timed' and source.visible:
                identifier=[0]
                def fire(source_id=source.id,link_id=link.id,identifier=identifier):
                    if identifier[0] in self.timers:self.timers.remove(identifier[0])
                    self.navigate(source_id,'timed',link_id);return False
                identifier[0]=GLib.timeout_add(max(16,link.delay_ms),fire);self.timers.append(identifier[0])

    def navigate(self,source_id,trigger=None,connection_id=None):
        if self.canvas.active_link:return None
        self.cancel_timers()
        try:link=self.canvas.navigate(source_id,trigger,connection_id)
        except ValueError as error:
            self.host._toast('Prototype: '+str(error));return None
        self.presentation=self.canvas.presentation
        if link and link.action=='open-link':self.open_link(link.url)
        return link

    def open_link(self,url):
        from urllib.parse import urlsplit
        from gi.repository import Adw
        parsed=urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname:
            self.host._toast('Prototype links need a complete http or https URL');return
        dialog=Adw.AlertDialog(heading='Open external link?',body=url)
        dialog.add_response('cancel','Cancel');dialog.add_response('open','Open link')
        dialog.set_default_response('cancel');dialog.set_close_response('cancel')
        dialog.connect('response',lambda _dialog,response:Gtk.UriLauncher.new(url).launch(self.window,None,None) if response=='open' else None)
        dialog.present(self.window)

    def follow(self,trigger,x,y):
        hit=self.canvas.hit(x,y,trigger)
        if hit:self.navigate(hit[0].id,trigger,hit[1].id);return hit
        return None

    def pressed(self,_gesture,_n,x,y):
        self.press_origin=(x,y);self.dragged=False;self.temporary=None
        for trigger in ('while-press','press'):
            hit=self.canvas.hit(x,y,trigger)
            if hit:
                if trigger=='while-press':self.temporary=self.temporary_state()
                self.navigate(hit[0].id,trigger,hit[1].id);break

    def released(self,_gesture,_n,x,y):
        if self.temporary is not None and self.press_origin is not None:self.restore_temporary()
        elif not getattr(self,'dragged',False):
            hit=self.follow('release',x,y) or self.follow('click',x,y)
            if not hit and self.presentation.overlays and not self.canvas.active_link:
                frame_id,settings=self.presentation.overlays[-1];frame=self.canvas.document.object(frame_id)
                lx,ly=self.canvas.local_view().to_document(x,y);ox,oy,w,h=self.canvas.overlay_rect(frame,settings)
                if settings.close_outside and not (ox<=lx<=ox+w and oy<=ly<=oy+h):self.back()
        self.press_origin=None

    def motion(self,_controller,x,y):
        self.pointer_position=(x,y)
        present=set()
        self.canvas.set_cursor_from_name('pointer' if self.canvas.hit(x,y,'click') else 'default')
        for trigger in ('mouse-enter','hover','while-hover','mouse-leave'):
            hit=self.canvas.hit(x,y,trigger)
            if not hit:continue
            source,link=hit;present.add(link.id)
            if trigger=='mouse-leave':continue
            if link.id not in self.hovered and not self.canvas.active_link:
                if trigger=='while-hover':
                    self.temporary=self.temporary_state()
                    self.temporary_rect=(source,self.canvas.local_view(),self.canvas.hotspot_local(source))
                self.navigate(source.id,trigger,link.id)
        if self.temporary is not None and self.press_origin is None and hasattr(self,'temporary_rect'):
            source,view,(ox,oy)=self.temporary_rect;lx,ly=view.to_document(x,y)
            from .geometry import contains
            if not contains(source,source.x+lx-ox,source.y+ly-oy):self.restore_temporary()
        for source,link in self.presentation.hotspots():
            if link.trigger=='mouse-leave' and link.id in self.hovered and link.id not in present:
                self.navigate(source.id,link.trigger,link.id);break
        self.hovered=present

    def leave(self,*_args):self.motion(None,-1e9,-1e9)

    def temporary_state(self):
        # Variant changes mutate only the preview document. Restore appearance,
        # interactions and variables as well as navigation when the gesture ends.
        return (self.presentation.current_id,list(self.presentation.history),
                list(self.presentation.overlays),copy.deepcopy(self.presentation.document))

    def restore_temporary(self):
        state=self.temporary;self.temporary=None
        self.cancel_timers();self.canvas.animate_state(state)

    def drag_begin(self,_gesture,x,y):
        self.drag_start=(x,y);self.dragged=False;self.drag_delta=(0,0);self.drag_state=None
    def drag_update(self,_gesture,dx,dy):
        if not self.drag_start:return
        if not self.dragged and math.hypot(dx,dy)>8:
            self.dragged=True
            hit=self.canvas.hit(*self.drag_start,'drag')
            if hit:
                self.drag_state=(self.presentation.current_id,list(self.presentation.history),list(self.presentation.overlays))
                self.navigate(hit[0].id,'drag',hit[1].id)
                if self.canvas.tick:self.canvas.remove_tick_callback(self.canvas.tick);self.canvas.tick=0
        if self.drag_state and self.canvas.active_link:
            family,direction=transition_parts(self.canvas.active_link.transition)
            zoom=self.canvas.local_view().zoom;w,h=self.canvas.viewport
            distance={'left':-dx,'right':dx,'up':-dy,'down':dy}[direction]
            length=(h if direction in {'up','down'} else w)*zoom
            if family in {'dissolve','smart-animate'}:distance=max(abs(dx),abs(dy));length=w*zoom
            self.canvas.progress=max(0.,min(1.,distance/max(1,length)));self.canvas.queue_draw()
        elif self.dragged and not self.canvas.active_link:
            previous_x,previous_y=self.drag_delta
            self.canvas.scroll_by(-(dx-previous_x)/self.canvas.local_view().zoom,-(dy-previous_y)/self.canvas.local_view().zoom,*(self.press_origin or (None,None)))
        self.drag_delta=(dx,dy)
    def drag_end(self,*_args):
        if self.drag_state and self.canvas.active_link:
            if self.canvas.progress<.3:
                self.canvas.finish()
                self.presentation.current_id,self.presentation.history,self.presentation.overlays=self.drag_state
                self.canvas.queue_draw();self.ready()
            else:
                self.canvas.start_progress=self.canvas.progress;self.canvas.started=None
                self.canvas.tick=self.canvas.add_tick_callback(self.canvas.advance)
        self.drag_start=None;self.drag_state=None

    def key(self,_controller,keyval,_code,state):
        for source,link in self.presentation.hotspots():
            if link.trigger=='keyboard':
                success,expected,modifiers=Gtk.accelerator_parse(link.key)
                if success and keyval==expected and (state & Gtk.accelerator_get_default_mod_mask())==modifiers:
                    self.navigate(source.id,'keyboard',link.id);return True
        if keyval==Gdk.KEY_Escape:
            if self.presentation.overlays:self.back()
            else:self.window.close()
            return True
        if keyval==Gdk.KEY_BackSpace:self.back();return True
        return False

    def back(self):
        self.cancel_timers();self.temporary=None
        current=self.presentation.current_id;history=list(self.presentation.history);overlays=list(self.presentation.overlays)
        if self.presentation.back():
            target=(self.presentation.current_id,list(self.presentation.history),list(self.presentation.overlays))
            self.presentation.current_id,self.presentation.history,self.presentation.overlays=current,history,overlays
            self.canvas.animate_state(target)
        else:self.ready()
    def restart(self):
        self.cancel_timers();self.canvas.finish();self.temporary=None
        document=copy.deepcopy(self.canvas.initial_document)
        self.canvas.document=document;self.presentation.document=document
        self.presentation.current_id=self.presentation.start_id;self.presentation.history=[];self.presentation.overlays=[]
        self.canvas.invalidate();self.canvas.scroll_positions.clear();self.canvas.queue_draw();self.ready()
    def toggle_fit(self):self.canvas.fit=not self.canvas.fit;self.canvas.queue_draw()
    def toggle_hotspots(self):self.canvas.show_hotspots=not self.canvas.show_hotspots;self.canvas.queue_draw()
    def closed(self,*_args):self.cancel_timers();self.canvas.finish();return False
