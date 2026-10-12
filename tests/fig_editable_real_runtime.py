"""Real-file layout/component behavior and editor responsiveness acceptance."""
import os, time, statistics
from pathlib import Path
from luma_canvas.fig import import_fig
from luma_canvas.editing import Editor
from luma_canvas.persistence import DocumentStore
from luma_canvas.layout_grids import grid_bands
from luma_canvas.model import Document

started=time.perf_counter()
result=import_fig(Path(os.environ['CANVAS_REAL_FIG']))
d=result.document
topics={f.topic for f in result.report.visible_findings}
assert not topics & {'auto_layout','layout_grids','instances','components_missing'},topics
objects=d.all_objects();definitions={o.id:o for o in objects if o.kind=='component'}
instances=[o for o in objects if o.kind=='instance']
assert len(instances)>=91 and all(o.component_source_id in definitions for o in instances)
assert any(p.name=='Imported components' for p in d.pages)
assert any(o.layout.get('auto') for o in objects)
frame=next(o for o in d.objects if o.metadata.get('layout_grids'))
grid=frame.metadata['layout_grids'][0]
assert len(grid_bands(grid,frame.width,frame.height))==grid.get('numSections',5)
button=next(o for o in d.objects if o.layout.get('auto') and o.layout.get('direction')=='horizontal' and o.layout.get('main_sizing')=='fixed' and o.layout.get('primary_align')=='centre' and d.children(o.id))
editor=Editor(d);original=d.to_dict()
children=d.children(button.id);assert children
before=[(o.id,o.x-button.x,o.y-button.y) for o in children]
editor.set_geometry(button.id,width=button.width+100)
assert [(o.id,o.x-button.x,o.y-button.y) for o in children]!=before,'Imported layout did not reflow'
editor.undo();assert d.to_dict()['objects']==original['objects']
# Edit a definition on its own page and prove paint propagation/Undo crosses pages.
source_id=next(o.component_source_id for o in instances if o.component_source_id not in {x.id for x in d.objects})
page=next(p for p in d.pages if any(o['id']==source_id for o in p.objects))
main=d.current_page_id;d.show_page(page.id);editor=Editor(d)
source=d.object(source_id);assert source
family=d.descendants(source.id)
target=next((o for o in family if o.kind=='text'),family[0])
clones=[o for o in d.all_objects() if o.metadata.get('component_origin_id')==target.id and 'opacity' not in o.overrides]
assert clones,'Missing imported child links'
editor.set_property([target.id],'opacity',.371)
updated={o.id:o for o in d.all_objects()}
assert all(updated[o.id].opacity==.371 for o in clones),'Cross-page master edit did not propagate'
editor.undo();updated={o.id:o for o in d.all_objects()}
assert all(updated[o.id].opacity==o.opacity for o in clones),'Undo did not restore instances'
d.show_page(main);d.validate()
path=Path(os.environ['DEV_ARTIFACT_DIR'])/'live-import.canvas'
DocumentStore.save(d,path);restored=DocumentStore().load(path);restored.validate()
assert len([o for o in restored.all_objects() if o.kind=='instance'])==len(instances)
assert restored.object(frame.id).metadata['layout_grids']==frame.metadata['layout_grids']
print('PASS real import grids, live resize/Undo, linked embedded masters, cross-page propagation/Undo, save/reopen; seconds',round(time.perf_counter()-started,2),flush=True)
