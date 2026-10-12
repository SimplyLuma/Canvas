# SPDX-License-Identifier: Apache-2.0
"""Persisted interactions and isolated prototype navigation state."""
from __future__ import annotations
from dataclasses import dataclass, field
from .model import Document, LayoutObject, PrototypeConnection, new_id

TRIGGERS = ('click','hover','mouse-enter','mouse-leave','while-hover','press','release','while-press','drag','keyboard','timed')
ACTIONS = ('navigate','back','open-overlay','close-overlay','swap-overlay','scroll-to','set-variable','conditional','open-link','change-variant')
TRANSITION_FAMILIES = ('none','dissolve','smart-animate','push','move-in','move-out','slide-in','slide-out')
DIRECTIONS = ('left','right','up','down')
TRANSITIONS = ('none','dissolve','smart-animate',*(f'{family}-{direction}' for family in TRANSITION_FAMILIES[3:] for direction in DIRECTIONS))
EASINGS = ('linear','ease-in','ease-out','ease-in-out','spring')


def transition_parts(value):
    if value in TRANSITION_FAMILIES:return value,'left'
    for direction in DIRECTIONS:
        if value.endswith('-'+direction):return value[:-len(direction)-1],direction
    return 'none','left'


def ease(value: float, curve: str) -> float:
    """Exact endpoints, with a restrained spring for geometry motion."""
    import math
    t=max(0.,min(1.,value))
    if t in (0.,1.):return t
    if curve=='ease-in':return t*t*t
    if curve=='ease-out':return 1-(1-t)**3
    if curve=='ease-in-out':return 4*t**3 if t<.5 else 1-(-2*t+2)**3/2
    if curve=='spring':return 1-math.exp(-7*t)*(math.cos(10*t)+.7*math.sin(10*t))
    return t


def connect(document: Document, source: LayoutObject, destination: LayoutObject | None,
            *, trigger='click', transition='dissolve', duration_ms=220,
            connection_id=None, **options) -> PrototypeConnection:
    if source is None or document.object(source.id) is None:raise ValueError('Choose an existing layer')
    if trigger not in TRIGGERS:raise ValueError(f'unsupported trigger: {trigger}')
    if transition not in TRANSITIONS:raise ValueError(f'unsupported transition: {transition}')
    action=options.get('action','navigate')
    if action not in ACTIONS:raise ValueError('unsupported prototype action')
    if action in {'navigate','open-overlay','swap-overlay','conditional','change-variant'} and (destination is None or destination.kind not in {'frame','page','component','instance'}):
        raise ValueError('Choose a destination frame or component')
    if action=='scroll-to' and destination is None:raise ValueError('Choose a layer to scroll to')
    if destination is None:destination=source
    # Existing callers replace the same trigger; authoring identifies an exact
    # connection, allowing different triggers on one hotspot to coexist.
    existing=next((link for link in document.connections if link.id==connection_id),None) if connection_id else next((link for link in document.connections if link.source_id==source.id and link.trigger==trigger),None)
    data=dict(id=existing.id if existing else new_id('connection'),source_id=source.id,destination_id=destination.id,
              trigger=trigger,transition=transition,duration_ms=int(duration_ms),**options)
    link=PrototypeConnection(**data)
    link.validate({item.id for item in document.objects})
    if existing:document.connections[document.connections.index(existing)]=link
    else:document.connections.append(link)
    document.touch()
    return link


def disconnect(document, source_id, connection_id=None):
    document.connections=[link for link in document.connections if not (link.source_id==source_id and (connection_id is None or link.id==connection_id))]
    document.touch()


def connections_from(document,object_id):return [link for link in document.connections if link.source_id==object_id]


def owning_frame(document, item):
    if item is None:return None
    candidates=[item,*document.ancestors(item.id)]
    return next((obj for obj in candidates if obj.kind in {'frame','page','component','instance'}),None)


def start_frames(document):
    frames=[item for item in document.objects if item.kind in {'frame','page','component','instance'} and item.parent_id is None]
    explicit=[item for item in frames if item.metadata.get('prototype_start')]
    arrived={link.destination_id for link in document.connections if link.action in {'navigate','conditional'}}
    return explicit or [item for item in frames if item.id not in arrived] or frames


