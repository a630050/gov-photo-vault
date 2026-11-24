import hashlib,hmac,io,json,uuid
from datetime import timedelta
from pathlib import Path
from PIL import Image,ImageOps,UnidentifiedImageError
from django.conf import settings
from django.db import transaction,IntegrityError,OperationalError
from django.db.models import Sum
from django.http import JsonResponse,FileResponse,Http404
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie,csrf_exempt
from django.middleware.csrf import rotate_token
from django.core.files.base import ContentFile
from .models import Admission,Upload,LoginThrottle
from transfer_common.image_metadata import extract_metadata
from transfer_common.forms import attributes,InvalidForm

MAX_FILE=10*1024*1024
class Problem(Exception):
    def __init__(self,message,status=400):self.message=message;self.status=status

def fail(e):return JsonResponse({'error':e.message},status=e.status)
def csrf_failure(request,reason=''):return JsonResponse({'error':'工作階段驗證失敗，請重新整理'},status=403)
def admission(request,lock=False):
    id=request.session.get('admission')
    q=Admission.objects.select_for_update() if lock else Admission.objects
    a=q.filter(id=id,active=True,expires__gt=timezone.now()).first() if id else None
    if not a:raise Problem('請使用有效的上傳通行碼登入；通行碼可能已到期或被撤銷',401)
    return a
def row(u):
    return {'id':str(u.id),'name':u.name,'size':u.size,'state':u.state,'message':u.message,'metadata':u.metadata,'attrs':u.attrs,'preview':'/preview/'+str(u.id),'created':u.created.isoformat()}

@ensure_csrf_cookie
def home(request):return render(request,'gateway.html')
def static_file(request,name):
    if name not in ['gateway.js','gateway.css']:raise Http404
    return FileResponse((Path(__file__).parent/'static'/name).open('rb'),content_type='text/javascript' if name.endswith('.js') else 'text/css')
def preview(request,id):
    try:
        a=admission(request);u=Upload.objects.filter(id=id,admission=a).first()
        if not u:raise Problem('找不到本人上傳照片',404)
        r=FileResponse(u.preview.open('rb'),content_type='image/jpeg');r['Cache-Control']='private, no-store';return r
    except Problem as e:return fail(e)
    except OSError:return JsonResponse({'error':'暫存預覽暫不可用'},status=503)

def api(request,path):
    try:
        data=json.loads(request.body or '{}') if request.content_type=='application/json' else {}
        if not isinstance(data,dict):raise Problem('資料格式錯誤',422)
        if path=='login' and request.method=='POST':
            key=hashlib.sha256((settings.SECRET_KEY+request.META.get('REMOTE_ADDR','')).encode()).hexdigest()
            now=timezone.now()
            with transaction.atomic():
                throttle,_=LoginThrottle.objects.select_for_update().get_or_create(key=key,defaults={'since':now})
                if throttle.since<now-timedelta(minutes=15):throttle.since=now;throttle.failures=0
                if throttle.failures>=10:raise Problem('登入嘗試過多，請稍後再試',429)
                token=str(data.get('code','')).strip()
                try:id=uuid.UUID(token.split('.')[0])
                except ValueError:id=None
                a=Admission.objects.select_for_update().filter(id=id,active=True,redeemed=False,expires__gt=now).first() if id else None
                if not a or not hmac.compare_digest(a.token_sha,hashlib.sha256(token.encode()).hexdigest()):
                    throttle.failures+=1;throttle.save();return JsonResponse({'error':'通行碼無效、已使用、到期或已撤銷'},status=401)
                a.redeemed=True;a.save(update_fields=['redeemed']);throttle.failures=0;throttle.save()
                request.session.cycle_key();request.session['admission']=str(a.id);request.session.set_expiry(a.expires);rotate_token(request)
                return JsonResponse({'ok':True})
        if path=='logout' and request.method=='POST':request.session.flush();return JsonResponse({'ok':True})
        a=admission(request)
        if path=='me' and request.method=='GET':
            return JsonResponse({'username':a.username,'destination':a.label,'fields':a.fields,'expires':a.expires.isoformat(),'max_files':a.max_files,'max_bytes':a.max_bytes,'uploads':[row(u) for u in a.uploads.order_by('-created')]})
        if request.method!='POST':raise Problem('不支援的方法',405)
        if path=='stage':
            files=request.FILES.getlist('files')
            if not 1<=len(files)<=5:raise Problem('每批請選擇 1 至 5 張照片',422)
            results=[]
            for f in files:
                stored=[]
                try:
                    if f.size>MAX_FILE:raise Problem('每張照片最多 10 MB',422)
                    raw=f.read();sha=hashlib.sha256(raw).hexdigest()
                    with Image.open(io.BytesIO(raw)) as im:
                        if im.format not in ['JPEG','PNG','WEBP'] or im.width*im.height>40000000:raise Problem('僅接受 4000 萬像素以內的 JPEG／PNG／WebP',422)
                        im.load();captured,confirmed,lat,lon,extracted=extract_metadata(im)
                        image=ImageOps.exif_transpose(im).convert('RGB');image.thumbnail((1200,1200));out=io.BytesIO();image.save(out,'JPEG',quality=85)
                    with transaction.atomic():
                        a=admission(request,lock=True)
                        existing=a.uploads.filter(sha=sha).first()
                        if existing:results.append({'name':f.name,'ok':True,'existing':True,'id':str(existing.id)});continue
                        count=a.uploads.count();used=a.uploads.aggregate(n=Sum('size'))['n'] or 0
                        if count>=a.max_files or used+len(raw)>a.max_bytes:raise Problem('此通行碼的張數或容量額度已用完',422)
                        u=Upload(admission=a,name=Path(f.name.replace('\\','/')).name[:255],sha=sha,size=len(raw),metadata={'captured':captured.isoformat() if captured else None,'time_confirmed':confirmed,'lat':lat,'lon':lon,'extracted':extracted})
                        u.file.save(uuid.uuid4().hex+'.image',ContentFile(raw),save=False);stored.append(u.file)
                        u.preview.save(uuid.uuid4().hex+'.jpg',ContentFile(out.getvalue()),save=False);stored.append(u.preview);u.save()
                    stored=[];results.append({'name':f.name,'ok':True,'id':str(u.id)})
                except Problem as e:results.append({'name':f.name,'error':e.message})
                except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):results.append({'name':f.name,'error':'照片無法解碼或暫存失敗'})
                finally:
                    for field in stored:
                        try:field.delete(save=False)
                        except OSError:pass
            return JsonResponse({'results':results})
        if path=='commit':
            values=data.get('uploads')
            if not isinstance(values,list) or not 1<=len(values)<=50:raise Problem('請選取 1 至 50 張照片提交',422)
            with transaction.atomic():
                a=admission(request,lock=True)
                ids=[uuid.UUID(v['id']) for v in values]
                if len(set(ids))!=len(ids):raise Problem('照片不可重複選取',422)
                uploads={u.id:u for u in Upload.objects.select_for_update().filter(id__in=ids,admission=a)}
                if len(uploads)!=len(ids):raise Problem('含有非本人或不存在的暫存照片',404)
                for v,id in zip(values,ids):
                    u=uploads[id];attrs=attributes(a.fields,v.get('attrs',{}))
                    if u.state!='staged':
                        if u.attrs==attrs:continue
                        raise Problem('已提交照片不能由外網修改，請回內網處理',409)
                    u.attrs=attrs;u.state='pending';u.message='閘道已接收，等待內網拉取，尚未完成內網提交';u.save(update_fields=['attrs','state','message','updated'])
            return JsonResponse({'ok':True,'count':len(ids)})
        raise Problem('找不到上傳功能',404)
    except Problem as e:return fail(e)
    except (ValueError,TypeError,KeyError,InvalidForm):return JsonResponse({'error':'資料格式或必填欄位不正確，請檢查表單'},status=422)
    except (IntegrityError,OperationalError):return JsonResponse({'error':'資料正被更新，請重新整理重試'},status=409)
    except Admission.DoesNotExist:return JsonResponse({'error':'通行碼已失效'},status=401)

