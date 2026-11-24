from datetime import date
import math

class InvalidForm(ValueError):pass
def attributes(fields,values):
    if not isinstance(values,dict) or set(values)-{f['key'] for f in fields}:raise InvalidForm('屬性欄位不符合指定表單')
    result={}
    for f in fields:
        value=values.get(f['key']);kind=f.get('type','text')
        if value is None or value=='' or value==[]:
            if f.get('required'):raise InvalidForm('必填欄位：'+f['label'])
            result[f['key']]=None;continue
        if kind in ['text','textarea']:
            if not isinstance(value,str) or len(value)>4000:raise InvalidForm(f['label']+' 最多 4000 字')
        elif kind in ['integer','number']:
            if isinstance(value,bool):raise InvalidForm(f['label']+' 需為數字')
            try:number=float(value)
            except (ValueError,TypeError):raise InvalidForm(f['label']+' 需為數字')
            if not math.isfinite(number) or (kind=='integer' and not number.is_integer()):raise InvalidForm(f['label']+' 數值無效')
            value=int(number) if kind=='integer' else number
        elif kind=='date':
            try:date.fromisoformat(value)
            except (ValueError,TypeError):raise InvalidForm(f['label']+' 日期無效')
        elif kind=='boolean':
            if type(value) is not bool:raise InvalidForm(f['label']+' 需為是／否')
        elif kind in ['select','multi']:
            if (kind=='multi' and not isinstance(value,list)) or (kind=='select' and not isinstance(value,str)):raise InvalidForm(f['label']+' 選項無效')
            selected=value if kind=='multi' else [value]
            if any(v not in f.get('options',[]) for v in selected):raise InvalidForm(f['label']+' 選項無效')
        else:raise InvalidForm('不支援此表單型別')
        result[f['key']]=value
    return result
