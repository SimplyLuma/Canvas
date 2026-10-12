# SPDX-License-Identifier: Apache-2.0
"""Undoable linked design resources; values persist with their explicit uses."""
import copy
from .model import Style, new_id
from .paint import parse_hex, solid


class Resources:
    def __init__(self, editor):
        self.editor = editor
        self.document = editor.document

    def create_style(self, item, name, kind):
        item=self.document.object(item.id)
        if not name.strip():raise ValueError('Enter a style name')
        value = ({'value':item.fill or '#000000'} if kind == 'color' else
                 {key:copy.deepcopy(item.metadata.get(key,default)) for key,default in
                  (('font_family','Figtree'),('font_size',16),('font_weight',400),('line_height',{'unit':'percent','value':120}))})
        style = Style(new_id('style'),name.strip(),kind,value)
        with self.editor.transaction('Create style'):
            self.document.styles.append(style)
            self.apply_style(item,style.id)
        return style

    def apply_style(self, item, style_id):
        item=self.document.object(item.id)
        style = next(style for style in self.document.styles if style.id==style_id)
        if style.kind=='text' and item.kind!='text':raise ValueError('Select a text layer')
        with self.editor.transaction('Apply style'):
            if style.kind=='color':self.editor.set_paint([item.id],'fill',solid(parse_hex(style.value['value'])))
            else:
                text_value={**style.value}
                for old,new in (('family','font_family'),('size','font_size'),('weight','font_weight')):
                    if old in text_value:text_value[new]=text_value.pop(old)
                self.editor.set_property([item.id],'metadata',{**item.metadata,**text_value})
            item.style_ids = [key for key in item.style_ids if not any(s.id==key and s.kind==style.kind for s in self.document.styles)] + [style.id]
            self.document.touch()

    def update_style(self, style_id, name, value):
        style = next(style for style in self.document.styles if style.id==style_id)
        if not name.strip():raise ValueError('Enter a style name')
        if style.kind=='color' and parse_hex(value.get('value','')) is None:raise ValueError('Enter a valid color')
        with self.editor.transaction('Update style'):
            style.name,style.value=name.strip(),copy.deepcopy(value)
            for item in list(self.document.objects):
                if style.id in item.style_ids:self.apply_style(item,style.id)
            self.document.touch()

    def remove_style(self, style_id):
        with self.editor.transaction('Remove style'):
            self.document.styles = [style for style in self.document.styles if style.id!=style_id]
            for item in self.document.objects:item.style_ids = [key for key in item.style_ids if key!=style_id]
            self.document.touch()

    def variables(self):
        return self.document.variables.get('Canvas',{}).get('values',{})

    def set_variable(self, name, value, kind):
        name=name.strip()
        if not name:raise ValueError('Enter a variable name')
        if kind=='color':
            if parse_hex(str(value)) is None:raise ValueError('Enter a valid color')
        elif kind=='number':
            import math
            value=float(value)
            if not math.isfinite(value):raise ValueError('Enter a finite number')
        elif kind=='text':value=str(value)
        elif kind=='boolean':
            if isinstance(value,str):
                if value.lower() not in {'true','false'}:raise ValueError('Enter true or false')
                value=value.lower()=='true'
            if not isinstance(value,bool):raise ValueError('Enter true or false')
        else:raise ValueError('Unknown variable type')
        with self.editor.transaction('Update variable'):
            collection=copy.deepcopy(self.document.variables.get('Canvas',{}))
            collection.setdefault('values',{})[name]={'type':kind,'value':value}
            self.document.variables={**self.document.variables,'Canvas':collection}
            for item in list(self.document.objects):
                for field,linked in list(item.metadata.get('variable_bindings',{}).items()):
                    if linked==name:self.bind_variable(item,field,name)
            self.document.touch()

    def bind_variable(self,item,field,name):
        item=self.document.object(item.id)
        variable=self.variables()[name]
        permitted={'color':{'fill','stroke'},'number':{'width','height','radius','opacity','stroke_width'},'text':{'text'},'boolean':{'visible'}}
        if field not in permitted[variable['type']]:raise ValueError('Variable type does not match the field')
        with self.editor.transaction('Apply variable'):
            value=variable['value']
            if field in {'width','height'} and value<=0:raise ValueError('Size must be positive')
            if field in {'radius','stroke_width'} and value<0:raise ValueError('Value cannot be negative')
            if field=='opacity' and not 0<=value<=1:raise ValueError('Opacity must be between 0 and 1')
            if field in {'width','height'}:self.editor.set_geometry(item.id,**{field:value})
            elif field in {'fill','stroke'}:self.editor.set_paint([item.id],field,solid(parse_hex(value)))
            else:self.editor.set_property([item.id],field,value)
            item.metadata={**item.metadata,'variable_bindings':{**item.metadata.get('variable_bindings',{}),field:name}}
            self.document.touch()

    def remove_variable(self,name):
        with self.editor.transaction('Remove variable'):
            collection=copy.deepcopy(self.document.variables['Canvas']);collection['values'].pop(name,None)
            self.document.variables={**self.document.variables,'Canvas':collection}
            for item in self.document.objects:
                item.metadata={**item.metadata,'variable_bindings':{field:key for field,key in item.metadata.get('variable_bindings',{}).items() if key!=name}}
            self.document.touch()
