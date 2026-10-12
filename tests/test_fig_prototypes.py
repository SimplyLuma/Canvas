"""Binary .fig prototype decoding, destination resolution and reopened playback."""
import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from fig_writer import FigWriter,prototype_schema,solid,matrix
from luma_canvas.fig import import_fig
from luma_canvas.model import Document
from luma_canvas.prototype import Presentation


def prototype_fixture():
    writer=FigWriter();writer.schema=prototype_schema()
    home=writer.add('FRAME','Home',writer.page,size={'x':400,'y':240},fillPaints=[solid('#ff0000')],prototypeStartingPoint={'name':'Checkout'},scrollDirection='VERTICAL')
    detail=writer.add('FRAME','Detail',writer.page,size={'x':400,'y':240},transform=matrix(tx=500),fillPaints=[solid('#0000ff')])
    modal=writer.add('FRAME','Dialog',writer.page,size={'x':160,'y':100},transform=matrix(tx=1000),fillPaints=[solid('#00ff00')],overlayPositionType='BOTTOM_CENTER',overlayBackgroundInteraction='CLOSE_ON_CLICK_OUTSIDE',overlayBackgroundAppearance={'backgroundType':'SOLID_COLOR','backgroundColor':{'r':0,'g':0,'b':0,'a':.4}})
    interactions=[{'event':{'interactionType':'ON_CLICK'},'actions':[{'connectionType':'INTERNAL_NODE','transitionNodeID':detail,'navigationType':'NAVIGATE','transitionType':'PUSH_FROM_TOP','transitionDuration':.35,'easingType':'OUT_CUBIC'}]},
      {'event':{'interactionType':'ON_KEY_DOWN','keyTrigger':{'keyCodes':[17,75]}},'actions':[{'connectionType':'INTERNAL_NODE','transitionNodeID':modal,'navigationType':'OVERLAY','transitionType':'DISSOLVE','transitionDuration':.15}]},
      {'event':{'interactionType':'AFTER_TIMEOUT','transitionTimeout':1.5},'actions':[{'connectionType':'INTERNAL_NODE','transitionNodeID':detail,'transitionType':'INSTANT_TRANSITION'}]}]
    button=writer.add('RECTANGLE','Continue',home,size={'x':100,'y':40},transform=matrix(tx=30,ty=30),fillPaints=[solid('#000000')],prototypeInteractions=interactions,scrollBehavior='FIXED_WHEN_CHILD_OF_SCROLLING_FRAME')
    writer.add('RECTANGLE','Back',detail,size={'x':100,'y':40},prototypeInteractions=[{'event':{'interactionType':'ON_CLICK'},'actions':[{'connectionType':'BACK'}]}])
    writer.add('RECTANGLE','Close',modal,size={'x':100,'y':40},prototypeInteractions=[{'event':{'interactionType':'ON_CLICK'},'actions':[{'connectionType':'CLOSE'}]}])
    return writer


def import_writer(writer):
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'prototype.fig';path.write_bytes(writer.build())
        return import_fig(path)


