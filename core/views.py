import json,hashlib,io,math,uuid,zipfile,csv,re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from PIL import Image,ImageOps,ExifTags,UnidentifiedImageError
from django.http import JsonResponse,FileResponse,HttpResponse
from django.shortcuts import render
from django.contrib.auth import authenticate,login,logout
from django.contrib.auth.models import User
from django.views.decorators.csrf import ensure_csrf_cookie
from django.db import transaction,IntegrityError,OperationalError
from django.db.models import Q
from django.core.files.base import ContentFile
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from .models import *
from .image_metadata import extract_metadata
from .permissions import allowed,effective,memberships_data,capabilities_data,CAPABILITIES
from .search import search_photos,SearchError
from .comparison import nearby_photos
class Problem(Exception):
    def __init__(self,message,status=400): self.message,self.status=message,status

def audit(u,action,target,detail=None): Audit.objects.create(actor=u,action=action,target=str(target),detail=detail or {})
def dept_ids(u,manage=False):
    q=Membership.objects.filter(user=u)
    if manage:q=q.filter(role='supervisor')
    return list(q.values_list('department_id',flat=True))
def manager(u,department):return u.is_superuser or department in dept_ids(u,True)
def writable(u,department):return allowed(u,department,'organize')
def visible(u):
    q=Photo.objects.select_related('dataset__item','uploader')
    if u.is_superuser:return q
    q=q.filter(dataset__item__department_id__in=dept_ids(u))
    from django.db.models import Q
    return q.filter(Q(status='submitted')|Q(uploader=u,status='draft')|Q(dataset__item__department_id__in=dept_ids(u,True)))
def photo(u,id):
    p=visible(u).filter(id=id).first()
    if not p:raise Problem('找不到照片或沒有權限',404)
    return p

def editable(u,p,capability='organize'):
    if not allowed(u,p.dataset.item.department_id,capability):raise Problem('沒有整理權限',403)
    if p.dataset.archived:raise Problem('資料集已封存',409)
    if p.status=='recycled':raise Problem('照片已回收',409)
def dt(value):
    if not value:return None
    d=parse_datetime(value)
    if not d:raise Problem('拍攝時間格式錯誤')
    return timezone.make_aware(d,ZoneInfo('Asia/Taipei')) if timezone.is_naive(d) else d

def metadata(data):
    la,lo=data.get('lat'),data.get('lon')
    la=None if la in [None,''] else float(la);lo=None if lo in [None,''] else float(lo)
    if (la is None)!=(lo is None):raise Problem('經緯度必須一起填寫')
    if la is not None and not(math.isfinite(la) and math.isfinite(lo) and -90<=la<=90 and -180<=lo<=180):raise Problem('經緯度超出合法範圍')
    return {'captured':dt(data.get('captured')),'lat':la,'lon':lo,'time_confirmed':bool(data.get('time_confirmed',False)),'attrs':data.get('attrs',{})}
FIELD_TYPES={'text','textarea','integer','number','date','select','multi','boolean'}
def form_fields(value):
    if not isinstance(value,list) or len(value)>100:raise Problem('表單最多 100 個欄位')
    result=[];keys=set()
    for f in value:
        if not isinstance(f,dict):raise Problem('欄位格式錯誤')
        key=f.get('key','');label=str(f.get('label','')).strip();kind=f.get('type','text')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}',key) or key in keys:raise Problem('欄位識別無效或重複')
        if not label or len(label)>100:raise Problem('請填寫 1 至 100 字的欄位名稱')
        if kind not in FIELD_TYPES:raise Problem('不支援的欄位類型')
        if not isinstance(f.get('required',False),bool):raise Problem('必填設定格式錯誤')
        field={'key':key,'label':label,'type':kind,'required':f.get('required',False)}
        if kind in ['select','multi']:
            options=f.get('options',[])
            if not isinstance(options,list) or not options or len(options)>100 or any(not isinstance(x,str) or not x.strip() or len(x)>100 for x in options):raise Problem(f'{label} 需設定 1 至 100 個有效選項')
            options=[x.strip() for x in options]
            if len(set(options))!=len(options):raise Problem(f'{label} 的選項不可重複')
            field['options']=options
        keys.add(key);result.append(field)
    return result