@dataclass
class Presentation:
    document: Document
    current_id: str
    history: list[str]
    overlays: list[tuple[str,PrototypeConnection]] = field(default_factory=list)
    start_id: str = ''

    @classmethod
    def begin(cls,document,frame=None):
        frames=start_frames(document)
        if frame is None:
            if not frames:raise ValueError('Add a frame to preview this prototype')
            frame=frames[0]
        return cls(document,frame.id,[],start_id=frame.id)

    @property
    def frame(self):return self.document.object(self.current_id)

    def hotspots(self):
        root=self.document.object(self.overlays[-1][0]) if self.overlays else self.frame
        if not root:return []
        inside={root.id,*[item.id for item in self.document.descendants(root.id)]}
        result=[];seen=set()
        for link in self.document.connections:
            source=self.document.object(link.source_id)
            identity=(link.source_id,link.interaction_id) if link.interaction_id else (link.id,)
            if source is None or source.id not in inside or identity in seen:continue
            if not all(parent.visible for parent in [source,*self.document.ancestors(source.id)]):continue
            seen.add(identity);result.append((source,link))
        return result

    def activate(self,object_id,trigger=None,connection_id=None):
        link=next((link for link in self.document.connections if link.source_id==object_id and (trigger is None or link.trigger==trigger) and (connection_id is None or link.id==connection_id)),None)
        if link is None:return None
        if link.script:
            return self.run_script(link)
        sequence=[candidate for candidate in self.document.connections if candidate.source_id==link.source_id and candidate.interaction_id==link.interaction_id] if link.interaction_id else [link]
        visual=link
        for step in sequence:
            self._apply(step)
            if step.action not in {'set-variable','open-link'}:visual=step
        return visual

    def run_script(self,link):
        from dataclasses import replace
        from .prototype_values import evaluate
        from .resources import Resources
        from .editing import Editor
        last=link
        def run(steps,depth=0):
            nonlocal last
            if depth>24:raise ValueError('Prototype actions are nested too deeply')
            for step in steps:
                action=step.get('action')
                if action=='conditional':
                    variables=Resources(Editor(self.document)).variables()
                    for block in step.get('blocks',[]):
                        condition=block.get('condition')
                        if condition is None or bool(evaluate(condition,variables)):
                            run(block.get('actions',[]),depth+1);break
                elif action=='set-variable':
                    resources=Resources(Editor(self.document));variables=resources.variables()
                    name=step.get('variable')
                    if name not in variables:raise ValueError('Choose an existing prototype variable')
                    value=evaluate(step['expression'],variables)
                    resources.set_variable(name,value,variables[name]['type'])
                else:
                    fields={key:value for key,value in step.items() if key in {'action','destination_id','transition','duration_ms','easing','overlay_position','overlay_x','overlay_y','close_outside','scrim_opacity','preserve_scroll','url'}}
                    current=replace(link,script=[],**fields)
                    self._apply(current)
                    last=current
        run(link.script)
        return last

    def _apply(self,link):
        if link.action=='back':self.back()
        elif link.action=='close-overlay':
            if self.overlays:self.overlays.pop()
        elif link.action=='open-overlay':self.overlays.append((link.destination_id,link))
        elif link.action=='swap-overlay':
            if self.overlays:
                # Swapping preserves the original placement and dismissal settings.
                self.overlays[-1]=(link.destination_id,self.overlays[-1][1])
            else:self.overlays.append((link.destination_id,link))
        elif link.action=='conditional':
            from .resources import Resources
            from .editing import Editor
            variable=Resources(Editor(self.document)).variables().get(link.variable)
            if variable is None:raise ValueError('Choose an existing variable for this condition')
            actual=variable['value']
            desired=link.value
            if variable and variable['type']=='number':
                try:desired=float(desired)
                except ValueError:desired=None
            equal=actual==desired
            destination=link.destination_id if (equal if link.condition=='equals' else not equal) else link.else_destination_id
            if destination:self._navigate(destination)
        elif link.action=='set-variable':
            from .resources import Resources
            from .editing import Editor
            resources=Resources(Editor(self.document));variable=resources.variables().get(link.variable)
            if variable is None:raise ValueError('Choose an existing variable for this interaction')
            resources.set_variable(link.variable,link.value,variable['type'])
        elif link.action=='navigate':self._navigate(link.destination_id)
        elif link.action=='change-variant':
            from .prototype_variants import change_variant
            change_variant(self.document,link.source_id,link.destination_id)

    def _navigate(self,destination):
        self.history.append(self.current_id);self.current_id=destination;self.overlays.clear()

    def back(self):
        if self.overlays:self.overlays.pop();return True
        if not self.history:return False
        self.current_id=self.history.pop();return True