class FigPrototypeTests(unittest.TestCase):
    def test_binary_interactions_survive_import_and_reopen(self):
        result=import_writer(prototype_fixture());document=result.document
        self.assertEqual(len(document.connections),5)
        by_name={item.name:item for item in document.objects}
        click=next(link for link in document.connections if link.source_id==by_name['Continue'].id and link.trigger=='click')
        self.assertEqual(click.transition,'push-down');self.assertEqual(click.duration_ms,350)
        self.assertEqual(click.easing,'ease-out')
        self.assertTrue(by_name['Home'].metadata['prototype_start']);self.assertTrue(by_name['Continue'].metadata['prototype_fixed'])
        keyboard=next(link for link in document.connections if link.trigger=='keyboard')
        self.assertEqual(keyboard.key,'<Control>k');self.assertEqual(keyboard.overlay_position,'bottom')
        self.assertAlmostEqual(keyboard.scrim_opacity,.4,places=5);self.assertTrue(keyboard.close_outside)
        timed=next(link for link in document.connections if link.trigger=='timed');self.assertEqual(timed.delay_ms,1500)
        document.validate();loaded=Document.from_dict(document.to_dict());loaded.validate()
        run=Presentation.begin(loaded);run.activate(by_name['Continue'].id,'click');self.assertEqual(run.frame.name,'Detail')
        run.activate(by_name['Back'].id);self.assertEqual(run.frame.name,'Home')
        run.activate(by_name['Continue'].id,'keyboard');self.assertEqual(run.overlays[-1][0],by_name['Dialog'].id)
        run.activate(by_name['Close'].id);self.assertFalse(run.overlays)
        self.assertEqual(result.report.stats['prototype_interactions'],5)

    def test_legacy_connection_and_missing_target(self):
        writer=FigWriter();writer.schema=prototype_schema()
        a=writer.add('FRAME','A',writer.page,size={'x':200,'y':100})
        b=writer.add('FRAME','B',writer.page,size={'x':200,'y':100})
        writer.add('RECTANGLE','Go',a,size={'x':30,'y':30},transitionNodeID=b,transitionType='DISSOLVE',transitionDuration=.2)
        writer.add('RECTANGLE','Missing',a,size={'x':30,'y':30},transitionNodeID={'sessionID':1,'localID':99999})
        result=import_writer(writer);self.assertEqual(len(result.document.connections),1)
        self.assertEqual(result.document.connections[0].duration_ms,200)
        self.assertTrue(result.report.has('prototype'))

    def test_sequential_variable_expression_and_conditional_navigation(self):
        writer=FigWriter();writer.schema=prototype_schema()
        a=writer.add('FRAME','A',writer.page,size={'x':200,'y':100})
        b=writer.add('FRAME','B',writer.page,size={'x':200,'y':100})
        literal=lambda value:{'value':{'floatValue':value}}
        variable=writer.add('VARIABLE','Count',writer.document,variableResolvedType='FLOAT',variableDataValues={'entries':[{'variableData':literal(0)}]})
        alias={'value':{'alias':{'guid':variable}}}
        addition={'value':{'expressionValue':{'expressionFunction':'ADDITION','expressionArguments':[alias,literal(1)]}}}
        condition={'value':{'expressionValue':{'expressionFunction':'GREATER_THAN','expressionArguments':[alias,literal(0)]}}}
        button=writer.add('RECTANGLE','Count and continue',a,size={'x':40,'y':40},prototypeInteractions=[{'event':{'interactionType':'ON_CLICK'},'actions':[
            {'connectionType':'SET_VARIABLE','targetVariableID':{'guid':variable},'targetVariableData':addition},
            {'connectionType':'CONDITIONAL','conditionalActions':[{'condition':condition,'actions':[{'connectionType':'INTERNAL_NODE','transitionNodeID':b}]}]}]}])
        result=import_writer(writer);document=Document.from_dict(result.document.to_dict());document.validate()
        self.assertEqual(len(document.connections),1);self.assertEqual(len(document.connections[0].script),2)
        run=Presentation.begin(document);run.activate(document.connections[0].source_id)
        self.assertEqual(document.variables['Canvas']['values']['Count']['value'],1)
        self.assertEqual(run.frame.name,'B');self.assertEqual(len(run.history),1)
        self.assertEqual(result.document.variables['Canvas']['values']['Count']['value'],0)

    def test_cross_page_destination_and_flow_start_resolve(self):
        writer=FigWriter();writer.schema=prototype_schema()
        a=writer.add('FRAME','A',writer.page,size={'x':200,'y':100})
        page2=writer.add('CANVAS','Second',writer.document)
        b=writer.add('FRAME','B',page2,size={'x':200,'y':100})
        writer.add('RECTANGLE','Go',a,size={'x':40,'y':40},transitionNodeID=b)
        next(item for item in writer.nodes if item['guid']==page2)['prototypeStartNodeID']=b
        result=import_writer(writer);document=result.document;document.validate()
        destination=next(item for item in document.all_objects() if item.name=='B')
        self.assertTrue(destination.metadata['prototype_start'])
        self.assertEqual(document.connections[0].destination_id,destination.id)

    def test_instance_tombstone_preserves_unrelated_master_interaction(self):
        writer=FigWriter();writer.schema=prototype_schema()
        home=writer.add('FRAME','Home',writer.page,size={'x':300,'y':200})
        target=writer.add('FRAME','Target',writer.page,size={'x':300,'y':200})
        master=writer.add('SYMBOL','Navigation',writer.page,size={'x':100,'y':40})
        child=writer.add('RECTANGLE','Home button',master,size={'x':30,'y':30},prototypeInteractions=[
            {'id':{'sessionID':1,'localID':900},'event':{'interactionType':'ON_CLICK'},'actions':[{'connectionType':'INTERNAL_NODE','transitionNodeID':target}]}])
        writer.add('INSTANCE','Navigation copy',home,size={'x':100,'y':40},symbolData={'symbolID':master,'symbolOverrides':[
            {'guidPath':{'guids':[child]},'prototypeInteractions':[{'id':{'sessionID':1,'localID':899},'isDeleted':True}]}]})
        removed=writer.add('INSTANCE','Explicitly disconnected',home,size={'x':100,'y':40},symbolData={'symbolID':master,'symbolOverrides':[
            {'guidPath':{'guids':[child]},'prototypeInteractions':[{'id':{'sessionID':1,'localID':900},'isDeleted':True}]}]})
        result=import_writer(writer);run=Presentation.begin(result.document)
        disconnected=next(o for o in result.document.objects if o.name=='Explicitly disconnected')
        removed_ids={o.id for o in result.document.descendants(disconnected.id)}
        self.assertFalse(any(link.source_id in removed_ids for link in result.document.connections))
        button=next(o for o in result.document.objects if o.name=='Home button' and '/' in o.metadata.get('figma_id','') and o.id not in removed_ids)
        self.assertTrue(any(link.source_id==button.id for link in result.document.connections))
        run.activate(button.id);self.assertEqual(run.frame.name,'Target')

    def test_interactive_component_changes_inside_instance(self):
        writer=FigWriter();writer.schema=prototype_schema()
        off=writer.add('SYMBOL','Off',writer.page,size={'x':100,'y':40},fillPaints=[solid('#ff0000')])
        on=writer.add('SYMBOL','On',writer.page,size={'x':100,'y':40},fillPaints=[solid('#0000ff')])
        writer.add('TEXT','Label',off,size={'x':80,'y':25},textData={'characters':'Off'},fontSize=16)
        writer.add('TEXT','Label',on,size={'x':80,'y':25},textData={'characters':'On'},fontSize=16)
        next(item for item in writer.nodes if item['guid']==off)['prototypeInteractions']=[{'event':{'interactionType':'ON_CLICK'},'actions':[{'connectionType':'INTERNAL_NODE','transitionNodeID':on,'navigationType':'SWAP_STATE','transitionType':'SMART_ANIMATE'}]}]
        next(item for item in writer.nodes if item['guid']==on)['prototypeInteractions']=[{'event':{'interactionType':'ON_CLICK'},'actions':[{'connectionType':'INTERNAL_NODE','transitionNodeID':off,'navigationType':'SWAP_STATE'}]}]
        home=writer.add('FRAME','Home',writer.page,size={'x':400,'y':240},prototypeStartingPoint={'name':'Flow'})
        writer.add('INSTANCE','Switch',home,size={'x':100,'y':40},transform=matrix(tx=40,ty=50),symbolData={'symbolID':off})
        result=import_writer(writer);document=Document.from_dict(result.document.to_dict())
        instance=next(item for item in document.objects if item.name=='Switch')
        run=Presentation.begin(document);self.assertEqual(run.frame.name,'Home')
        run.activate(instance.id);updated=document.object(instance.id)
        self.assertEqual((updated.x,updated.y),(40,50));self.assertEqual(updated.fill,'#0000ff')
        self.assertEqual(document.children(instance.id)[0].text,'On');self.assertEqual(run.frame.name,'Home')
        run.activate(instance.id);self.assertEqual(document.object(instance.id).fill,'#ff0000')
        document.validate()

    def test_malformed_script_is_rejected(self):
        from luma_canvas.model import PrototypeConnection,InvalidDocument
        for script in ([42],[{'action':'execute-code'}],[{'action':'conditional','blocks':[42]}]):
            with self.assertRaises(InvalidDocument):
                PrototypeConnection('bad','source','target',script=script).validate({'source','target'})
