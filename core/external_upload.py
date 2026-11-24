import hashlib,io,json,math,secrets,uuid
from datetime import timedelta
from pathlib import Path
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError,URLError
from PIL import Image,ImageOps,UnidentifiedImageError
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from .models import ExternalUploadGrant,ExternalImportReceipt,Dataset,Photo
from .permissions import allowed
from .image_metadata import extract_metadata
from transfer_common.config import config
from transfer_common.forms import attributes,InvalidForm

MAX_FILE=10*1024*1024
TERMINAL=['imported','duplicate','rejected']
class BridgeUnavailable(Exception):pass
class Rejected(Exception):pass
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise BridgeUnavailable('接收閘道發生重新導向，請檢查設定')

def remote(path,data=None,binary=False):
    cfg=config()
    request=Request(cfg['url'].rstrip('/')+'/bridge/'+path,data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,headers={'Authorization':'Bearer '+cfg['bridge_key'],'Content-Type':'application/json'})
    try:
        with build_opener(NoRedirect).open(request,timeout=8) as response:
            limit=MAX_FILE if binary else 32*1024*1024
            raw=response.read(limit+1)
            if len(raw)>limit:raise BridgeUnavailable('接收閘道回應超過限制')
            return raw if binary else json.loads(raw)
    except (OSError,HTTPError,URLError,ValueError):raise BridgeUnavailable('接收閘道暫不可用，請檢查連線與服務設定')

def grant_error(g):
    if g.revoked:return '上傳通行碼已撤銷'
    if g.expires<=timezone.now():return '上傳通行碼已到期'
    if not User.objects.filter(id=g.user_id,is_active=True).exists() or not allowed(g.user,g.dataset.item.department_id,'upload'):return '上傳者帳號或資料集權限已失效'
    if g.dataset.archived or not g.dataset.item.active:return '目的資料集已封存或表單已停用'
    if g.dataset.item_id!=g.form_id or g.dataset.item.version!=g.form_version or g.dataset.item.fields!=g.fields:return '指定表單已更新，請重新申請通行碼'
    return ''

def grants_for(u):return ExternalUploadGrant.objects.all() if u.is_superuser else ExternalUploadGrant.objects.filter(user=u)
def describe(g):
    return {'id':str(g.id),'user':g.user.username,'dataset':g.dataset_id,'dataset_name':g.dataset.name,'expires':g.expires.isoformat(),'max_files':g.max_files,'max_bytes':g.max_bytes,'revoked':g.revoked,'effective':not grant_error(g),'synced':g.synced.isoformat() if g.synced else None,'sync_error':g.sync_error,'receipts':[{'id':str(r.id),'state':r.state,'message':r.message,'photo':r.photo_id if r.state=='imported' else None,'updated':r.updated.isoformat()} for r in g.receipts.order_by('-updated')[:50]]}

def external_api(path,data,u):
    from .helpers import Problem,audit
    if path=='external/list':
        return JsonResponse({'configured':config(False) is not None,'gateway_url':config(False)['url'] if config(False) else None,'grants':[describe(g) for g in grants_for(u).select_related('dataset__item','user').order_by('-created')[:100]]})
    if path=='external/issue':
        if type(data.get('dataset')) is not int:raise Problem('資料集識別格式無效',422)
        dataset=Dataset.objects.select_related('item').filter(id=data['dataset']).first()
        if not dataset or not allowed(u,dataset.item.department_id,'upload'):raise Problem('沒有目的資料集上傳權限',403)
        if not User.objects.filter(id=u.id,is_active=True).exists():raise Problem('帳號已停用',403)
        if dataset.archived or not dataset.item.active:raise Problem('目的資料集已封存或表單已停用',409)
        if not config(False):raise Problem('請先準備本機接收閘道設定',503)
        hours=float(data.get('hours',4));files=int(data.get('max_files',20))
        if not math.isfinite(hours) or not 1<=hours<=24 or not 1<=files<=50:raise Problem('有效期須為 1 至 24 小時，張數須為 1 至 50 張',422)
        g=ExternalUploadGrant(id=uuid.uuid4(),user=u,dataset=dataset,form_id=dataset.item_id,form_version=dataset.item.version,fields=dataset.item.fields,expires=timezone.now()+timedelta(hours=hours),max_files=files,max_bytes=files*MAX_FILE)
        token=str(g.id)+'.'+secrets.token_urlsafe(32);g.token_sha=hashlib.sha256(token.encode()).hexdigest();g.save();audit(u,'external_grant_issue',g.id,{'dataset':dataset.id,'max_files':files,'expires':g.expires.isoformat()})
        return JsonResponse({'grant':describe(g),'code':token,'gateway_url':config()['url']})
    if path=='external/revoke':
        id=uuid.UUID(str(data['id']))
        with transaction.atomic():
            g=grants_for(u).select_for_update().filter(id=id).first()
            if not g:raise Problem('找不到本人通行碼或沒有管理權限',404)
            g.revoked=True;g.synced=None;g.save(update_fields=['revoked','synced']);audit(u,'external_grant_revoke',g.id)
        return JsonResponse({'ok':True})
    raise Problem('找不到外網上傳管理功能',404)

def publish_grants():
    rows=list(ExternalUploadGrant.objects.select_related('dataset__item','user').order_by('-created'))
    if not rows:return 0
    docs=[{'id':str(g.id),'token_sha':g.token_sha,'username':g.user.username,'label':g.dataset.item.department.name+'／'+g.dataset.name,'fields':g.fields,'expires':g.expires.isoformat(),'max_files':g.max_files,'max_bytes':g.max_bytes,'active':not grant_error(g)} for g in rows]
    try:
        for offset in range(0,len(docs),5):remote('admissions',{'grants':docs[offset:offset+5]})
        ExternalUploadGrant.objects.filter(id__in=[g.id for g in rows]).update(synced=timezone.now(),sync_error='')
    except BridgeUnavailable as e:
        ExternalUploadGrant.objects.filter(id__in=[g.id for g in rows]).update(sync_error=str(e));raise
    return len(rows)

