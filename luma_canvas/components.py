# SPDX-License-Identifier: Apache-2.0

"""Components and their instances.

A component is a definition; an instance is a use of it. Editing the component
changes every instance, and an instance may differ from its component in the
ways a person deliberately changed — its text, its fills, whether something is
shown — without breaking the link to it.

The rule that matters: an override is a record of *what the person changed*,
not a copy of the whole object. That is what lets a component change
underneath an instance without discarding the instance's own decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy, copy

from .model import Document, LayoutObject, new_id


# What an instance is allowed to differ in. Geometry is not here: an instance
# is placed by its own x and y, and its size follows the component unless the
# component's own layout says otherwise.
OVERRIDABLE = ("text", "fill", "fill_gradient", "fills", "stroke", "stroke_gradient", "strokes", "opacity",
               "visible", "asset_id", "metadata")
# A paint stack and its scalar mirror are overridden together.
PAINT_GROUPS = ({"fill", "fill_gradient", "fills"}, {"stroke", "stroke_gradient", "strokes"})


def _overridden(overrides: dict, field: str) -> bool:
    if field in overrides:
        return True
    return any(field in group and group & set(overrides) for group in PAINT_GROUPS)


@dataclass(frozen=True)
class ComponentUse:
    component: LayoutObject
    instances: list[LayoutObject]

    @property
    def count(self) -> int:
        return len(self.instances)


def components(document: Document) -> list[LayoutObject]:
    return [item for item in document.all_objects() if item.kind == "component"]


def instances_of(document: Document, component_id: str) -> list[LayoutObject]:
    return [item for item in document.all_objects()
            if item.kind == "instance" and item.component_source_id == component_id]


def usage(document: Document) -> list[ComponentUse]:
    """Every component with the instances that use it.

    The Assets panel shows these counts, and they are not decoration: knowing
    a component has six uses before changing it is the point of the panel.
    """
    objects=document.all_objects();uses={}
    for item in objects:
        if item.kind=='instance':uses.setdefault(item.component_source_id,[]).append(item)
    return [ComponentUse(item,uses.get(item.id,[])) for item in objects if item.kind=='component']


def record_override(instance: LayoutObject, field: str, value) -> None:
    """Remember that this instance was deliberately changed in this way."""
    if field not in OVERRIDABLE:
        return
    instance.overrides = {**instance.overrides, field: value}
    setattr(instance, field, deepcopy(value))


def clear_override(instance: LayoutObject, field: str) -> None:
    overrides = dict(instance.overrides)
    overrides.pop(field, None)
    instance.overrides = overrides


def sync_instance(document: Document, component: LayoutObject,
                  instance: LayoutObject) -> list[LayoutObject]:
    """Make one instance agree with its component, keeping its overrides."""
    touched: list[LayoutObject] = []
    def inherited_metadata(source,target):
        identity={key:value for key,value in target.metadata.items() if key in {'figma_id','figma_instance','figma_component','figma_component_id','prototype_instance','component_origin_id','component_position_override'}}
        return {**deepcopy(source.metadata),**identity}

    for field in (*OVERRIDABLE, 'rotation', 'radius', 'corner_radii', 'stroke_width',
                  'stroke_align', 'layout', 'constraints', 'effects', 'path', 'style_ids'):
        if _overridden(instance.overrides, field):
            continue
        value = inherited_metadata(component,instance) if field=="metadata" else getattr(component, field)
        if getattr(instance, field) != value:
            setattr(instance, field, deepcopy(value))
            if instance not in touched:
                touched.append(instance)

    # The instance's size follows the component; its position does not.
    before_size=(instance.width,instance.height)
    if not _overridden(instance.overrides,'width'):instance.width=component.width
    if not _overridden(instance.overrides,'height'):instance.height=component.height
    if before_size!=(instance.width,instance.height) and instance not in touched:touched.append(instance)
    sx=instance.width/component.width if component.width else 1.
    sy=instance.height/component.height if component.height else 1.

    # Source IDs keep identically named children and renames unambiguous.
    previous = document.descendants(instance.id)
    existing = {child.metadata.get('component_origin_id'): child for child in previous
                if child.metadata.get('component_origin_id')}
    legacy = [child for child in previous if not child.metadata.get('component_origin_id')]
    wanted = document.descendants(component.id)
    mapping = {component.id: instance.id}
    offset_x, offset_y = instance.x - component.x, instance.y - component.y
    retained = set()
    for source in wanted:
        clone = existing.get(source.id)
        if clone is None:
            clone = next((child for child in legacy if child.name == source.name), None)
            if clone is not None:
                legacy.remove(clone)
        if clone is None:
            clone = LayoutObject(new_id(source.kind), source.kind, source.name)
            document.objects.append(clone)
        mapping[source.id] = clone.id
        retained.add(clone.id)
        clone.kind, clone.name = source.kind, source.name
        for field in ("width", "height", "rotation", "radius", "corner_radii",
                      "stroke_width", "stroke_align", "layout", "constraints",
                      "effects", "path", "style_ids", "component_source_id", "component_variant", *OVERRIDABLE):
            if not _overridden(clone.overrides, field):
                setattr(clone, field, inherited_metadata(source,clone) if field=="metadata" else deepcopy(getattr(source, field)))
        clone.metadata = {**clone.metadata, 'component_origin_id': source.id}
        dx,dy=clone.metadata.get('component_position_override',(0.,0.))
        clone.x,clone.y=instance.x+(source.x-component.x)*sx+dx,instance.y+(source.y-component.y)*sy+dy
        if not _overridden(clone.overrides,'width'):clone.width=source.width*sx
        if not _overridden(clone.overrides,'height'):clone.height=source.height*sy
        touched.append(clone)
    for source in wanted:
        document.object(mapping[source.id]).parent_id = mapping.get(source.parent_id, instance.id)
    for orphan in previous:
        if orphan.id not in retained:
            document.objects.remove(orphan)
            document.connections=[link for link in document.connections if orphan.id not in link.referenced_ids()]
    return touched


def global_graph(document,operation):
    """Apply a component operation across pages, preserving active object identity."""
    from dataclasses import asdict
    owners={o.id:document.current_page_id for o in document.objects}
    for page in document.pages:
        if page.id!=document.current_page_id:owners.update({o['id']:page.id for o in page.objects})
    graph=copy(document);graph.objects=document.all_objects();graph.pages=[]
    result=operation(graph)
    pending=[o for o in graph.objects if o.id not in owners]
    while pending:
        ready=[o for o in pending if o.parent_id in owners or o.parent_id is None]
        if not ready:raise ValueError('Component sync created an orphan')
        for item in ready:owners[item.id]=owners.get(item.parent_id,document.current_page_id);pending.remove(item)
    document.connections=graph.connections
    document.objects=[o for o in graph.objects if owners[o.id]==document.current_page_id]
    document.selected_ids=[identity for identity in document.selected_ids if any(o.id==identity for o in document.objects)]
    for page in document.pages:
        if page.id!=document.current_page_id:
            page.objects=[asdict(o) for o in graph.objects if owners[o.id]==page.id]
            ids={o['id'] for o in page.objects};page.selected_ids=[identity for identity in page.selected_ids if identity in ids]
    return result


def sync_component(document: Document, component_id: str) -> list[LayoutObject]:
    def apply(graph):
        component=graph.object(component_id)
        if component is None or component.kind!='component':return []
        touched=[]
        for instance in [o for o in graph.objects if o.kind=='instance' and o.component_source_id==component_id]:
            touched.extend(sync_instance(graph,component,instance))
        return touched
    return global_graph(document,apply)


def sync_all(document: Document) -> list[LayoutObject]:
    touched: list[LayoutObject] = []
    for component in components(document):
        touched.extend(sync_component(document, component.id))
    return touched


def _mapped_parent(document: Document, source: LayoutObject,
                   component: LayoutObject, instance: LayoutObject) -> str:
    """Where a cloned child belongs inside the instance."""
    if source.parent_id == component.id:
        return instance.id
    original_parent = document.object(source.parent_id)
    if original_parent is None:
        return instance.id
    for candidate in document.descendants(instance.id):
        if candidate.name == original_parent.name:
            return candidate.id
    return instance.id


def detach(document: Document, instance: LayoutObject) -> LayoutObject:
    """Cut an instance loose from its component.

    Explicit and hard to undo by accident: everything it inherited becomes its
    own, and it stops changing when the component does.
    """
    instance.kind = "group"
    instance.component_source_id = None
    instance.component_variant = None
    instance.overrides = {}
    return instance


def make_component(document: Document, item: LayoutObject) -> LayoutObject:
    """Turn an object into a component, leaving an instance in its place."""
    if item.kind not in {"component", "frame", "group", "section"}:
        definition = LayoutObject(new_id("component"), "component", item.name,
                                  parent_id=item.parent_id, x=item.x, y=item.y,
                                  width=item.width, height=item.height)
        document.objects.insert(document.objects.index(item), definition)
        item.parent_id = definition.id
        item = definition
    item.kind = "component"
    instance = LayoutObject(
        new_id("instance"), "instance", f"{item.name} instance",
        parent_id=item.parent_id, x=item.x, y=item.y,
        width=item.width, height=item.height,
        component_source_id=item.id,
    )
    document.objects.append(instance)
    sync_instance(document, item, instance)
    return instance
