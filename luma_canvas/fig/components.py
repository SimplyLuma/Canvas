# SPDX-License-Identifier: MPL-2.0
"""Retain embedded definitions and link imported, already-rendered instances."""
from copy import deepcopy
from ..model import Page
from ..components import OVERRIDABLE
from .document import guid_of,guid_text
IDENTITY={'figma_id','figma_instance','figma_component','figma_component_id','prototype_instance','prototype_variant','component_origin_id'}

def link_instances(converter,pages):
    objects=lambda:[o for page in pages for o in page.objects]
    definitions={o.metadata.get('figma_id'):o for o in objects() if o.kind=='component'}
    library=None;column=0;cursor_x=0.;cursor_y=0.;row_height=0.
    for _ in range(16):
        missing={o.metadata.get('figma_component_id') for o in objects() if o.metadata.get('figma_instance') and o.metadata.get('figma_component_id') not in definitions}
        if not missing:break
        created=False
        for identity in sorted(identity for identity in missing if identity):
            if not identity:continue
            master=converter.fig.node(tuple(int(v) for v in identity.split(':')))
            if master is None:continue
            if library is None:
                library=Page(converter.new_id('page'),'Imported components');library.objects=[];pages.append(library)
            converter.objects=library.objects
            from .convert import IDENTITY as affine_identity
            before=len(converter.objects)
            definition=converter._convert(master,affine_identity,None)
            if definition is not None and definition.kind=='component':
                if column==4:column=0;cursor_x=0.;cursor_y+=row_height+100.;row_height=0.
                dx,dy=cursor_x-definition.x,cursor_y-definition.y
                for item in converter.objects[before:]:item.x+=dx;item.y+=dy
                cursor_x+=max(160.,definition.width)+80.;row_height=max(row_height,definition.height);column+=1
                definitions[identity]=definition;created=True
        if not created:break
    all_objects=objects();by_parent={}
    for item in all_objects:by_parent.setdefault(item.parent_id,[]).append(item)
    def family(root):
        result=[];pending=[root.id]
        while pending:
            children=by_parent.get(pending.pop(),[]);result.extend(children);pending.extend(o.id for o in children)
        return result
    for instance in all_objects:
        if not instance.metadata.get('figma_instance'):continue
        definition=definitions.get(instance.metadata.get('figma_component_id'))
        if definition is None:
            converter.report.note('components_missing',instance.name);continue
        instance.kind='instance';instance.component_source_id=definition.id
        children={o.metadata.get('figma_id'):o for o in family(instance)}
        sx=instance.width/definition.width if definition.width else 1
        sy=instance.height/definition.height if definition.height else 1
        def overrides(source,clone):
            for field in OVERRIDABLE:
                before,after=getattr(source,field),getattr(clone,field)
                if field=='metadata':
                    before={k:v for k,v in before.items() if k not in IDENTITY}
                    after={k:v for k,v in after.items() if k not in IDENTITY}
                if before!=after:clone.overrides[field]=deepcopy(getattr(clone,field))
            for field in ('width','height','rotation','radius','corner_radii','layout','constraints','effects'):
                if getattr(source,field)!=getattr(clone,field):clone.overrides[field]=deepcopy(getattr(clone,field))
        overrides(definition,instance)
        for source in family(definition):
            clone=children.get(instance.metadata['figma_id']+'/'+source.metadata.get('figma_id',''))
            if clone is None:continue
            overrides(source,clone)
            clone.metadata={**clone.metadata,'component_origin_id':source.id}
            dx=clone.x-instance.x-(source.x-definition.x)*sx
            dy=clone.y-instance.y-(source.y-definition.y)*sy
            if abs(dx)>1e-5 or abs(dy)>1e-5:clone.metadata['component_position_override']=[dx,dy]
