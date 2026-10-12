# SPDX-License-Identifier: MPL-2.0
"""Map native Kiwi frame layout rules to editable Canvas layout data."""
from copy import deepcopy

def auto_layout(props):
    mode=props.get('stackMode')
    if mode not in {'HORIZONTAL','VERTICAL'} or props.get('stackWrap') in {'WRAP',True}:return None
    align={'MIN':'start','CENTER':'centre','MAX':'end','SPACE_BETWEEN':'space-between','STRETCH':'stretch'}
    hug=lambda value:'hug' if value in {'RESIZE_TO_FIT','RESIZE_TO_FIT_WITH_IMPLICIT_SIZE'} else 'fixed'
    common=props.get('stackPadding',0)
    left=props.get('stackHorizontalPadding',common);top=props.get('stackVerticalPadding',common)
    return {'auto':True,'direction':mode.lower(),'gap':props.get('stackSpacing',0),
        'padding':{'left':left,'top':top,'right':props.get('stackPaddingRight',left),'bottom':props.get('stackPaddingBottom',top)},
        'align':align.get(props.get('stackCounterAlignItems'),'start'),
        'primary_align':align.get(props.get('stackPrimaryAlignItems'),'start'),
        'main_sizing':hug(props.get('stackPrimarySizing')),'cross_sizing':hug(props.get('stackCounterSizing'))}

def layout_grids(props):
    return [deepcopy(grid) for grid in props.get('layoutGrids',[]) if isinstance(grid,dict) and
        grid.get('pattern','STRIPES') in {'STRIPES','GRID'} and grid.get('axis','X') in {'X','Y'} and
        grid.get('type','STRETCH') in {'STRETCH','MIN','MAX','CENTER'}]