def ack(meta,state,message):return remote('ack',{'id':meta['id'],'grant':meta['grant'],'sha':meta['sha'],'state':state,'message':message})

def receive(meta):
    """Reserve a lease, fetch from DMZ, revalidate, then atomically create one draft."""
    from .helpers import audit,validate
    id=uuid.UUID(meta['id']);grant_id=uuid.UUID(meta['grant'])
    size=int(meta['size']);sha=str(meta['sha'])
    if not 1<=size<=MAX_FILE or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):return 'rejected','檔案大小或雜湊格式無效'
    with transaction.atomic():
        g=ExternalUploadGrant.objects.select_for_update().select_related('user','dataset__item').filter(id=grant_id).first()
        if not g:return 'rejected','找不到內網授權，未匯入'
        existing=ExternalImportReceipt.objects.select_for_update().filter(id=id).first()
        if existing:
            if existing.grant_id!=grant_id or existing.sha!=sha or existing.size!=size:return 'rejected','收件識別與原紀錄不符，未匯入'
            if existing.state in TERMINAL:return existing.state,existing.message
            if existing.lease_until and existing.lease_until>timezone.now():return None,None
        reason=grant_error(g)
        if reason:return 'rejected',reason+'，未匯入'
        receipts=g.receipts.exclude(id=id)
        if receipts.count()>=g.max_files or (receipts.aggregate(n=Sum('size'))['n'] or 0)+size>g.max_bytes:return 'rejected','內網授權額度已滿，未匯入'
        lease=timezone.now()+timedelta(minutes=2)
        r,_=ExternalImportReceipt.objects.update_or_create(id=id,defaults={'grant':g,'sha':sha,'size':size,'state':'importing','lease_until':lease,'message':'正在內網驗證檔案'})
    stored=[]
    try:
        raw=remote('blob/'+str(id),binary=True)
        if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=sha:raise Rejected('檔案大小或 SHA-256 不符，未匯入')
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in ['JPEG','PNG','WEBP'] or im.width*im.height>40000000:raise Rejected('圖片格式或尺寸不符，未匯入')
                extension={'JPEG':'.jpg','PNG':'.png','WEBP':'.webp'}[im.format];im.load();captured,confirmed,lat,lon,extracted=extract_metadata(im)
                image=ImageOps.exif_transpose(im).convert('RGB');image.thumbnail((1800,1800));out=io.BytesIO();image.save(out,'JPEG',quality=88)
        except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):raise Rejected('內網無法解碼此圖片，未匯入')
        with transaction.atomic():
            g=ExternalUploadGrant.objects.select_for_update().select_related('user','dataset__item').get(id=grant_id)
            r=ExternalImportReceipt.objects.select_for_update().get(id=id)
            if r.state!='importing' or r.lease_until!=lease:return None,None
            reason=grant_error(g)
            if reason:raise Rejected(reason+'，未匯入')
            try:attrs=attributes(g.fields,meta.get('attrs',{}))
            except InvalidForm as e:raise Rejected(str(e)+'，未匯入')
            duplicate=Photo.objects.filter(dataset=g.dataset,sha=sha).first()
            if duplicate:r.state='duplicate';r.message='資料集已有相同檔案，未重複匯入';r.photo=duplicate
            else:
                name=Path(str(meta.get('name','upload')).replace('\\','/')).name[:255]
                name=''.join(c for c in name if ord(c)>=32 and c!='\x7f') or 'upload'+extension
                p=Photo(dataset=g.dataset,uploader=g.user,name=name,sha=sha,status='draft',form_snapshot=g.fields,form_version=g.form_version,captured=captured,time_confirmed=confirmed,lat=lat,lon=lon,extracted=extracted,attrs=attrs)
                validate(p)
                p.file.save(uuid.uuid4().hex+extension,ContentFile(raw),save=False);stored.append(p.file)
                p.preview.save(uuid.uuid4().hex+'.jpg',ContentFile(out.getvalue()),save=False);stored.append(p.preview);p.save()
                r.state='imported';r.photo=p;r.message='已匯入內網草稿，請回內網確認後正式提交'
                audit(g.user,'external_import',p.id,{'receipt':str(r.id),'grant':str(g.id),'sha':sha})
            r.lease_until=None;r.save();stored=[]
        return r.state,r.message
    except Rejected as e:
        ExternalImportReceipt.objects.filter(id=id,state='importing',lease_until=lease).update(state='rejected',message=str(e)[:250],lease_until=None)
        return 'rejected',str(e)[:250]
    except (BridgeUnavailable,OSError):
        ExternalImportReceipt.objects.filter(id=id,state='importing',lease_until=lease).update(state='retry',message='暫時無法拉取或儲存檔案，等待重試',lease_until=None)
        raise BridgeUnavailable('暫時無法拉取或儲存檔案，等待重試')
    finally:
        for field in stored:
            try:field.delete(save=False)
            except OSError:pass

def bridge_once():
    published=publish_grants();pending=remote('pending')['uploads'];processed=0
    if not isinstance(pending,list) or len(pending)>10:raise BridgeUnavailable('接收閘道待辦格式無效')
    for meta in pending:
        state,message=receive(meta)
        if state:ack(meta,state,message);processed+=1
    return published,processed
