# SPDX-License-Identifier: MPL-2.0
"""Change a component's local state inside an isolated presentation document."""
import copy
from dataclasses import fields
from .model import new_id


def change_variant(document,source_id,destination_id):
    source=document.object(source_id);target=document.object(destination_id)
    if source is None or target is None:raise ValueError('Variant target is missing')
    candidates=[source,*document.ancestors(source.id)]
    instance=next((item for item in candidates if item.kind in {'instance','component'} or item.metadata.get('prototype_instance') or item.metadata.get('prototype_variant')),None)
    if instance is None:raise ValueError('Change to needs a component instance')
    if instance.id==target.id:return
    old=[instance,*document.descendants(instance.id)];template=[target,*document.descendants(target.id)]
    def paths(items):
        by_id={item.id:item for item in items};result={};counts={}
        def visit(item,path):
            result[path]=item
            for child in items:
                if child.parent_id!=item.id:continue
                key=(item.id,child.name,child.kind);number=counts.get(key,0);counts[key]=number+1
                visit(child,path+((child.name,child.kind,number),))
        visit(items[0],());return result
    before,after=paths(old),paths(template)
    ids={item.id:(before[path].id if path in before else new_id(item.kind)) for path,item in after.items()}
    sx=instance.width/max(.001,target.width);sy=instance.height/max(.001,target.height)
    root_id,parent,x,y,width,height,name=instance.id,instance.parent_id,instance.x,instance.y,instance.width,instance.height,instance.name
    clones=copy.deepcopy(template)
    for item in clones:
        previous_id=item.id;item.id=ids[previous_id]
        item.parent_id=ids.get(item.parent_id,item.parent_id)
        item.x=x+(item.x-target.x)*sx;item.y=y+(item.y-target.y)*sy
        item.width*=sx;item.height*=sy
        item.mask_id=ids.get(item.mask_id,item.mask_id)
        item.text_flow_next_id=ids.get(item.text_flow_next_id,item.text_flow_next_id)
        if previous_id==target.id:
            item.parent_id=parent;item.x=x;item.y=y;item.width=width;item.height=height;item.name=name
            item.metadata={**instance.metadata,**item.metadata,'prototype_instance':True,'prototype_active_variant':target.id}
    old_ids={item.id for item in old};template_ids={item.id for item in template}
    inherited=copy.deepcopy([link for link in document.connections if link.source_id in template_ids])
    def remap_script(steps):
        for step in steps:
            # Change-to destinations remain master templates, not their local instance.
            if step.get('action')!='change-variant' and step.get('destination_id') in ids:step['destination_id']=ids[step['destination_id']]
            for block in step.get('blocks',[]):remap_script(block.get('actions',[]))
    for link in inherited:
        link.id='variant-'+root_id+'-'+link.id;link.source_id=ids[link.source_id]
        if link.action!='change-variant':link.destination_id=ids.get(link.destination_id,link.destination_id)
        link.else_destination_id=ids.get(link.else_destination_id,link.else_destination_id)
        remap_script(link.script)
    position=min(document.objects.index(item) for item in old)
    document.objects=[item for item in document.objects if item.id not in old_ids]
    document.objects[position:position]=clones
    document.connections=[link for link in document.connections if link.source_id not in old_ids]+inherited
    document.touch()
