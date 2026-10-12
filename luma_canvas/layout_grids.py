# SPDX-License-Identifier: Apache-2.0
"""Editable frame grid guides. Editor overlays, never exported artwork."""
from __future__ import annotations
import math

def grid_bands(grid,width,height):
    if not grid.get('visible',True):return []
    size=max(.1,float(grid.get('sectionSize',10)));offset=float(grid.get('offset',0))
    if grid.get('pattern')=='GRID':
        return [(x,0,0,height) for x in (i*size for i in range(min(2048,math.ceil(width/size))))]+[(0,y,width,0) for y in (i*size for i in range(min(2048,math.ceil(height/size))))]
    horizontal=grid.get('axis','X')=='X';length=width if horizontal else height
    count=max(1,min(2048,int(grid.get('numSections',5))));gap=float(grid.get('gutterSize',10))
    kind=grid.get('type','STRETCH')
    if kind=='STRETCH':size=max(.1,(length-2*offset-gap*(count-1))/count)
    occupied=size*count+gap*(count-1)
    start=(length-occupied)/2 if kind=='CENTER' else length-offset-occupied if kind=='MAX' else offset
    return [(start+i*(size+gap),0,size,height) if horizontal else (0,start+i*(size+gap),width,size) for i in range(count)]

def controls(inspector,item):
    from .design_controls import action,header_actions,color_field
    from .paint import Color,to_hex,parse_hex
    host=inspector.host
    def change(index,**values):
        grids=[dict(g) for g in item.metadata.get('layout_grids',[])]
        if not 0<=index<len(grids):return
        grids[index].update(values)
        host.edit('Change layout grid',lambda:host.editor.set_property([item.id],'metadata',{**item.metadata,'layout_grids':grids}),coalesce=True)
    def add():
        grids=[*item.metadata.get('layout_grids',[]),{'pattern':'STRIPES','axis':'X','type':'STRETCH','numSections':5,'sectionSize':10.,'offset':20.,'gutterSize':10.,'visible':True,'color':{'r':.3,'g':.5,'b':1.,'a':.1}}]
        host.edit('Add layout grid',lambda:host.editor.set_property([item.id],'metadata',{**item.metadata,'layout_grids':grids}))
    inspector.append(header_actions(inspector.PropertySection('Layout grids'),action('plus','Add layout grid',add)))
    for index,grid in enumerate(item.metadata.get('layout_grids',[])):
        def current(i=index):return item.metadata.get('layout_grids',[])[i]
        def update(i=index,**values):change(i,**values)
        def remove(i=index):
            grids=[g for n,g in enumerate(item.metadata.get('layout_grids',[])) if n!=i]
            host.edit('Remove layout grid',lambda:host.editor.set_property([item.id],'metadata',{**item.metadata,'layout_grids':grids}))
        section=inspector.PropertySection('Grid '+str(index+1))
        header_actions(section,action('eye' if grid.get('visible',True) else 'eye-off','Toggle grid visibility',lambda get=current,put=update:put(visible=not get().get('visible',True))),action('minus','Remove layout grid',remove))
        kind='grid' if grid.get('pattern')=='GRID' else 'columns' if grid.get('axis','X')=='X' else 'rows'
        section.append(inspector._choice('Type',[('columns','Columns'),('rows','Rows'),('grid','Grid')],kind,
            lambda get=current:'grid' if get().get('pattern')=='GRID' else 'columns' if get().get('axis','X')=='X' else 'rows',
            lambda value,put=update:put(pattern='GRID' if value=='grid' else 'STRIPES',axis='Y' if value=='rows' else 'X')))
        for label,key,default,minimum in [('Count','numSections',5,1),('Size','sectionSize',10,.1),('Gutter','gutterSize',10,0),('Margin','offset',20,0)]:
            section.append(inspector._number(label,grid.get(key,default),lambda k=key,d=default,get=current:get().get(k,d),lambda value,k=key,put=update:put(**{k:int(value) if k=='numSections' else value}),minimum=minimum))
        section.append(inspector._choice('Alignment',[(v,v.title()) for v in ('STRETCH','MIN','CENTER','MAX')],grid.get('type','STRETCH'),lambda get=current:get().get('type','STRETCH'),lambda value,put=update:put(type=value)))
        color=grid.get('color',{})
        section.append(color_field(host,'Color',to_hex(Color(color.get('r',.3),color.get('g',.5),color.get('b',1),color.get('a',.1))),lambda value,put=update:put(color=parse_hex(value).to_dict())))
        inspector.append(section)

def draw(canvas,snapshot,view):
    from .scene import rect,point,rgba,append_clipped
    from gi.repository import Gtk
    from .paint import Color
    for frame in canvas.document.objects:
        grids=frame.metadata.get('layout_grids')
        if not grids or not frame.visible or not all(p.visible for p in canvas.document.ancestors(frame.id)):continue
        x,y=view.to_view(frame.x+frame.width/2,frame.y+frame.height/2)
        radius=math.hypot(frame.width,frame.height)*view.zoom/2
        if x+radius<0 or y+radius<0 or x-radius>canvas.get_width() or y-radius>canvas.get_height():continue
        snapshot.save();snapshot.translate(point(x,y));snapshot.rotate(frame.rotation);snapshot.scale(view.zoom,view.zoom)
        snapshot.translate(point(-frame.width/2,-frame.height/2))
        guides=Gtk.Snapshot.new()
        for grid in grids:
            color=grid.get('color',{});paint=rgba(Color(color.get('r',.3),color.get('g',.5),color.get('b',1),color.get('a',.1)))
            for bx,by,w,h in grid_bands(grid,frame.width,frame.height):guides.append_color(paint,rect(bx,by,w or 1/view.zoom,h or 1/view.zoom))
        append_clipped(snapshot,guides,rect(0,0,frame.width,frame.height))
        snapshot.restore()