def validate(p):
    if not isinstance(p.attrs,dict):raise Problem('屬性資料格式錯誤')
    for f in p.form_snapshot:
        v=p.attrs.get(f['key'])
        if f.get('required') and (v is None or v=='' or v==[]):raise Problem(f"必填欄位：{f['label']}")
        if v in [None,'',[]]:continue
        t=f.get('type','text')
        if t in ['integer','number']:
            try:n=float(v)
            except (ValueError,TypeError):raise Problem(f"{f['label']} 必須是數字")
            if not math.isfinite(n) or (t=='integer' and not n.is_integer()):raise Problem('數值格式錯誤')
        if t in ['select','multi']:
            if t=='multi' and not isinstance(v,list):raise Problem(f"{f['label']} 必須是多選資料")
            if t=='select' and not isinstance(v,str):raise Problem(f"{f['label']} 必須是單選資料")
            values=v if isinstance(v,list) else [v]
            if any(x not in f.get('options',[]) for x in values):raise Problem('選項不合法')
        if t=='boolean' and not isinstance(v,bool):raise Problem(f"{f['label']} 必須是是／否")
        if t=='date':
            try:datetime.strptime(v,'%Y-%m-%d')
            except (ValueError,TypeError):raise Problem(f"{f['label']} 日期格式錯誤")
        if t in ['text','textarea'] and not isinstance(v,str):raise Problem(f"{f['label']} 必須是文字")
def revision(p,u):
    Revision.objects.create(photo=p,version=p.version,actor=u,snapshot={'attrs':p.attrs,'captured':p.captured.isoformat() if p.captured else None,'lat':p.lat,'lon':p.lon,'dataset':p.dataset_id,'status':p.status,'fields':p.form_snapshot,'time_confirmed':p.time_confirmed})

def request_scope(u):
    from django.db.models import Q
    q=Request.objects.all()
    if u.is_superuser:return q
    return q.filter(photo__dataset__item__department_id__in=dept_ids(u)).filter(Q(author=u)|Q(photo__dataset__item__department_id__in=dept_ids(u,True)))
def photo_snapshot(p):
    return dict(attrs=p.attrs,captured=p.captured.isoformat() if p.captured else None,lat=p.lat,lon=p.lon,time_confirmed=p.time_confirmed,dataset=p.dataset_id,status=p.status,version=p.version,fields=p.form_snapshot)
def serialize_request(r,u):
    p=r.photo;is_manager=manager(u,p.dataset.item.department_id)
    return dict(id=r.id,photo_id=p.id,photo_name=p.name,department=p.dataset.item.department_id,dataset_name=p.dataset.name,author__username=r.author.username,kind=r.kind,reason=r.reason,payload=r.payload,before=r.before,state=r.state,created=r.created.isoformat(),closed_at=r.closed_at.isoformat() if r.closed_at else None,reviewer=r.reviewer.username if r.reviewer else None,decision_reason=r.decision_reason,can_review=r.state=='pending' and is_manager and (r.author_id!=u.id or u.is_superuser),can_withdraw=r.state=='pending' and r.author_id==u.id,can_open=visible(u).filter(id=p.id).exists(),stale=p.version!=r.base_version or p.status!='submitted',events=[dict(actor=e.actor.username if e.actor else '系統',action=e.action,detail=e.detail,created=e.created.isoformat()) for e in r.events.select_related('actor').order_by('created','id')])

def serialize(p,u):
    from .people import photo_people
    return dict(people=photo_people(p,u),can_download=allowed(u,p.dataset.item.department_id,'download'),extracted=p.extracted,id=p.id,name=p.name,dataset=p.dataset_id,item=p.dataset.item_id,department=p.dataset.item.department_id,status=p.status,captured=p.captured.isoformat() if p.captured else '',time_confirmed=p.time_confirmed,lat=p.lat,lon=p.lon,attrs=p.attrs,fields=p.form_snapshot,form_version=p.form_version,tags=p.tags,version=p.version,star=Star.objects.filter(photo=p,user=u).exists(),uploader=p.uploader.username,preview=f'/media/{p.id}/preview',original=f'/media/{p.id}/original',anomalies=list(Anomaly.objects.filter(photo=p).values('id','text','state','resolution')),can_write=writable(u,p.dataset.item.department_id),can_manage=manager(u,p.dataset.item.department_id))

