# SPDX-License-Identifier: MPL-2.0
"""Resolve embedded Kiwi prototype data after every page and instance is converted."""
from __future__ import annotations
import math
from dataclasses import asdict
from ..model import PrototypeConnection
from .document import guid_of, guid_text

TRIGGERS = {'ON_CLICK':'click','AFTER_TIMEOUT':'timed','MOUSE_IN':'mouse-enter',
    'MOUSE_OUT':'mouse-leave','MOUSE_ENTER':'mouse-enter','MOUSE_LEAVE':'mouse-leave',
    'ON_HOVER':'while-hover','MOUSE_DOWN':'press','MOUSE_UP':'release','ON_PRESS':'while-press',
    'DRAG':'drag','ON_KEY_DOWN':'keyboard'}
TRANSITIONS = {'INSTANT_TRANSITION':'none','DISSOLVE':'dissolve','FADE':'dissolve',
    'MAGIC_MOVE':'smart-animate','SMART_ANIMATE':'smart-animate','SCROLL_ANIMATE':'dissolve'}
# FROM_LEFT enters at the left edge, moving right. Canvas directions describe motion.
for fig, canvas in (('SLIDE_FROM','slide-in'),('PUSH_FROM','push'),('MOVE_FROM','move-in')):
    for edge,motion in (('LEFT','right'),('RIGHT','left'),('TOP','down'),('BOTTOM','up')):
        TRANSITIONS[fig+'_'+edge]=canvas+'-'+motion
for fig,canvas in (('SLIDE_OUT_TO','slide-out'),('MOVE_OUT_TO','move-out')):
    for edge,motion in (('LEFT','left'),('RIGHT','right'),('TOP','up'),('BOTTOM','down')):
        TRANSITIONS[fig+'_'+edge]=canvas+'-'+motion
EASING = {'IN_CUBIC':'ease-in','OUT_CUBIC':'ease-out','INOUT_CUBIC':'ease-in-out','LINEAR':'linear'}
POSITIONS = {'CENTER':'center','TOP_LEFT':'top-left','TOP_CENTER':'top','TOP_RIGHT':'top-right',
    'BOTTOM_LEFT':'bottom-left','BOTTOM_CENTER':'bottom','BOTTOM_RIGHT':'bottom-right','MANUAL':'manual'}


def milliseconds(value,default,maximum):
    if value is None:return default
    number=float(value)
    if not math.isfinite(number) or number<0:raise ValueError('invalid interaction timing')
    # Native Kiwi transitionDuration/Timeout is expressed in seconds.
    return min(maximum,round(number*1000))


def frame_settings(props):
    result={}
    direction=props.get('scrollDirection')
    if direction in {'HORIZONTAL','VERTICAL','BOTH','NONE'}:result['prototype_scroll']=direction.lower()
    if props.get('scrollBehavior')=='FIXED_WHEN_CHILD_OF_SCROLLING_FRAME':result['prototype_fixed']=True
    if props.get('prototypeStartingPoint'):result['prototype_start']=True
    device=props.get('prototypeDevice')
    if isinstance(device,dict):
        size=device.get('size')
        if isinstance(size,dict):
            width,height=size.get('x'),size.get('y')
            if all(isinstance(n,(int,float)) and math.isfinite(n) and n>0 for n in (width,height)):
                result['prototype_viewport']=[width,height]
    return result


def keyboard(event):
    key=event.get('keyTrigger',{})
    codes=key.get('keyCodes',[]) if isinstance(key,dict) else []
    modifiers={16:'<Shift>',17:'<Control>',18:'<Alt>',91:'<Super>',93:'<Super>'}
    names={8:'BackSpace',9:'Tab',13:'Return',27:'Escape',32:'space',37:'Left',38:'Up',39:'Right',40:'Down',46:'Delete'}
    prefix=''.join(modifiers[code] for code in dict.fromkeys(codes) if code in modifiers)
    ordinary=[code for code in codes if code not in modifiers]
    if len(ordinary)!=1:raise ValueError('unsupported keyboard chord')
    code=ordinary[0]
    name=names.get(code)
    if name is None:
        if 65<=code<=90:name=chr(code).lower()
        elif 48<=code<=57:name=chr(code)
        elif 112<=code<=135:name='F'+str(code-111)
        else:raise ValueError('unsupported keyboard key')
    return prefix+name


