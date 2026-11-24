from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from core.models import *
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image,ImageDraw
from datetime import timedelta
import io,hashlib
class Command(BaseCommand):
    help='Create local demo accounts and sample photos without overwriting existing data'
    def handle(self,*args,**options):
        dept_names=['保育科','管理科','建管科','企劃科','水質科','秘書室','人事室','主計室','政風室']
        depts={name:Department.objects.get_or_create(name=name)[0] for name in dept_names}
        d=depts['保育科'];other=depts['管理科']
        users={}
        for name,role,department in [('admin','admin',d),('supervisor','supervisor',d),('member','member',d),('viewer','viewer',d),('other','member',other)]:
            u,created=User.objects.get_or_create(username=name)
            if created:u.set_password('LocalPhoto!2026');u.is_superuser=name=='admin';u.is_staff=name=='admin';u.save()
            Membership.objects.get_or_create(user=u,department=department,defaults={'role':role});users[name]=u
        fields=[{'key':'condition','label':'現場狀況','type':'select','required':True,'options':['正常','待處理','已處理']},{'key':'note','label':'現場說明','type':'text'}]
        item,_=Item.objects.get_or_create(department=d,name='道路巡查',defaults={'fields':fields})
        ds,_=Dataset.objects.get_or_create(item=item,name='本機測試資料集',defaults={'creator':users['member']})
        oi,_=Item.objects.get_or_create(department=other,name='環境巡查',defaults={'fields':fields})
        ods,_=Dataset.objects.get_or_create(item=oi,name='跨科室權限測試',defaults={'creator':users['other']})
        if not Photo.objects.exists():
            for n in range(7):
                im=Image.new('RGB',(1200,800),(155+n*9,180+n*5,176));draw=ImageDraw.Draw(im);draw.rectangle((0,510,1200,800),fill='#455c65');draw.polygon([(400,800),(600,300),(800,800)],fill='#788c93');draw.line((600,800,600,500),fill='#f0d58c',width=10);draw.text((45,45),f'LOCAL TEST PHOTO {n+1} - illustrative test image',fill='white');draw.rectangle((100+n*90,400,220+n*90,540),fill='#cc8f50');out=io.BytesIO();im.save(out,'JPEG');raw=out.getvalue()
                p=Photo(dataset=ods if n==6 else ds,uploader=users['other'] if n==6 else users['member'],name=f'local-sample-{n+1}.jpg',sha=hashlib.sha256(raw).hexdigest(),status='submitted',captured=timezone.now()-timedelta(days=n*15),time_confirmed=True,lat=25.033+n*.0001,lon=121.5654+n*.0001,form_snapshot=fields,attrs={'condition':'待處理' if n%2 else '正常','note':'系統產生的測試圖片，非真實現場'},tags=['測試照片'])
                p.file.save(f'seed-{n}.jpg',ContentFile(raw),save=False);p.preview.save(f'seed-{n}.jpg',ContentFile(raw),save=False);p.save()
        Config.objects.get_or_create(id=1)
        self.stdout.write('Local demo ready. Accounts: admin / supervisor / member / viewer / other. Password: LocalPhoto!2026')