@csrf_exempt
def bridge(request,path):
    supplied=request.headers.get('Authorization','')
    if not hmac.compare_digest(supplied.encode(),('Bearer '+settings.GATEWAY_CONFIG['bridge_key']).encode()):return JsonResponse({'error':'拒絕存取'},status=403)
    try:
        data=json.loads(request.body or '{}') if request.method=='POST' else {}
        if path=='admissions' and request.method=='POST':
            with transaction.atomic():
                for g in data['grants']:
                    if len(g['fields'])>100 or not 1<=g['max_files']<=50 or not 1<=g['max_bytes']<=500*1024*1024:raise Problem('通行碼額度無效',422)
                    Admission.objects.update_or_create(id=uuid.UUID(g['id']),defaults={'token_sha':g['token_sha'],'label':g['label'][:250],'username':g['username'][:150],'fields':g['fields'],'expires':parse_datetime(g['expires']),'max_files':g['max_files'],'max_bytes':g['max_bytes'],'active':g['active']})
            return JsonResponse({'ok':True})
        if path=='pending' and request.method=='GET':
            return JsonResponse({'uploads':[{'id':str(u.id),'grant':str(u.admission_id),'name':u.name,'sha':u.sha,'size':u.size,'attrs':u.attrs} for u in Upload.objects.filter(state='pending').order_by('created')[:10]]})
        if path.startswith('blob/') and request.method=='GET':
            u=Upload.objects.filter(id=uuid.UUID(path.split('/')[1]),state='pending').first()
            if not u:raise Problem('找不到待匯入檔案',404)
            response=FileResponse(u.file.open('rb'),content_type='application/octet-stream');response['Cache-Control']='no-store';return response
        if path=='ack' and request.method=='POST':
            if data['state'] not in ['imported','duplicate','rejected']:raise Problem('回報狀態無效',422)
            upload_id=uuid.UUID(str(data['id']));grant_id=uuid.UUID(str(data['grant']))
            with transaction.atomic():
                u=Upload.objects.select_for_update().filter(id=upload_id,admission_id=grant_id,sha=data['sha']).first()
                if not u:raise Problem('匯入回報不符',409)
                if u.state=='pending':u.state=data['state'];u.message=str(data['message'])[:250];u.save(update_fields=['state','message','updated'])
                elif u.state!=data['state']:raise Problem('已處理的回報不可覆寫',409)
            return JsonResponse({'ok':True})
        raise Problem('找不到接收介面',404)
    except Problem as e:return fail(e)
    except (ValueError,TypeError,KeyError):return JsonResponse({'error':'接收資料格式無效'},status=422)
    except (IntegrityError,OperationalError):return JsonResponse({'error':'資料正被更新，請重試'},status=409)
    except OSError:return JsonResponse({'error':'暫存檔案暫不可用'},status=503)
