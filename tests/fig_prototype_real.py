"""Inspect a supplied .fig using its own embedded schema, then import/save/reopen."""
import os,sys,json,collections,hashlib
from pathlib import Path
from luma_canvas.fig.container import open_fig
from luma_canvas.fig.document import decode,guid_of,guid_text
from luma_canvas.fig import import_fig
from luma_canvas.persistence import DocumentStore

path=Path(sys.argv[1]);archive=open_fig(path);fig=decode(archive)
reactions=[];legacy=[];flows=[]
for node in fig.nodes.values():
    if node.get('prototypeStartingPoint') or guid_of(node.get('prototypeStartNodeID')):
        flows.append({'id':guid_text(node['_guid']),'name':node.get('name'),'start':node.get('prototypeStartingPoint'),'legacy_start':node.get('prototypeStartNodeID')})
    for reaction in node.get('prototypeInteractions') or []:
        if reaction.get('isDeleted'):continue
        reactions.append({'source':guid_text(node['_guid']),'name':node.get('name'),**reaction})
    if guid_of(node.get('transitionNodeID')):legacy.append({'source':guid_text(node['_guid']),'name':node.get('name'),'destination':node.get('transitionNodeID')})
raw={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'nodes':len(fig.nodes),'reactions':reactions,'legacy':legacy,'flows':flows}
output=Path(os.environ['DEV_ARTIFACT_DIR']);(output/'encoded-prototypes.json').write_text(json.dumps(raw,indent=2))
print('ENCODED',json.dumps({'sha256':raw['sha256'],'nodes':len(fig.nodes),'reactions':len(reactions),'legacy':len(legacy),'flows':flows,
 'triggers':dict(collections.Counter(item.get('event',{}).get('interactionType') for item in reactions)),
 'actions':dict(collections.Counter(action.get('connectionType') for item in reactions for action in item.get('actions',[]))),
 'navigation':dict(collections.Counter(action.get('navigationType') for item in reactions for action in item.get('actions',[])))}),flush=True)
fig.release();result=import_fig(path);document=result.document;document.validate()
(output/'import-report.json').write_text(json.dumps(result.report.to_dict(),indent=2))
(output/'imported-document.json').write_text(json.dumps(document.to_dict(),indent=2))
print('IMPORTED',json.dumps({'objects':len(document.all_objects()),'connections':len(document.connections),'pages':len(document.pages),'report':result.report.to_dict()}),flush=True)
store=DocumentStore();saved=output/'real-prototype.canvas';store.save(document,saved);loaded=store.load(saved)
loaded.validate();assert loaded.to_dict()==document.to_dict()
assert loaded.embedded_assets==document.embedded_assets
print('PASS real .fig import/save/reopen retains connections and embedded images',flush=True)
