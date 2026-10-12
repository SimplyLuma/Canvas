# SPDX-License-Identifier: MPL-2.0
"""Figma variable literals, aliases and expressions for isolated prototype state."""
from .document import guid_of,guid_text
from .paints import hex_color
from ..prototype_values import evaluate


def expression(data,names,depth=0):
    if depth>48:raise ValueError('variable expression is nested too deeply')
    if not isinstance(data,dict):raise ValueError('missing variable expression')
    value=data.get('value',data)
    for key in ('boolValue','textValue','floatValue'):
        if key in value:return {'literal':value[key]}
    if 'colorValue' in value:return {'literal':hex_color(value['colorValue'])}
    if 'alias' in value:
        identifier=guid_of(value['alias'].get('guid'))
        name=names.get(identifier)
        if name is None:raise ValueError('variable alias is missing from this file')
        return {'variable':name}
    if 'expressionValue' in value:
        operation=value['expressionValue']
        return {'operation':operation.get('expressionFunction'),
            'arguments':[expression(item,names,depth+1) for item in operation.get('expressionArguments',[])]}
    raise ValueError('unsupported variable value')


def import_variables(document,fig,report):
    nodes=[node for node in fig.nodes.values() if node.get('type')=='VARIABLE']
    names={};used=set()
    for node in nodes:
        name=str(node.get('name') or 'Variable')
        if name in used:name+=' ('+guid_text(node['_guid'])+')'
        used.add(name);names[node['_guid']]=name
    values={};pending=[]
    types={'BOOLEAN':'boolean','FLOAT':'number','STRING':'text','COLOR':'color'}
    defaults={'boolean':False,'number':0.,'text':'','color':'#000000'}
    for node in nodes:
        name=names[node['_guid']];kind=types.get(node.get('variableResolvedType'))
        if kind is None:report.note('prototype',name,extra='unsupported variable type');continue
        entries=(node.get('variableDataValues') or {}).get('entries',[])
        data=entries[0].get('variableData') if entries else node.get('variableData')
        try:expr=expression(data,names)
        except ValueError as error:report.note('prototype',name,extra=str(error));continue
        values[name]={'type':kind,'value':defaults[kind]};pending.append((name,expr))
    # Aliases may refer to a later variable. Resolve in dependency order.
    resolved=set()
    while pending:
        progress=False
        for name,expr in list(pending):
            def references(node):
                if 'variable' in node:return {node['variable']}
                return set().union(*(references(item) for item in node.get('arguments',[])))
            if not references(expr)<=resolved:continue
            try:values[name]['value']=evaluate(expr,values)
            except ValueError as error:report.note('prototype',name,extra=str(error))
            pending.remove((name,expr));resolved.add(name);progress=True
        if not progress:
            for name,_ in pending:report.note('prototype',name,extra='cyclic or unresolved variable alias')
            break
    if values:document.variables['Canvas']={'values':values}
    return names