@ensure_csrf_cookie
def home(request):
    from django.conf import settings
    return render(request,'index.html',{'csrf_cookie_name':settings.CSRF_COOKIE_NAME})
def asset(request,id,variant):
    try:
        if not request.user.is_authenticated:raise Problem('請先登入',401)
        p=photo(request.user,id)
        if variant not in ['original','preview']:raise Problem('找不到檔案',404)
        if variant=='original' and not allowed(request.user,p.dataset.item.department_id,'download'):raise Problem('沒有原圖下載權限',403)
        field=p.preview if variant=='preview' else p.file
        if not field:raise Problem('預覽未完成',404)
        response=FileResponse(field.open('rb'),as_attachment=variant=='original',filename=p.name if variant=='original' else 'preview.jpg')
        response['Cache-Control']='private, no-store'
        if variant=='original':audit(request.user,'download',p.id)
        return response
    except Problem as e:return JsonResponse({'error':e.message},status=e.status)
    except OSError:return JsonResponse({'error':'圖片暫不可用'},status=503)

def api(request,path):
    try:
        data=json.loads(request.body or '{}') if request.content_type=='application/json' else {}
        u=request.user;method=request.method
        if path=='login' and method=='POST':
            user=authenticate(request,username=data.get('username'),password=data.get('password'))
            if not user:raise Problem('帳號或密碼錯誤',401)
            login(request,user);audit(user,'login',user.id);return JsonResponse({'ok':True})
        if not u.is_authenticated:raise Problem('請先登入',401)
        if path.startswith('external/') and method=='POST':
            from .external_upload import external_api
            return external_api(path,data,u)
        if path.startswith('people/') and method=='POST':
            from .people import people_api
            return people_api(request,path,data,u)
        if path.startswith('semantic/') and method=='POST':
            from .semantic import semantic_api
            return semantic_api(path,data,u)
        if path.startswith('similarity/') and method=='POST':
            from .similarity import similarity_api
            return similarity_api(path,data,u)
        if path=='nearby' and method=='POST':
            try:
                with transaction.atomic():
                    base=photo(u,data['photo'])
                    result=nearby_photos(visible(u),u,base,data)
                    result['candidates']=[{**serialize(p,u),'distance_m':round(distance,2)} for p,distance in result.pop('rows')]
                    result['base']=serialize(base,u)
                    return JsonResponse(result)
            except SearchError as exc:raise Problem(str(exc),422)
        if path=='search' and method=='POST':
            try:
                with transaction.atomic():
                    result=search_photos(visible(u),u,data)
                    rows={p.id:p for p in visible(u).filter(id__in=result['ids'])}
                    result['photos']=[serialize(rows[id],u) for id in result['ids'] if id in rows]
                    return JsonResponse(result)
            except SearchError as exc:raise Problem(str(exc),422)
        if path=='logout' and method=='POST':logout(request);return JsonResponse({'ok':True})
        if path=='state' and method=='GET':
            deps=Department.objects.all() if u.is_superuser else Department.objects.filter(id__in=dept_ids(u))
            ids=list(deps.values_list('id',flat=True))
            photos=visible(u).order_by('-id')
            return JsonResponse(dict(user={'name':u.username,'admin':u.is_superuser,'managed':dept_ids(u,True),'writable':[d for d in ids if allowed(u,d,'upload')],'capabilities':capabilities_data(u)},departments=list(deps.values()),items=list(Item.objects.filter(department_id__in=ids).values()),datasets=list(Dataset.objects.filter(item__department_id__in=ids).values()),photos=[serialize(p,u) for p in photos],requests=[serialize_request(r,u) for r in request_scope(u).select_related('photo__dataset__item','author','reviewer').order_by('-id')],notices=list(Notice.objects.filter(user=u).order_by('-id').values()),radius=Config.objects.get_or_create(id=1)[0].radius,users=list(User.objects.values('id','username','is_active','is_superuser')) if u.is_superuser else [],memberships=memberships_data() if u.is_superuser else [],audits=list(Audit.objects.exclude(Q(action__startswith='person_')|Q(action__startswith='face_')).order_by('-id').values('id','actor__username','action','target','detail','created')[:100]) if u.is_superuser else []))
        if method!='POST':raise Problem('不支援的方法',405)
        with transaction.atomic():
            if path=='department':
                if not u.is_superuser:raise Problem('僅管理者可操作',403)
                x=Department.objects.create(name=data['name'].strip());audit(u,path,x.id)
            elif path=='item':
                dep=int(data['department'])
                if not allowed(u,dep,'forms'):raise Problem('沒有表單管理權限',403)
                Department.objects.get(id=dep)
                fields=form_fields(data.get('fields',[]));name=str(data.get('name','')).strip()
                if not name or len(name)>100:raise Problem('請填寫 1 至 100 字的表單名稱')
                if data.get('id'):
                    x=Item.objects.select_for_update().get(id=data['id'],department_id=dep)
                    if data.get('version')!=x.version:raise Problem('表單已被更新，請重新載入再編輯',409)
                    old={f['key']:f.get('type','text') for f in x.fields}
                    if any(f['key'] in old and old[f['key']]!=f['type'] for f in fields):raise Problem('改變欄位型別須建立新欄位識別')
                    FormPublication.objects.get_or_create(template=x,version=x.version,defaults={'name':x.name,'fields':x.fields})
                    x.fields=fields;x.version+=1;x.name=name;x.active=data.get('active',True);x.save()
                else:x=Item.objects.create(department_id=dep,name=name,fields=fields)
                FormPublication.objects.create(template=x,version=x.version,name=x.name,fields=fields,actor=u)
                audit(u,'publish_form',x.id,{'version':x.version,'fields':fields})
                return JsonResponse({'ok':True,'id':x.id,'version':x.version})
            elif path=='dataset':
                item=Item.objects.get(id=data['item'])
                if not allowed(u,item.department_id,'upload'):raise Problem('沒有上傳權限',403)
                Department.objects.select_for_update().get(id=item.department_id)
                if not item.active:raise Problem('表單已停用',409)
                name=str(data.get('name','')).strip()
                if not name or len(name)>120:raise Problem('請填寫 1 至 120 字的資料集名稱')
                if Dataset.objects.filter(item__department_id=item.department_id,name=name).exists():raise Problem('同科室已有同名資料集',409)
                x=Dataset.objects.create(item=item,name=name,creator=u);audit(u,path,x.id)
            elif path=='assign_form':
                x=Dataset.objects.select_for_update().select_related('item').get(id=data['id'])
                if not allowed(u,x.item.department_id,'forms'):raise Problem('沒有資料集表單管理權限',403)
                if data.get('version')!=x.version:raise Problem('資料集已變更，請重新載入',409)
                if x.archived:raise Problem('請先重新開放資料集再調整表單',409)
                target=Item.objects.get(id=data['item'])
                if target.department_id!=x.item.department_id:raise Problem('只能指定同科室表單',400)
                if not target.active:raise Problem('不能指定停用表單',409)
                old=x.item_id;x.item=target;x.version+=1;x.save();audit(u,path,x.id,{'before':old,'after':target.id,'existing_photos':'unchanged'})
            elif path=='archive':
                x=Dataset.objects.select_for_update().select_related('item').get(id=data['id'])
                if not manager(u,x.item.department_id):raise Problem('需要主管權限',403)
                x.archived=not x.archived;x.version+=1;x.save();audit(u,path,x.id,{'archived':x.archived})
            elif path=='rename':
                x=Dataset.objects.select_for_update().select_related('item').get(id=data['id'])
                if not (manager(u,x.item.department_id) or (x.creator==u and writable(u,x.item.department_id))):raise Problem('沒有改名權限',403)
                if int(data['version'])!=x.version:raise Problem('資料已變動',409)
                old=x.name;x.name=data['name'].strip()
                if not x.name or len(x.name)>120:raise Problem('名稱需 1 至 120 字')
                if Dataset.objects.filter(item__department_id=x.item.department_id,name=x.name).exclude(id=x.id).exists():raise Problem('同科室已有同名資料集',409)
                x.version+=1;x.save();audit(u,path,x.id,{'before':old,'after':x.name})
            elif path=='undo_tags':
                assertions=TagAssertion.objects.filter(batch=data['batch'],actor=u,active=True)
                pairs=list(assertions.values_list('photo_id','tag'))
                photos=list(visible(u).select_for_update().filter(id__in={id for id,_ in pairs}))
                if len(photos)!=len({id for id,_ in pairs}):raise Problem('照片權限已改變，無法撤回本批次',409)
                for p in photos:editable(u,p)
                assertions.update(active=False)
                for p in photos:
                    tags={tag for id,tag in pairs if id==p.id}
                    retained=set(TagAssertion.objects.filter(photo=p,tag__in=tags,active=True).values_list('tag',flat=True))
                    p.tags=[tag for tag in p.tags if tag not in tags or tag in retained];p.save(update_fields=['tags'])
                audit(u,path,data['batch'])
            elif path=='upload':
                ds=Dataset.objects.select_related('item').get(id=request.POST['dataset'])
                if not allowed(u,ds.item.department_id,'upload'):raise Problem('沒有上傳權限',403)
                if ds.archived or not ds.item.active:raise Problem('目的地不可上傳',409)
                expected=request.POST.get('form_version')
                if expected and (int(expected)!=ds.item.version or int(request.POST.get('form_id',0))!=ds.item_id):raise Problem('資料集指定表單已更新，請重新確認表單後上傳',409)
                files=request.FILES.getlist('files')
                if not files or len(files)>500:raise Problem('每批需 1 至 500 張')
                results=[]
                for f in files:
                    if f.size>50*1024*1024:results.append({'name':f.name,'error':'超過 50 MB'});continue
                    raw=f.read();sha=hashlib.sha256(raw).hexdigest()
                    if Photo.objects.filter(dataset=ds,sha=sha).exists():results.append({'name':f.name,'error':'此資料集已有相同檔案'});continue
                    try:
                        im=Image.open(io.BytesIO(raw));im.load()
                        if im.format not in ['JPEG','PNG','WEBP','HEIF','HEIC']:raise ValueError('不支援的圖片格式')
                        cap,time_confirmed,lat,lon,extracted=extract_metadata(im)
                        im=ImageOps.exif_transpose(im).convert('RGB');im.thumbnail((1800,1800));out=io.BytesIO();im.save(out,'JPEG',quality=88)
                        p=Photo(dataset=ds,uploader=u,name=Path(f.name).name,sha=sha,form_snapshot=ds.item.fields,form_version=ds.item.version,captured=cap,time_confirmed=time_confirmed,lat=lat,lon=lon,extracted=extracted)
                        p.file.save(uuid.uuid4().hex+Path(f.name).suffix.lower(),ContentFile(raw),save=False);p.preview.save(uuid.uuid4().hex+'.jpg',ContentFile(out.getvalue()),save=False);p.save();audit(u,'upload',p.id);results.append({'name':p.name,'id':p.id})
                    except (UnidentifiedImageError,ValueError,OSError,Image.DecompressionBombError) as e:results.append({'name':f.name,'error':'無法解碼或不支援格式'})
                return JsonResponse({'results':results})
            elif path in ['save','submit','star','tags','request','restore','anomaly','discard']:
                p=photo(u,data['photo']);p=Photo.objects.select_for_update().select_related('dataset__item','uploader').get(id=p.id)
                if path=='star':
                    s=Star.objects.filter(user=u,photo=p)
                    if s.exists():s.delete()
                    else:Star.objects.create(user=u,photo=p)
                elif path=='restore':
                    if not manager(u,p.dataset.item.department_id):raise Problem('需要主管權限',403)
                    if p.status!='recycled':raise Problem('照片未回收',409)
                    p.status=p.previous_status;p.version+=1;p.save()
                else:
                    if path=='request':
                        if not writable(u,p.dataset.item.department_id):raise Problem('沒有整理權限',403)
                        if p.status!='submitted':raise Problem('只可申請正式照片',409)
                    else:editable(u,p,'upload' if path in ['save','submit','discard'] else 'organize')
                    if path in ['save','submit','discard']:
                        if p.status!='draft' or (p.uploader!=u and not manager(u,p.dataset.item.department_id)):raise Problem('無法編輯此草稿',403)
                        if data.get('version')!=p.version:raise Problem('資料已變動，請重新載入',409)
                        if path=='discard':p.previous_status='draft';p.status='recycled'
                        else:
                            for k,v in metadata(data).items():setattr(p,k,v)
                            if path=='submit':validate(p);p.status='submitted'
                        p.version+=1;p.save()
                    elif path=='tags':
                        tags=[str(t).strip()[:60] for t in data.get('tags',[]) if str(t).strip()]
                        batch=data.get('batch') or str(uuid.uuid4())
                        for tag in p.tags:
                            if not TagAssertion.objects.filter(photo=p,tag=tag,active=True).exists():TagAssertion.objects.create(photo=p,tag=tag,batch='legacy',actor=p.uploader)
                        for tag in set(tags):TagAssertion.objects.create(photo=p,tag=tag,batch=batch,actor=u)
                        p.tags=sorted(set(p.tags+tags));p.save()
                        audit(u,path,p.id,{'batch':batch,'tags':tags})
                        return JsonResponse({'ok':True,'batch':batch})
                    elif path=='request':
                        kind=data['kind']
                        if kind not in ['delete','correct','move']:raise Problem('未知申請類型')
                        if not data.get('reason','').strip():raise Problem('請填理由')
                        if data.get('version') is not None and data['version']!=p.version:raise Problem('照片已變動，請重新檢查',409)
                        proposal=data.get('payload',{})
                        before=photo_snapshot(p)
                        if kind=='correct':
                            # Normalize the full proposed state without writing it to the photo.
                            combined={**before,**proposal};values=metadata(combined)
                            probe=Photo(form_snapshot=p.form_snapshot,attrs=values['attrs']);validate(probe)
                            proposal={**values,'captured':values['captured'].isoformat() if values['captured'] else None}
                            if Request.objects.filter(photo=p,kind='correct',state='pending').exists():raise Problem('此照片已有待審更正，請先處理或撤回',409)
                        if kind=='delete' and Request.objects.filter(photo=p,kind='delete',state='pending').exists():raise Problem('此照片已有待審刪除申請',409)
                        r=Request.objects.create(photo=p,author=u,kind=kind,reason=data['reason'],payload=proposal,before=before,base_version=p.version)
                        RequestEvent.objects.create(request=r,actor=u,action='submitted',detail={'reason':r.reason,'before':before,'proposed':proposal})
                        recipients=set(Membership.objects.filter(department_id=p.dataset.item.department_id,role='supervisor',user__is_active=True).values_list('user_id',flat=True))|set(User.objects.filter(is_superuser=True,is_active=True).values_list('id',flat=True))
                        for recipient in recipients-{u.id}:Notice.objects.create(user_id=recipient,text=f'照片申請 #{r.id} 待處理（{kind}）')
                    elif path=='anomaly':
                        if not data.get('text','').strip():raise Problem('請填異常類型與說明')
                        Anomaly.objects.create(photo=p,author=u,text=data['text'])
                audit(u,path,p.id)
            elif path=='decision':
                r=request_scope(u).select_for_update().select_related('photo__dataset__item','author').get(id=data['id']);p=r.photo
                if data.get('decision') not in ['withdraw','approve','reject']:raise Problem('未知審核動作')
                if data.get('decision')=='withdraw':
                    if r.author!=u:raise Problem('只能撤回本人申請',403)
                    state='withdrawn'
                else:
                    if not manager(u,p.dataset.item.department_id):raise Problem('需要主管權限',403)
                    if r.author==u and not u.is_superuser:raise Problem('本人申請需由他人核准',403)
                    state='approved' if data.get('decision')=='approve' else 'rejected'
                    if not data.get('reason','').strip():raise Problem('請填處理理由')
                if r.state!='pending':raise Problem('案件已結案',409)
                if state=='approved':
                    p=Photo.objects.select_for_update().select_related('dataset__item').get(id=p.id)
                    if p.version!=r.base_version or p.status!='submitted':raise Problem('照片已變動，請駁回並重新申請',409)
                    revision(p,u)
                    if r.kind=='delete':
                        p.previous_status=p.status;p.status='recycled'
                        for other in Request.objects.filter(photo=p,state='pending').exclude(id=r.id):
                            other.state='invalidated';other.closed_at=timezone.now();other.decision_reason='照片已回收，原申請失效';other.save()
                            RequestEvent.objects.create(request=other,actor=u,action='invalidated',detail={'reason':other.decision_reason,'caused_by_request':r.id})
                            Notice.objects.create(user=other.author,text=f'申請 #{other.id} 因照片回收而失效')
                    elif r.kind=='correct':
                        for k,v in metadata(r.payload).items():setattr(p,k,v)
                        validate(p)
                    elif r.kind=='move':
                        dest=Dataset.objects.get(id=r.payload['dataset'])
                        if dest.item_id!=p.dataset.item_id or dest.archived:raise Problem('只能移至同項目未封存資料集')
                        p.dataset=dest
                    p.version+=1;p.save()
                r.state=state;r.reviewer=u;r.decision_reason=data.get('reason','');r.closed_at=timezone.now();r.save()
                RequestEvent.objects.create(request=r,actor=u,action=state,detail={'reason':r.decision_reason,'after':photo_snapshot(p)})
                Notice.objects.create(user=r.author,text=f'申請 #{r.id} 已處理：{state}');audit(u,'decision',r.id,{'state':state,'reason':r.decision_reason,'before':r.before,'after':photo_snapshot(p)})
            elif path=='resolve':
                a=Anomaly.objects.select_related('photo__dataset__item').get(id=data['id'])
                if not manager(u,a.photo.dataset.item.department_id):raise Problem('需要主管權限',403)
                if data['state'] not in ['pending','confirmed','dismissed'] or not data.get('reason'):raise Problem('需填處理理由')
                a.state=data['state'];a.resolution=data['reason'];a.save();audit(u,path,a.id,{'state':a.state,'reason':a.resolution})
            elif path=='notice':Notice.objects.filter(user=u,id=data['id']).update(read=True)
            elif path=='export':
                ids=list(set(map(int,data['photos'])))
                if not ids or len(ids)>500:raise Problem('單次匯出需 1 至 500 張')
                photos=list(visible(u).filter(id__in=ids,status='submitted'))
                if len(photos)!=len(ids):raise Problem('照片狀態或權限已變動',409)
                if any(not allowed(u,p.dataset.item.department_id,'export') or not allowed(u,p.dataset.item.department_id,'download') for p in photos):raise Problem('需要批次匯出及原圖下載權限',403)
                output=io.BytesIO();csvout=io.StringIO();writer=csv.writer(csvout);writer.writerow(['id','name','dataset','captured','lat','lon','tags','attributes'])
                def safe(value):
                    text=str(value)
                    return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text
                with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
                    for p in photos:
                        with p.file.open('rb') as f:z.writestr(f'photos/{p.id}{Path(p.name).suffix}',f.read())
                        writer.writerow([p.id,safe(p.name),safe(p.dataset.name),p.captured.isoformat() if p.captured else '',p.lat,p.lon,safe(','.join(p.tags)),safe(json.dumps(p.attrs,ensure_ascii=False))])
                    z.writestr('metadata.csv',csvout.getvalue().encode('utf-8-sig'))
                e=Export(actor=u,photo_ids=ids);e.file.save(str(uuid.uuid4())+'.zip',ContentFile(output.getvalue()));audit(u,path,e.id,{'photos':ids})
                return JsonResponse({'ok':True,'url':f'/exports/{e.id}'})
            elif path=='membership':
                if not u.is_superuser:raise Problem('需要管理者權限',403)
                target=User.objects.select_for_update().get(id=data['user'])
                before={'active':target.is_active,'memberships':list(Membership.objects.filter(user=target).values('department_id','role','permissions'))}
                if data.get('active') is not None:
                    if not isinstance(data['active'],bool):raise Problem('帳號啟用設定必須為是或否')
                    if target.is_superuser and not data['active'] and User.objects.filter(is_superuser=True,is_active=True).count()<=1:raise Problem('不可停用最後管理者',409)
                    target.is_active=bool(data['active']);target.save()
                else:
                    Department.objects.get(id=data['department'])
                    if data.get('remove'):
                        Membership.objects.filter(user=target,department_id=data['department']).delete()
                    else:
                        role=data['role'];permissions=data.get('permissions',{})
                        if role not in ['member','supervisor','viewer']:raise Problem('角色錯誤')
                        if not isinstance(permissions,dict) or any(k not in CAPABILITIES or not isinstance(v,bool) for k,v in permissions.items()):raise Problem('權限設定格式錯誤')
                        Membership.objects.update_or_create(user=target,department_id=data['department'],defaults={'role':role,'permissions':permissions})
                audit(u,path,target.id,{'before':before,'after':{'active':target.is_active,'memberships':list(Membership.objects.filter(user=target).values('department_id','role','permissions'))}})
            elif path=='config':
                if not u.is_superuser:raise Problem('需要管理者權限',403)
                radius=int(data['radius'])
                if radius<100:raise Problem('半徑不得小於 100 公尺')
                Config.objects.update_or_create(id=1,defaults={'radius':radius});audit(u,path,1,{'radius':radius})
            elif path=='user':
                if not u.is_superuser:raise Problem('需要管理者權限',403)
                if data.get('role','member') not in ['member','supervisor','viewer']:raise Problem('角色錯誤')
                x=User.objects.create_user(data['username'],password=data['password'])
                Membership.objects.create(user=x,department_id=data['department'],role=data.get('role','member'));audit(u,path,x.id)
            else:raise Problem('找不到功能',404)
        return JsonResponse({'ok':True})
    except Problem as e:return JsonResponse({'error':e.message},status=e.status)
    except IntegrityError:return JsonResponse({'error':'名稱或帳號重複，請重新確認'},status=409)
    except OperationalError:return JsonResponse({'error':'資料正在被其他操作更新或暫時不可用，請重新整理後再試'},status=409)
    except (ValueError,KeyError,TypeError) as e:return JsonResponse({'error':'欄位格式錯誤或缺少必要資料'},status=422)
    except (Photo.DoesNotExist,Item.DoesNotExist,Dataset.DoesNotExist,Request.DoesNotExist,Anomaly.DoesNotExist,User.DoesNotExist,Department.DoesNotExist):return JsonResponse({'error':'找不到資料'},status=404)

def download_export(request,id):
    if not request.user.is_authenticated:return JsonResponse({'error':'請登入'},status=401)
    e=Export.objects.filter(id=id,actor=request.user).first()
    if not e:return JsonResponse({'error':'沒有權限'},status=403)
    if any(not allowed(request.user,p.dataset.item.department_id,'export') or not allowed(request.user,p.dataset.item.department_id,'download') for p in Photo.objects.filter(id__in=e.photo_ids).select_related('dataset__item')):return JsonResponse({'error':'匯出或下載權限已撤銷'},status=403)
    if timezone.now()-e.created>__import__('datetime').timedelta(days=7):return JsonResponse({'error':'下載包已到期'},status=410)
    if visible(request.user).filter(id__in=e.photo_ids,status='submitted').count()!=len(e.photo_ids):return JsonResponse({'error':'照片已變動，請重新匯出'},status=409)
    audit(request.user,'export_download',e.id)
    response=FileResponse(e.file.open('rb'),as_attachment=True,filename=f'photos-{e.id}.zip');response['Cache-Control']='private, no-store';return response