def import_prototypes(document,sources,starts,report,variable_names=None):
    from .prototype_variables import expression
    variable_names=variable_names or {}
    objects=document.all_objects();by_id={item.id:item for item in objects}
    by_fig={item.metadata.get('figma_id'):item for item in objects}
    props_by_id={object_id:props for object_id,props in sources}
    for fig_id in starts:
        if fig_id in by_fig:by_fig[fig_id].metadata['prototype_start']=True
    # all_objects returns copies for inactive pages. Write metadata back below.
    count=0
    for source_id,props in sources:
        source=by_id.get(source_id)
        if source is None:continue
        def note(reason):report.note('prototype',source.name,extra=reason)
        def resolve(value):
            guid=guid_of(value)
            if guid is None:return None
            identity=guid_text(guid);origin=source.metadata.get('figma_id','')
            # Prefer the copy inside this instance before the global component.
            if '/' in origin:
                prefix=origin.rsplit('/',1)[0]
                while prefix:
                    found=by_fig.get(prefix+'/'+identity)
                    if found:return found
                    prefix=prefix.rsplit('/',1)[0] if '/' in prefix else ''
            return by_fig.get(identity)
        interactions=props.get('prototypeInteractions')
        if not isinstance(interactions,list):
            if not guid_of(props.get('transitionNodeID')) and props.get('connectionType') not in {'URL','BACK','CLOSE'}:continue
            interactions=[{'event':{'interactionType':'ON_CLICK'},'actions':[props]}]
        def convert_step(action,event,depth=0):
            if depth>24:raise ValueError('prototype actions are nested too deeply')
            if 'connectionType' not in action and not guid_of(action.get('transitionNodeID')):
                return {'action':'noop'}  # Unconnected editor animation hint.
            kind=action.get('connectionType','INTERNAL_NODE')
            navigation=action.get('navigationType','NAVIGATE')
            destination=resolve(action.get('transitionNodeID'))
            if kind=='SET_VARIABLE':
                target=action.get('targetVariableID') or (action.get('targetVariable') or {}).get('id') or {}
                identifier=guid_of(target.get('guid'))
                name=variable_names.get(identifier)
                if name is None:raise ValueError('variable target is missing from this file')
                data=action.get('targetVariableData') or action.get('targetVariableValue')
                return {'action':'set-variable','variable':name,'expression':expression(data,variable_names)}
            if kind=='CONDITIONAL':
                blocks=[]
                for block in action.get('conditionalActions',[]):
                    condition=block.get('condition')
                    blocks.append({'condition':expression(condition,variable_names) if condition else None,
                        'actions':[convert_step(item,event,depth+1) for item in block.get('actions',[])]})
                if not blocks:raise ValueError('conditional action has no branches')
                return {'action':'conditional','blocks':blocks}
            action_name={'URL':'open-link','BACK':'back','CLOSE':'close-overlay'}.get(kind)
            if kind=='NONE':return {'action':'noop'}
            if action_name is None:
                if kind!='INTERNAL_NODE':raise ValueError('unsupported action '+str(kind))
                action_name={'NAVIGATE':'navigate','OVERLAY':'open-overlay','SWAP':'swap-overlay','SCROLL_TO':'scroll-to','SWAP_STATE':'change-variant'}.get(navigation)
                if action_name is None:raise ValueError('unsupported navigation '+str(navigation))
                if destination is None:raise ValueError('destination is missing from this file')
            target=destination or source
            if action_name in {'navigate','open-overlay','swap-overlay'} and target.kind not in {'frame','page','component','instance'}:
                raise ValueError('destination is not a frame')
            transition=TRANSITIONS.get(action.get('transitionType','INSTANT_TRANSITION'))
            if transition is None:raise ValueError('unsupported animation '+str(action.get('transitionType')))
            if action.get('transitionShouldSmartAnimate'):transition='smart-animate'
            easing_type=action.get('easingType','INOUT_CUBIC')
            easing=EASING.get(easing_type)
            if easing is None:
                if 'SPRING' in easing_type:easing='spring';note('spring uses Canvas spring curve')
                else:easing='ease-in-out';note('custom easing uses ease in-out')
            target_props=props_by_id.get(target.id,{})
            position=POSITIONS.get(target_props.get('overlayPositionType','CENTER'),'center')
            offset=action.get('overlayRelativePosition') or target_props.get('overlayRelativePosition') or {}
            appearance=target_props.get('overlayBackgroundAppearance') or {}
            background=appearance.get('backgroundColor') or {}
            opacity=background.get('a',.25) if appearance.get('backgroundType')=='SOLID_COLOR' else 0.
            options=dict(action=action_name,easing=easing,
                delay_ms=milliseconds(event.get('transitionTimeout',action.get('transitionTimeout')),800,60000),
                key=keyboard(event) if trigger=='keyboard' else 'Return',
                overlay_position=position,overlay_x=float(offset.get('x',0)),overlay_y=float(offset.get('y',0)),
                close_outside=target_props.get('overlayBackgroundInteraction')=='CLOSE_ON_CLICK_OUTSIDE',
                scrim_opacity=max(0.,min(1.,float(opacity))),
                preserve_scroll=bool(action.get('transitionPreserveScroll',False)),url=action.get('connectionURL',''))
            return dict(destination_id=target.id,transition=transition,
                duration_ms=milliseconds(action.get('transitionDuration'),220,10000),**options)
        for index,interaction in enumerate(interactions):
            if not isinstance(interaction,dict) or interaction.get('isDeleted'):continue
            event=interaction.get('event') or {}
            if not event.get('interactionType'):continue
            trigger=TRIGGERS.get(event.get('interactionType'))
            if trigger is None:note('unsupported trigger '+str(event.get('interactionType')));continue
            actions=interaction.get('actions') or []
            script=[]
            for action in actions:
                if not isinstance(action,dict):continue
                try:
                    step=convert_step(action,event)
                    if step['action']!='noop':script.append(step)
                except (ValueError,TypeError,OverflowError) as error:note(str(error))
            if not script:continue
            first=next((step for step in script if step['action'] not in {'set-variable','conditional'}),None)
            base=dict(first or {'action':'set-variable','destination_id':source.id,'transition':'none','duration_ms':0})
            for field in ('key','delay_ms'):
                base.pop(field,None)
            try:
                connection=PrototypeConnection('fig-prototype-'+source.id+'-'+str(index),source.id,trigger=trigger,
                    delay_ms=milliseconds(event.get('transitionTimeout'),800,60000),
                    key=keyboard(event) if trigger=='keyboard' else 'Return',
                    script=script if len(script)>1 or script[0]['action'] in {'set-variable','conditional'} else [],**base)
                connection.validate(set(by_id))
                document.connections.append(connection);count+=1
            except (ValueError,TypeError,OverflowError) as error:note(str(error))
    for page in document.pages:
        if page.id==document.current_page_id:continue
        for item in page.objects:
            if item['id'] in by_id:item['metadata']=by_id[item['id']].metadata
    report.stats['prototype_interactions']=count
