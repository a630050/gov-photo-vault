from django.test import TestCase,Client,override_settings
from django.contrib.auth.models import User
from .models import *
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
import io,json
class CoreTests(TestCase):
    def setUp(self):
        self.d=Department.objects.create(name='A');self.other=Department.objects.create(name='B')
        self.member=User.objects.create_user('member',password='test');self.boss=User.objects.create_user('boss',password='test');self.out=User.objects.create_user('out',password='test')
        for u,d,r in [(self.member,self.d,'member'),(self.boss,self.d,'supervisor'),(self.out,self.other,'member')]:Membership.objects.create(user=u,department=d,role=r)
        self.item=Item.objects.create(department=self.d,name='巡查',fields=[{'key':'note','label':'說明','required':True}]);self.ds=Dataset.objects.create(item=self.item,name='現場',creator=self.member)
        self.p=Photo.objects.create(dataset=self.ds,uploader=self.member,name='test.jpg',sha='x',status='submitted',form_snapshot=self.item.fields,attrs={'note':'原始'})
        self.client.force_login(self.member)
    def post(self,path,data):return self.client.post('/api/'+path,json.dumps(data),content_type='application/json')
    def test_scope_and_direct_asset(self):
        self.client.force_login(self.out)
        s=self.client.get('/api/state').json()
        self.assertNotIn('photos',s)
        self.assertEqual(s['summary']['visible_photos'],0)
        self.assertEqual(self.post('search',{}).json()['photos'],[])
        self.assertEqual(self.client.get(f'/media/{self.p.id}/preview').status_code,404)
        self.assertEqual(self.post('star',{'photo':self.p.id}).status_code,404)
    def test_delete_review_restore_invalidates(self):
        self.assertEqual(self.post('request',{'photo':self.p.id,'kind':'correct','reason':'更新','payload':{'attrs':{'note':'修改'}}}).status_code,200)
        self.post('request',{'photo':self.p.id,'kind':'delete','reason':'誤傳'})
        r=Request.objects.get(kind='delete')
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'ok'}).status_code,403)
        self.client.force_login(self.boss)
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'核准'}).status_code,200)
        self.p.refresh_from_db();self.assertEqual(self.p.status,'recycled');self.assertEqual(Request.objects.get(kind='correct').state,'invalidated')
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'再次'}).status_code,409)
        self.post('restore',{'photo':self.p.id});self.p.refresh_from_db();self.assertEqual(self.p.status,'submitted')
    def test_self_approval_forbidden(self):
        self.client.force_login(self.boss);self.post('request',{'photo':self.p.id,'kind':'delete','reason':'test'});r=Request.objects.get()
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'test'}).status_code,403)
    def test_archive_blocks_shared_not_star(self):
        self.ds.archived=True;self.ds.save()
        self.assertEqual(self.post('tags',{'photo':self.p.id,'tags':['x']}).status_code,409)
        self.assertEqual(self.post('star',{'photo':self.p.id}).status_code,200)
    def test_upload_and_required_submit(self):
        im=Image.new('RGB',(30,20),'red');b=io.BytesIO();im.save(b,'JPEG')
        r=self.client.post('/api/upload',{'dataset':self.ds.id,'files':SimpleUploadedFile('photo.jpg',b.getvalue(),content_type='image/jpeg')})
        self.assertEqual(r.status_code,200);p=Photo.objects.get(id=r.json()['results'][0]['id'])
        try:
            data={'photo':p.id,'version':1,'attrs':{}}
            self.assertEqual(self.post('submit',data).status_code,400)
            data['attrs']={'note':'完成'};self.assertEqual(self.post('submit',data).status_code,200)
            self.assertEqual(self.post('save',data).status_code,403)
        finally:p.file.delete(save=False);p.preview.delete(save=False)
    def test_csrf_required(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.member)
        self.assertEqual(c.post('/api/star',json.dumps({'photo':self.p.id}),content_type='application/json').status_code,403)
    @override_settings(CSRF_COOKIE_NAME='photo_ops_csrf',SESSION_COOKIE_NAME='photo_ops_session')
    def test_deployment_cookie_login(self):
        c=Client(enforce_csrf_checks=True)
        response=c.get('/')
        self.assertContains(response,'<meta name="csrf-cookie" content="photo_ops_csrf">')
        token=c.cookies['photo_ops_csrf'].value
        response=c.post('/api/login',json.dumps({'username':'member','password':'test'}),content_type='application/json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,200)
        self.assertIn('photo_ops_session',c.cookies)
    def test_stars_private_and_deactivation(self):
        self.post('star',{'photo':self.p.id});self.client.force_login(self.boss)
        self.assertFalse(self.post('search',{}).json()['photos'][0]['star'])
        self.boss.is_active=False;self.boss.save();self.assertEqual(self.client.get('/api/state').status_code,401)
    def test_tag_undo_preserves_other_assertion(self):
        self.post('tags',{'photo':self.p.id,'tags':['積水'],'batch':'first'})
        self.client.force_login(self.boss);self.post('tags',{'photo':self.p.id,'tags':['積水'],'batch':'second'})
        self.client.force_login(self.member);self.post('undo_tags',{'batch':'first'})
        self.p.refresh_from_db();self.assertIn('積水',self.p.tags)
    def test_config_and_last_admin_guard(self):
        a=User.objects.create_superuser('admin',password='test');self.client.force_login(a)
        self.assertEqual(self.post('config',{'radius':99}).status_code,400)
        self.assertEqual(self.post('membership',{'user':a.id,'active':False}).status_code,409)
    def test_export_requires_explicit_local_admin(self):
        self.assertEqual(self.post('export',{'photos':[self.p.id]}).status_code,403)
    def test_form_schema_and_optimistic_publication(self):
        self.client.force_login(self.boss)
        data={'id':self.item.id,'department':self.d.id,'name':'新版','version':1,'fields':[{'key':'note','label':'說明','type':'text','required':True},{'key':'result','label':'處理結果','type':'select','options':['完成','待處理'],'required':True}]}
        self.assertEqual(self.post('item',data).status_code,200)
        self.assertEqual(self.post('item',data).status_code,409)
        self.p.refresh_from_db();self.assertEqual(self.p.form_snapshot,[{'key':'note','label':'說明','required':True}]);self.assertEqual(self.p.attrs,{'note':'原始'})
        self.assertEqual(FormPublication.objects.filter(template=self.item).count(),2)
    def test_reject_invalid_form_options_and_type(self):
        self.client.force_login(self.boss)
        base={'department':self.d.id,'name':'表單','fields':[{'key':'a','label':'狀態','type':'select','options':['重複','重複']}]}
        self.assertEqual(self.post('item',base).status_code,400)
        base['fields'][0]['options']=[];self.assertEqual(self.post('item',base).status_code,400)
        base['fields'][0]['type']='script';self.assertEqual(self.post('item',base).status_code,400)
    def test_form_assignment_same_department_and_snapshot(self):
        new=Item.objects.create(department=self.d,name='新範本',fields=[{'key':'new','label':'新欄位','type':'text'}])
        foreign=Item.objects.create(department=self.other,name='他科表單')
        self.client.force_login(self.boss)
        self.assertEqual(self.post('assign_form',{'id':self.ds.id,'version':1,'item':foreign.id}).status_code,400)
        self.assertEqual(self.post('assign_form',{'id':self.ds.id,'version':1,'item':new.id}).status_code,200)
        self.p.refresh_from_db();self.assertEqual(self.p.form_snapshot,self.item.fields);self.assertEqual(self.p.attrs,{'note':'原始'})
        self.assertEqual(self.post('assign_form',{'id':self.ds.id,'version':1,'item':self.item.id}).status_code,409)
    def test_members_cannot_publish_or_assign_forms(self):
        self.assertEqual(self.post('item',{'department':self.d.id,'name':'不准','fields':[]}).status_code,403)
        self.assertEqual(self.post('assign_form',{'id':self.ds.id,'version':1,'item':self.item.id}).status_code,403)
    def test_archived_or_inactive_form_assignment(self):
        self.client.force_login(self.boss);self.item.active=False;self.item.save()
        self.assertEqual(self.post('assign_form',{'id':self.ds.id,'version':1,'item':self.item.id}).status_code,409)
        self.item.active=True;self.item.save();self.ds.archived=True;self.ds.save()
        self.assertEqual(self.post('assign_form',{'id':self.ds.id,'version':1,'item':self.item.id}).status_code,409)
    def test_new_upload_uses_assigned_form(self):
        new=Item.objects.create(department=self.d,name='另一份',version=3,fields=[{'key':'answer','label':'結果','type':'select','required':True,'options':['甲','乙']}])
        self.client.force_login(self.boss);self.post('assign_form',{'id':self.ds.id,'version':1,'item':new.id})
        self.client.force_login(self.member);im=Image.new('RGB',(20,20),'blue');b=io.BytesIO();im.save(b,'JPEG')
        r=self.client.post('/api/upload',{'dataset':self.ds.id,'files':SimpleUploadedFile('new.jpg',b.getvalue())});p=Photo.objects.get(id=r.json()['results'][0]['id'])
        try:
            self.assertEqual(p.form_snapshot,new.fields);self.assertEqual(p.form_version,3)
            body={'photo':p.id,'version':1,'attrs':{'answer':'非法'}};self.assertEqual(self.post('submit',body).status_code,400)
            body['attrs']={'answer':'甲'};self.assertEqual(self.post('submit',body).status_code,200)
        finally:p.file.delete(save=False);p.preview.delete(save=False)
    def test_dataset_name_unique_within_department(self):
        new=Item.objects.create(department=self.d,name='別表單')
        self.assertEqual(self.post('dataset',{'item':new.id,'name':self.ds.name}).status_code,409)
    def test_upload_exif_preview_and_immutable_source(self):
        from PIL.TiffImagePlugin import IFDRational
        exif=Image.Exif();exif[36867]='2026:11:20 09:30:00';exif[36881]='+08:00'
        exif[34853]={1:'N',2:tuple(map(IFDRational,[25,1,30])),3:'E',4:tuple(map(IFDRational,[121,30,0]))}
        im=Image.new('RGB',(30,20),'green');b=io.BytesIO();im.save(b,'JPEG',exif=exif)
        r=self.client.post('/api/upload',{'dataset':self.ds.id,'form_id':self.item.id,'form_version':1,'files':SimpleUploadedFile('gps.jpg',b.getvalue())})
        self.assertEqual(r.status_code,200);p=Photo.objects.get(id=r.json()['results'][0]['id'])
        try:
            self.assertAlmostEqual(p.lat,25.025);self.assertAlmostEqual(p.lon,121.5);self.assertTrue(p.time_confirmed)
            source=p.extracted.copy();self.assertEqual(source['raw_time'],'2026:11:20 09:30:00')
            body={'photo':p.id,'version':1,'captured':'2026-11-21T10:00:00','lat':24,'lon':120,'time_confirmed':True,'attrs':{'note':'單張修改'}}
            self.assertEqual(self.post('save',body).status_code,200);p.refresh_from_db();self.assertEqual(p.extracted,source);self.assertEqual(p.lat,24)
            body['version']=p.version;self.assertEqual(self.post('submit',body).status_code,200)
        finally:p.file.delete(save=False);p.preview.delete(save=False)
    def test_upload_rejects_changed_template_before_receiving(self):
        r=self.client.post('/api/upload',{'dataset':self.ds.id,'form_id':self.item.id,'form_version':999,'files':SimpleUploadedFile('x.jpg',b'x')})
        self.assertEqual(r.status_code,409);self.assertEqual(Photo.objects.count(),1)
    def test_draft_save_allows_incomplete_but_submission_enforces_required(self):
        p=Photo.objects.create(dataset=self.ds,uploader=self.member,name='draft',sha='draft',form_snapshot=self.item.fields)
        body={'photo':p.id,'version':1,'attrs':{}}
        self.assertEqual(self.post('save',body).status_code,200);p.refresh_from_db();body['version']=p.version
        self.assertEqual(self.post('submit',body).status_code,400);p.refresh_from_db();self.assertEqual(p.status,'draft')
        body['attrs']={'note':'批次共同值後逐張修正'};self.assertEqual(self.post('submit',body).status_code,200)
    def test_cannot_edit_another_members_draft(self):
        p=Photo.objects.create(dataset=self.ds,uploader=self.boss,name='private draft',sha='draft',form_snapshot=[])
        self.assertEqual(self.post('save',{'photo':p.id,'version':1,'attrs':{}}).status_code,404)
    def test_nonfinite_coordinates_rejected(self):
        p=Photo.objects.create(dataset=self.ds,uploader=self.member,name='draft',sha='draft')
        self.assertEqual(self.post('save',{'photo':p.id,'version':1,'lat':'NaN','lon':121,'attrs':{}}).status_code,400)

from django.test import SimpleTestCase
from .image_metadata import extract_metadata
class MetadataTests(SimpleTestCase):
    def test_missing_metadata_is_not_upload_time(self):
        captured,confirmed,lat,lon,snapshot=extract_metadata(Image.new('RGB',(10,10)))
        self.assertIsNone(captured);self.assertFalse(confirmed);self.assertIsNone(lat);self.assertEqual(snapshot['time_source'],'missing')
    def test_exif_without_offset_is_inferred(self):
        im=Image.new('RGB',(10,10));im.getexif()[36867]='2026:11:20 12:00:00'
        captured,confirmed,lat,lon,snapshot=extract_metadata(im)
        self.assertIsNotNone(captured);self.assertFalse(confirmed);self.assertTrue(snapshot['timezone_inferred'])

class PermissionWorkflowTests(TestCase):
    setUp=CoreTests.setUp
    post=CoreTests.post
    def test_granular_upload_and_organize_are_independent(self):
        m=Membership.objects.get(user=self.member);m.permissions={'upload':False,'organize':True};m.save()
        r=self.client.post('/api/upload',{'dataset':self.ds.id,'files':SimpleUploadedFile('x.jpg',b'x')});self.assertEqual(r.status_code,403)
        self.assertEqual(self.post('request',{'photo':self.p.id,'kind':'delete','reason':'誤傳'}).status_code,200)
        m.permissions={'upload':True,'organize':False};m.save()
        self.assertEqual(self.post('tags',{'photo':self.p.id,'tags':['test']}).status_code,403)
        self.assertEqual(self.post('request',{'photo':self.p.id,'kind':'correct','reason':'x','payload':{'attrs':{'note':'x'}}}).status_code,403)
    def test_only_admin_can_assign_supervisor_and_permissions(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.post('membership',{'user':self.member.id,'department':self.d.id,'role':'supervisor'}).status_code,403)
        admin=User.objects.create_superuser('admin',password='test');self.client.force_login(admin)
        self.assertEqual(self.post('membership',{'user':self.member.id,'department':self.d.id,'role':'supervisor','permissions':{'upload':False}}).status_code,200)
        self.assertEqual(Membership.objects.get(user=self.member).role,'supervisor')
        event=Audit.objects.filter(action='membership').latest('id');self.assertEqual(event.detail['before']['memberships'][0]['role'],'member');self.assertEqual(event.detail['after']['memberships'][0]['role'],'supervisor')
    def test_revoke_membership_removes_scope_immediately(self):
        admin=User.objects.create_superuser('admin',password='test');c=Client();c.force_login(admin)
        self.assertEqual(c.post('/api/membership',json.dumps({'user':self.member.id,'department':self.d.id,'remove':True}),content_type='application/json').status_code,200)
        self.assertEqual(self.post('search',{}).json()['photos'],[])
        self.assertEqual(self.post('star',{'photo':self.p.id}).status_code,404)
    def test_delegated_form_permission(self):
        m=Membership.objects.get(user=self.member);m.permissions={'forms':True};m.save()
        self.assertEqual(self.post('item',{'department':self.d.id,'name':'授權表單','fields':[]}).status_code,200)
        m.permissions={'forms':False};m.save()
        self.assertEqual(self.post('item',{'department':self.d.id,'name':'拒絕表單','fields':[]}).status_code,403)
    def test_approval_records_before_after_and_events(self):
        self.post('request',{'photo':self.p.id,'kind':'correct','reason':'現場誤植','payload':{'attrs':{'note':'修正值'}}});r=Request.objects.get()
        self.p.refresh_from_db();self.assertEqual(self.p.attrs,{'note':'原始'});self.assertEqual(r.before['attrs'],{'note':'原始'})
        self.client.force_login(self.boss)
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'已核對現場'}).status_code,200)
        r.refresh_from_db();self.p.refresh_from_db();self.assertEqual(self.p.attrs,{'note':'修正值'});self.assertIsNotNone(r.closed_at);self.assertEqual(list(r.events.values_list('action',flat=True)),['submitted','approved'])
        self.assertEqual(r.events.get(action='approved').detail['after']['attrs'],{'note':'修正值'})
        self.assertEqual(Revision.objects.get(photo=self.p).snapshot['attrs'],{'note':'原始'})
    def test_rejection_leaves_photo_unchanged_and_notifies(self):
        self.post('request',{'photo':self.p.id,'kind':'delete','reason':'刪除'});r=Request.objects.get();self.client.force_login(self.boss)
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'reject','reason':''}).status_code,400)
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'reject','reason':'需保留'}).status_code,200)
        self.p.refresh_from_db();self.assertEqual(self.p.status,'submitted');self.assertTrue(Notice.objects.filter(user=self.member,text__contains='rejected').exists())
    def test_other_members_do_not_see_private_requests(self):
        self.post('request',{'photo':self.p.id,'kind':'delete','reason':'敏感理由'});r=Request.objects.get()
        peer=User.objects.create_user('peer',password='test');Membership.objects.create(user=peer,department=self.d,role='member');self.client.force_login(peer)
        self.assertEqual(self.post('requests',{'view':'history'}).json()['requests'],[])
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'x'}).status_code,404)
    def test_cross_department_supervisor_cannot_review(self):
        self.post('request',{'photo':self.p.id,'kind':'delete','reason':'誤傳'});r=Request.objects.get()
        m=Membership.objects.get(user=self.out);m.role='supervisor';m.save();self.client.force_login(self.out)
        self.assertEqual(self.post('requests',{'view':'history'}).json()['requests'],[])
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'x'}).status_code,404)
    def test_stale_and_duplicate_requests(self):
        body={'photo':self.p.id,'kind':'correct','reason':'更正','payload':{'attrs':{'note':'新值'}}}
        self.assertEqual(self.post('request',body).status_code,200);self.assertEqual(self.post('request',body).status_code,409)
        r=Request.objects.get();self.p.version+=1;self.p.save();self.client.force_login(self.boss)
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'approve','reason':'x'}).status_code,409)
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'reject','reason':'請重新核對版本'}).status_code,200)
    def test_requester_keeps_deletion_result_without_photo_access(self):
        self.post('request',{'photo':self.p.id,'kind':'delete','reason':'誤傳'});r=Request.objects.get();self.client.force_login(self.boss);self.post('decision',{'id':r.id,'decision':'approve','reason':'核准'})
        self.client.force_login(self.member);self.assertEqual(self.post('search',{}).json()['photos'],[]);reqs=self.post('requests',{'view':'mine'}).json()['requests'];self.assertEqual(reqs[0]['state'],'approved');self.assertFalse(reqs[0]['can_open']);self.assertEqual(self.client.get(f'/media/{self.p.id}/preview').status_code,404)

    def test_download_permission_can_be_revoked_separately(self):
        m=Membership.objects.get(user=self.member);m.permissions={'download':False};m.save()
        self.assertEqual(self.client.get(f'/media/{self.p.id}/original').status_code,403)
        self.assertEqual(len(self.post('search',{}).json()['photos']),1)
    def test_invalid_permission_and_decision_inputs_are_rejected(self):
        admin=User.objects.create_superuser('admin',password='test');self.client.force_login(admin)
        self.assertEqual(self.post('membership',{'user':self.member.id,'department':self.d.id,'role':'member','permissions':{'organize':'false'}}).status_code,400)
        self.assertEqual(self.post('membership',{'user':self.member.id,'active':'false'}).status_code,400)
        self.client.force_login(self.member);self.post('request',{'photo':self.p.id,'kind':'delete','reason':'test'});r=Request.objects.get();self.client.force_login(self.boss)
        self.assertEqual(self.post('decision',{'id':r.id,'decision':'something','reason':'x'}).status_code,400)
    def test_admin_effective_permissions_are_global(self):
        from .permissions import effective
        admin=User.objects.create_superuser('admin',password='test');m=Membership.objects.create(user=admin,department=self.d,role='viewer')
        self.assertTrue(all(effective(m)[k] for k in ('upload','organize','download','forms','export','analyze')));self.assertFalse(effective(m)['people_view'])

class SearchTests(TestCase):
    setUp=CoreTests.setUp
    post=CoreTests.post
    def search(self,**criteria):return self.post('search',criteria)
    def test_permission_scope_applies_before_counts(self):
        oi=Item.objects.create(department=self.other,name='他科');ds=Dataset.objects.create(item=oi,name='他科資料',creator=self.out)
        Photo.objects.create(dataset=ds,uploader=self.out,name='private',sha='x',status='submitted',lat=1,lon=2)
        r=self.search();self.assertEqual(r.status_code,200);self.assertEqual(r.json()['ids'],[self.p.id]);self.assertEqual(r.json()['total'],1)
        self.assertEqual(self.search(departments=[self.other.id]).json()['total'],0)
    def test_scope_is_union_and_tags_are_or(self):
        second=Dataset.objects.create(item=self.item,name='另一資料集',creator=self.member)
        p2=Photo.objects.create(dataset=second,uploader=self.member,name='two',sha='2',status='submitted',tags=['乙'])
        self.p.tags=['甲'];self.p.save()
        self.assertEqual(set(self.search(tags=['甲','乙']).json()['ids']),{self.p.id,p2.id})
        self.assertEqual(self.search(datasets=[second.id],tags=['甲']).json()['total'],0)
        self.assertEqual(self.search(departments=[self.d.id],datasets=[second.id]).json()['total'],2)
    def test_star_is_personal(self):
        Star.objects.create(user=self.member,photo=self.p)
        self.assertEqual(self.search(star=True).json()['total'],1)
        self.client.force_login(self.boss);self.assertEqual(self.search(star=True).json()['total'],0)
    def test_anomaly_states_and_none(self):
        self.assertEqual(self.search(anomaly='none').json()['total'],1)
        Anomaly.objects.create(photo=self.p,author=self.member,text='測試',state='confirmed')
        Anomaly.objects.create(photo=self.p,author=self.boss,text='重複案件',state='pending')
        self.assertEqual(self.search(anomaly='any').json()['total'],1)
        self.assertEqual(self.search(anomaly='confirmed').json()['total'],1)
        self.assertEqual(self.search(anomaly='pending').json()['total'],1)
        self.assertEqual(self.search(anomaly='dismissed').json()['total'],0)
        self.assertEqual(self.search(anomaly='none').json()['total'],0)
    def test_taiwan_inclusive_end_date_and_missing_time_count(self):
        self.p.captured=__import__('django.utils.dateparse',fromlist=['parse_datetime']).parse_datetime('2026-11-20T15:59:59.999999Z');self.p.save()
        unknown=Photo.objects.create(dataset=self.ds,uploader=self.member,name='unknown',sha='u',status='submitted')
        nextday=Photo.objects.create(dataset=self.ds,uploader=self.member,name='next',sha='n',status='submitted',captured='2026-11-20T16:00:00Z')
        result=self.search(**{'from':'2026-11-20','to':'2026-11-20'}).json()
        self.assertEqual(result['ids'],[self.p.id]);self.assertEqual(result['excluded_missing_time'],1)
        self.assertEqual(self.search().json()['total'],3)
    def test_custom_fields_types_and_combination(self):
        schema=[{'key':'note','label':'說明','type':'text'},{'key':'n','label':'數量','type':'integer'},{'key':'done','label':'完成','type':'boolean'},{'key':'day','label':'巡查日','type':'date'},{'key':'choices','label':'類型','type':'multi','options':['甲','乙']}]
        self.p.form_snapshot=schema;self.p.attrs={'note':'道路積水','n':0,'done':False,'day':'2026-11-20','choices':['甲']};self.p.save()
        rules=[{'key':'note','type':'text','op':'contains','value':'積水'},{'key':'n','type':'integer','op':'range','min':0,'max':0},{'key':'done','type':'boolean','op':'equals','value':False},{'key':'day','type':'date','op':'range','min':'2026-11-20','max':'2026-11-20'},{'key':'choices','type':'multi','op':'in','values':['乙','甲']}]
        self.assertEqual(self.search(datasets=[self.ds.id],fields=rules).json()['ids'],[self.p.id])
        rules[1]['min']=1;rules[1]['max']=10;self.assertEqual(self.search(datasets=[self.ds.id],fields=rules).json()['total'],0)
    def test_missing_field_does_not_treat_false_or_zero_as_empty(self):
        self.p.form_snapshot=[{'key':'done','type':'boolean','label':'完成'},{'key':'n','type':'integer','label':'數量'}];self.p.attrs={'done':False,'n':0};self.p.save()
        for key,kind in [('done','boolean'),('n','integer')]:self.assertEqual(self.search(datasets=[self.ds.id],fields=[{'key':key,'type':kind,'op':'missing'}]).json()['total'],0)
    def test_empty_dataset_current_fields_are_valid(self):
        ds=Dataset.objects.create(item=self.item,name='空集合',creator=self.member)
        r=self.search(datasets=[ds.id],fields=[{'key':'note','type':'text','op':'contains','value':'x'}]);self.assertEqual(r.status_code,200);self.assertEqual(r.json()['total'],0)
    def test_custom_fields_require_single_dataset_and_valid_schema(self):
        rule={'key':'note','type':'text','op':'contains','value':'原始'}
        self.assertEqual(self.search(fields=[rule]).status_code,422)
        rule['key']='hidden';self.assertEqual(self.search(datasets=[self.ds.id],fields=[rule]).status_code,422)
    def test_invalid_ranges_and_no_duplicate_results(self):
        self.assertEqual(self.search(**{'from':'2026-11-21','to':'2026-11-20'}).status_code,422)
        self.assertEqual(self.search(**{'from':'not-a-date'}).status_code,422)
        self.assertEqual(self.search(anomaly='bad').status_code,422)
    def test_missing_time_count_uses_other_filters(self):
        self.p.tags=['保留'];self.p.save();Photo.objects.create(dataset=self.ds,uploader=self.member,name='other',sha='o',status='submitted',tags=['排除'])
        r=self.search(tags=['保留'],**{'from':'2026-11-20'}).json();self.assertEqual(r['total'],0);self.assertEqual(r['excluded_missing_time'],1)
    def test_removed_membership_cannot_search_old_results(self):
        Membership.objects.filter(user=self.member).delete();self.assertEqual(self.search().json()['total'],0)

    def test_search_returns_latest_records_with_matching_ids(self):
        self.assertEqual(self.search().json()['photos'][0]['attrs']['note'],'原始')
        self.p.attrs={'note':'其他人剛更新'};self.p.save()
        result=self.search().json();self.assertEqual(result['total'],len(result['photos']));self.assertEqual([p['id'] for p in result['photos']],result['ids']);self.assertEqual(result['photos'][0]['attrs']['note'],'其他人剛更新')

class ComparisonTests(TestCase):
    setUp=CoreTests.setUp
    post=CoreTests.post
    def nearby(self,**data):return self.post('nearby',{'photo':self.p.id,**data})
    def point(self,name,meters,captured='2026-11-20T09:00:00+08:00',dataset=None):
        import math
        return Photo.objects.create(dataset=dataset or self.ds,uploader=self.member,name=name,sha=name,status='submitted',lat=self.p.lat+math.degrees(meters/6371008.8),lon=self.p.lon,captured=captured,attrs={'note':name},form_snapshot=self.item.fields)
    def setUp(self):
        CoreTests.setUp(self);self.p.lat=25;self.p.lon=121;self.p.captured='2026-11-21T09:00:00+08:00';self.p.save()
    def test_radius_boundaries_and_base_excluded(self):
        a=self.point('100m',100);b=self.point('101m',101);c=self.point('1000m',1000)
        r=self.nearby(radius=100);self.assertEqual(r.status_code,200);self.assertEqual([p['id'] for p in r.json()['candidates']],[a.id])
        self.assertEqual(set(p['id'] for p in self.nearby(radius=1000).json()['candidates']),{a.id,b.id,c.id})
        self.assertEqual(self.nearby(radius=99).status_code,422);self.assertEqual(self.nearby(radius=1001).status_code,422)
    def test_time_window_updates_rows_but_keeps_full_domain(self):
        a=self.point('earlier',30,'2026-11-19T09:00:00+08:00');b=self.point('later',40,'2026-11-21T09:00:00+08:00')
        full=self.nearby(radius=100).json();limited=self.nearby(radius=100,**{'from':'2026-11-21T09:00:00+08:00','to':'2026-11-21T09:00:00+08:00'}).json()
        self.assertEqual([p['id'] for p in limited['candidates']],[b.id]);self.assertEqual(full['earliest'],limited['earliest']);self.assertEqual(full['latest'],limited['latest'])
    def test_cross_dataset_and_optional_current_dataset(self):
        second=Dataset.objects.create(item=self.item,name='歷史',creator=self.member);a=self.point('other dataset',20,dataset=second)
        self.assertEqual(self.nearby(radius=100).json()['candidates'][0]['id'],a.id)
        self.assertEqual(self.nearby(radius=100,only_dataset=True).json()['candidates'],[])
    def test_hidden_and_recycled_candidates_not_returned(self):
        a=self.point('visible',30);oi=Item.objects.create(department=self.other,name='他科');ods=Dataset.objects.create(item=oi,name='他科',creator=self.out);self.point('hidden',10,dataset=ods)
        recycled=self.point('recycled',5);recycled.status='recycled';recycled.save()
        self.assertEqual([p['id'] for p in self.nearby().json()['candidates']],[a.id])
    def test_candidate_metadata_and_missing_time(self):
        a=self.point('with metadata',25);self.point('without time',20,captured=None)
        r=self.nearby().json();self.assertEqual(r['excluded_missing_time'],1);self.assertEqual(r['candidates'][0]['attrs'],{'note':'with metadata'});self.assertEqual(r['candidates'][0]['fields'],self.item.fields);self.assertAlmostEqual(r['candidates'][0]['distance_m'],25,places=2)
    def test_scope_tags_and_bad_time(self):
        a=self.point('tagged',10);a.tags=['巡查'];a.save();self.point('not tagged',20)
        self.assertEqual([p['id'] for p in self.nearby(tags=['巡查']).json()['candidates']],[a.id])
        self.assertEqual(self.nearby(**{'from':'2026-11-20T12:00:00'}).status_code,422)
        self.assertEqual(self.nearby(**{'from':'2026-11-22T00:00:00Z','to':'2026-11-20T00:00:00Z'}).status_code,422)
    def test_missing_base_location_and_revocation(self):
        self.p.lat=None;self.p.save();self.assertEqual(self.nearby().status_code,422)
        Membership.objects.filter(user=self.member).delete();self.assertEqual(self.nearby().status_code,404)


class BatchOperationsTest(TestCase):
    def setUp(self):
        self.dep = Department.objects.create(name='水利科')
        self.other = Department.objects.create(name='環保科')
        self.member = User.objects.create_user('m1', password='p')
        self.viewer = User.objects.create_user('v1', password='p')
        Membership.objects.create(user=self.member, department=self.dep, role='member')
        Membership.objects.create(user=self.viewer, department=self.dep, role='viewer')
        self.item = Item.objects.create(
            department=self.dep,
            name='堤防巡檢',
            fields=[{'key': 'note', 'label': '備註', 'type': 'text', 'required': True}],
        )
        self.ds = Dataset.objects.create(item=self.item, name='第一季', creator=self.member)
        self.draft1 = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='d1.jpg', sha='a1',
            status='draft', form_snapshot=self.item.fields,
        )
        self.draft2 = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='d2.jpg', sha='a2',
            status='draft', form_snapshot=self.item.fields,
        )
        self.submitted = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='s1.jpg', sha='s1',
            status='submitted', form_snapshot=self.item.fields, attrs={'note': 'ok'},
        )
        self.client.force_login(self.member)

    def post(self, path, payload):
        return self.client.post(f'/api/{path}', data=json.dumps(payload), content_type='application/json')

    def test_batch_star_and_tags(self):
        r1 = self.post('batch_star', {'photos': [self.submitted.id]})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()['count'], 1)
        # Re-starring is idempotent
        r2 = self.post('batch_star', {'photos': [self.submitted.id]})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()['count'], 0)

        t1 = self.post('batch_tags', {'photos': [self.submitted.id], 'tags': ['護岸', '巡查']})
        self.assertEqual(t1.status_code, 200)
        self.submitted.refresh_from_db()
        self.assertEqual(self.submitted.tags, ['巡查', '護岸'])

        # Viewer cannot batch tag
        self.client.force_login(self.viewer)
        t2 = self.post('batch_tags', {'photos': [self.submitted.id], 'tags': ['測試']})
        self.assertEqual(t2.status_code, 403)

    def test_batch_save_and_submit_partial_results(self):
        res = self.post('batch_save', {
            'items': [
                {'photo': self.draft1.id, 'version': self.draft1.version, 'attrs': {'note': '已完成'}},
                {'photo': self.draft2.id, 'version': self.draft2.version, 'attrs': {}},
            ]
        })
        self.assertEqual(res.status_code, 200)
        self.draft1.refresh_from_db()
        self.assertEqual(self.draft1.attrs['note'], '已完成')

        sub = self.post('batch_submit', {
            'items': [
                {'photo': self.draft1.id, 'version': self.draft1.version, 'attrs': {'note': '已完成'}},
                {'photo': self.draft2.id, 'version': self.draft2.version, 'attrs': {}},
            ]
        })
        self.assertEqual(sub.status_code, 200)
        results = {r['id']: r for r in sub.json()['results']}
        self.assertTrue(results[self.draft1.id]['ok'])
        self.assertIn('error', results[self.draft2.id])
        self.draft1.refresh_from_db()
        self.draft2.refresh_from_db()
        self.assertEqual(self.draft1.status, 'submitted')
        self.assertEqual(self.draft2.status, 'draft')


class BurstStackingTest(TestCase):
    def setUp(self):
        self.dep = Department.objects.create(name='保育科')
        self.member = User.objects.create_user('m1', password='p')
        self.viewer = User.objects.create_user('v1', password='p')
        Membership.objects.create(user=self.member, department=self.dep, role='member')
        Membership.objects.create(user=self.viewer, department=self.dep, role='viewer')
        self.item = Item.objects.create(department=self.dep, name='活動紀錄', fields=[])
        self.ds = Dataset.objects.create(item=self.item, name='記者會', creator=self.member)
        self.client.force_login(self.member)

    def _upload_and_submit(self, name, color_shift, captured_iso):
        from PIL import ImageDraw
        im = Image.new('RGB', (120, 90), (100, 140, 160))
        draw = ImageDraw.Draw(im)
        draw.rectangle((10 + (color_shift % 20), 10, 90, 70), fill=(200 + color_shift, 150, 80))
        buf = io.BytesIO()
        im.save(buf, 'JPEG')
        buf.seek(0)
        buf.name = name
        up = self.client.post('/api/upload', {'dataset': self.ds.id, 'files': buf})
        self.assertEqual(up.status_code, 200)
        res0 = up.json()['results'][0]
        self.assertIn('id', res0, f'Upload failed for {name}: {res0}')
        pid = res0['id']
        p = Photo.objects.get(id=pid)
        sub = self.client.post(
            '/api/submit',
            data=json.dumps({'photo': pid, 'version': p.version, 'captured': captured_iso, 'time_confirmed': True, 'attrs': {}}),
            content_type='application/json',
        )
        self.assertEqual(sub.status_code, 200)
        return Photo.objects.get(id=pid)

    def test_burst_auto_grouping_and_cover_selection(self):
        p1 = self._upload_and_submit('b1.jpg', 0, '2026-11-24T10:00:00+08:00')
        p2 = self._upload_and_submit('b2.jpg', 5, '2026-11-24T10:00:03+08:00')
        p_far = self._upload_and_submit('far.jpg', 10, '2026-11-24T10:01:00+08:00')

        p1.refresh_from_db()
        p2.refresh_from_db()
        p_far.refresh_from_db()
        self.assertTrue(p1.burst_group)
        self.assertEqual(p1.burst_group, p2.burst_group)
        self.assertEqual(p_far.burst_group, '')

        # Set p2 as burst cover
        r = self.client.post(
            '/api/burst_cover',
            data=json.dumps({'photo': p2.id}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200)
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertFalse(p1.burst_cover)
        self.assertTrue(p2.burst_cover)

        # Viewer cannot change cover
        self.client.force_login(self.viewer)
        r_denied = self.client.post(
            '/api/burst_cover',
            data=json.dumps({'photo': p1.id}),
            content_type='application/json',
        )
        self.assertEqual(r_denied.status_code, 403)

    def test_burst_span_limit_and_recompute_consistency(self):
        from core.models import Config
        cfg, _ = Config.objects.get_or_create(id=1)
        cfg.burst_gap_seconds = 5
        cfg.burst_span_seconds = 10
        cfg.burst_dhash_max = 14
        cfg.save()

        # Neighboring gaps are 4s (<=5s), but t=12s exceeds span 10s from t=0s
        p0 = self._upload_and_submit('s0.jpg', 0, '2026-11-24T11:00:00+08:00')
        p1 = self._upload_and_submit('s1.jpg', 2, '2026-11-24T11:00:04+08:00')
        p2 = self._upload_and_submit('s2.jpg', 4, '2026-11-24T11:00:08+08:00')
        p3 = self._upload_and_submit('s3.jpg', 6, '2026-11-24T11:00:12+08:00')
        for p in (p0, p1, p2, p3):
            p.refresh_from_db()
        self.assertTrue(p0.burst_group)
        self.assertEqual(p0.burst_group, p1.burst_group)
        self.assertEqual(p0.burst_group, p2.burst_group)
        self.assertEqual(p3.burst_group, '')

        # Adding another photo at t=15s forms a second group [p3, p4] with p3
        p4 = self._upload_and_submit('s4.jpg', 8, '2026-11-24T11:00:15+08:00')
        p3.refresh_from_db()
        p4.refresh_from_db()
        self.assertTrue(p3.burst_group)
        self.assertEqual(p3.burst_group, p4.burst_group)
        self.assertNotEqual(p3.burst_group, p0.burst_group)

        # Admin recompute produces identical deterministic grouping
        admin = User.objects.create_superuser('adm_b', password='p')
        self.client.force_login(admin)
        rec = self.client.post(
            '/api/burst_recompute',
            data=json.dumps({'dataset': self.ds.id}),
            content_type='application/json',
        )
        self.assertEqual(rec.status_code, 200)
        p0_after = Photo.objects.get(id=p0.id)
        p3_after = Photo.objects.get(id=p3.id)
        self.assertEqual(p0_after.burst_group, p0.burst_group)
        self.assertEqual(p3_after.burst_group, p3.burst_group)

    def test_burst_lifecycle_on_correct_move_recycle_restore_and_unique_cover(self):
        from django.db import IntegrityError, transaction
        sup = User.objects.create_user('sup_b', password='p')
        Membership.objects.create(user=sup, department=self.dep, role='supervisor')
        ds2 = Dataset.objects.create(item=self.item, name='第二活動', creator=self.member)

        p1 = self._upload_and_submit('l1.jpg', 0, '2026-11-24T12:00:00+08:00')
        p2 = self._upload_and_submit('l2.jpg', 3, '2026-11-24T12:00:02+08:00')
        p3 = self._upload_and_submit('l3.jpg', 6, '2026-11-24T12:00:04+08:00')

        # Designate p2 as manual cover
        self.client.post('/api/burst_cover', data=json.dumps({'photo': p2.id}), content_type='application/json')
        p2.refresh_from_db()
        self.assertTrue(p2.burst_cover)

        # DB-level constraint prevents two active covers in the same group
        with self.assertRaises(IntegrityError), transaction.atomic():
            Photo.objects.filter(id=p1.id).update(burst_cover=True)

        # 1) Correct p2's time outside the window -> p2 exits group, p1 becomes fallback cover
        req_c = self.client.post(
            '/api/request',
            data=json.dumps({
                'photo': p2.id, 'version': p2.version, 'kind': 'correct', 'reason': '時間誤植',
                'payload': {'captured': '2026-11-24T13:00:00+08:00', 'time_confirmed': True, 'attrs': {}},
            }),
            content_type='application/json',
        )
        self.assertEqual(req_c.status_code, 200)
        self.client.force_login(sup)
        r_id = Request.objects.order_by('-id').first().id
        dec_c = self.client.post(
            '/api/decision',
            data=json.dumps({'id': r_id, 'decision': 'approve', 'reason': 'ok'}),
            content_type='application/json',
        )
        self.assertEqual(dec_c.status_code, 200)
        p1.refresh_from_db(); p2.refresh_from_db(); p3.refresh_from_db()
        self.assertEqual(p2.burst_group, '')
        self.assertFalse(p2.burst_cover)
        self.assertTrue(p1.burst_group)
        self.assertEqual(p1.burst_group, p3.burst_group)
        self.assertTrue(p1.burst_cover)

        # 2) Move p3 to ds2 -> p3 leaves old group and p1 becomes ungrouped
        self.client.force_login(self.member)
        self.client.post(
            '/api/request',
            data=json.dumps({'photo': p3.id, 'version': p3.version, 'kind': 'move', 'reason': '搬移', 'payload': {'dataset': ds2.id}}),
            content_type='application/json',
        )
        self.client.force_login(sup)
        r_move = Request.objects.order_by('-id').first().id
        self.client.post('/api/decision', data=json.dumps({'id': r_move, 'decision': 'approve', 'reason': 'ok'}), content_type='application/json')
        p1.refresh_from_db(); p3.refresh_from_db()
        self.assertEqual(p1.burst_group, '')
        self.assertEqual(p3.burst_group, '')

        # 3) Search partial match count vs total group count
        self.client.force_login(self.member)
        p_a = self._upload_and_submit('m1.jpg', 10, '2026-11-24T14:00:00+08:00')
        p_b = self._upload_and_submit('m2.jpg', 12, '2026-11-24T14:00:02+08:00')
        p_a.refresh_from_db()
        self.assertTrue(p_a.burst_group)
        self.client.post('/api/tags', data=json.dumps({'photo': p_a.id, 'tags': ['精選篩選']}), content_type='application/json')
        s = self.client.post('/api/search', data=json.dumps({'tags': ['精選篩選']}), content_type='application/json').json()
        self.assertEqual(s['burst_matched_counts'][p_a.burst_group], 1)
        self.assertEqual(s['photos'][0]['burst_total_count'], 2)

        # 4) Recycle p_a -> remaining p_b ungroups; Restore p_a -> regroups with p_b
        self.client.post('/api/request', data=json.dumps({'photo': p_a.id, 'version': p_a.version, 'kind': 'delete', 'reason': 'del', 'payload': {}}), content_type='application/json')
        self.client.force_login(sup)
        r_del = Request.objects.order_by('-id').first().id
        self.client.post('/api/decision', data=json.dumps({'id': r_del, 'decision': 'approve', 'reason': 'del'}), content_type='application/json')
        p_a.refresh_from_db(); p_b.refresh_from_db()
        self.assertEqual(p_a.status, 'recycled')
        self.assertEqual(p_a.burst_group, '')
        self.assertEqual(p_b.burst_group, '')

        self.client.post('/api/restore', data=json.dumps({'photo': p_a.id}), content_type='application/json')
        p_a.refresh_from_db(); p_b.refresh_from_db()
        self.assertEqual(p_a.status, 'submitted')
        self.assertTrue(p_a.burst_group)
        self.assertEqual(p_a.burst_group, p_b.burst_group)

    def test_burst_merge_conflict_resolves_to_latest_manual_cover_and_audits(self):
        from core.models import Audit, Config
        cfg, _ = Config.objects.get_or_create(id=1)
        cfg.burst_gap_seconds = 5
        cfg.burst_span_seconds = 20
        cfg.burst_dhash_max = 14
        cfg.save()

        # Two separate groups: [t=0, t=2] and [t=10, t=12] (gap 8s > 5s)
        g1a = self._upload_and_submit('g1a.jpg', 0, '2026-11-24T15:00:00+08:00')
        g1b = self._upload_and_submit('g1b.jpg', 2, '2026-11-24T15:00:02+08:00')
        g2a = self._upload_and_submit('g2a.jpg', 4, '2026-11-24T15:00:10+08:00')
        g2b = self._upload_and_submit('g2b.jpg', 6, '2026-11-24T15:00:12+08:00')

        r_c1 = self.client.post('/api/burst_cover', data=json.dumps({'photo': g1b.id}), content_type='application/json')
        self.assertEqual(r_c1.status_code, 200, r_c1.json())
        r_c2 = self.client.post('/api/burst_cover', data=json.dumps({'photo': g2b.id}), content_type='application/json')
        self.assertEqual(r_c2.status_code, 200, r_c2.json())

        # Bridge photos at t=5s and t=7s merge all into one group within span 12s <= 20s
        self._upload_and_submit('br1.jpg', 3, '2026-11-24T15:00:05+08:00')
        self._upload_and_submit('br2.jpg', 5, '2026-11-24T15:00:07+08:00')

        for p in (g1a, g1b, g2a, g2b):
            p.refresh_from_db()
        self.assertEqual(len({g1a.burst_group, g1b.burst_group, g2a.burst_group, g2b.burst_group}), 1)
        covers = list(Photo.objects.filter(dataset=self.ds, burst_group=g1a.burst_group, burst_cover=True))
        self.assertIn(covers[0].id, [g1b.id, g2b.id], [(p.id, p.burst_cover, str(p.burst_cover_at)) for p in (g1a, g1b, g2a, g2b)])
        self.assertEqual([c.id for c in covers], [g2b.id])
        self.assertTrue(Audit.objects.filter(action='burst_cover_conflict_resolved').exists())


class ReverseGeocodingTest(TestCase):
    def setUp(self):
        self.dep = Department.objects.create(name='水質科')
        self.member = User.objects.create_user('m1', password='p')
        Membership.objects.create(user=self.member, department=self.dep, role='member')
        self.item = Item.objects.create(department=self.dep, name='河川採樣', fields=[])
        self.ds = Dataset.objects.create(item=self.item, name='淡水河', creator=self.member)
        self.client.force_login(self.member)

    def test_offline_reverse_geocode_and_search_stats(self):
        from core.geocoding import reverse_geocode

        county, town, village = reverse_geocode(25.033, 121.565)
        self.assertEqual((county, town), ('臺北市', '信義區'))
        self.assertTrue(village)
        self.assertEqual(reverse_geocode(None, None), ('', '', ''))

        p1 = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='a.jpg', sha='r1',
            status='draft', lat=25.033, lon=121.565, form_snapshot=[],
        )
        res = self.client.post(
            '/api/submit',
            data=json.dumps({'photo': p1.id, 'version': p1.version, 'lat': 25.033, 'lon': 121.565, 'attrs': {}}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        p1.refresh_from_db()
        self.assertEqual(p1.admin_county, '臺北市')
        self.assertEqual(p1.admin_town, '信義區')
        self.assertTrue(p1.admin_village)

        p2 = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='b.jpg', sha='r2',
            status='submitted', lat=None, lon=None, form_snapshot=[],
        )

        s1 = self.client.post(
            '/api/search',
            data=json.dumps({'county': '臺北市', 'town': '信義區'}),
            content_type='application/json',
        )
        self.assertEqual(s1.status_code, 200)
        self.assertEqual(s1.json()['ids'], [p1.id])
        self.assertEqual(s1.json()['region_stats'], [{'county': '臺北市', 'town': '信義區', 'count': 1}])

        s2 = self.client.post(
            '/api/search',
            data=json.dumps({'county': '__missing__'}),
            content_type='application/json',
        )
        self.assertEqual(s2.json()['ids'], [p2.id])


class OcrSearchTest(TestCase):
    def setUp(self):
        self.dep = Department.objects.create(name='建管科')
        self.other = Department.objects.create(name='政風室')
        self.member = User.objects.create_user('m1', password='p')
        self.out = User.objects.create_user('o1', password='p')
        Membership.objects.create(user=self.member, department=self.dep, role='member')
        Membership.objects.create(user=self.out, department=self.other, role='member')
        self.item = Item.objects.create(department=self.dep, name='工程告示', fields=[])
        self.ds = Dataset.objects.create(item=self.item, name='115年堤防', creator=self.member)
        self.o_item = Item.objects.create(department=self.other, name='稽查', fields=[])
        self.o_ds = Dataset.objects.create(item=self.o_item, name='機密', creator=self.out)
        self.client.force_login(self.member)

    def test_ocr_search_scoped_and_multiterm(self):
        from core.models import PhotoOcr
        from core.ocr_engine import MODEL_KEY as OCR_MODEL_KEY

        p1 = Photo.objects.create(
            dataset=self.ds, uploader=self.member, name='sign1.jpg', sha='o1',
            status='submitted', form_snapshot=[],
        )
        p2 = Photo.objects.create(
            dataset=self.o_ds, uploader=self.out, name='sign2.jpg', sha='o2',
            status='submitted', form_snapshot=[],
        )
        PhotoOcr.objects.create(
            photo=p1, model_key=OCR_MODEL_KEY, source_sha='o1',
            text='115年度 淡水河堤防改善工程\n財產編號 EQ-2026-099', state='ready',
        )
        PhotoOcr.objects.create(
            photo=p2, model_key=OCR_MODEL_KEY, source_sha='o2',
            text='115年度 淡水河堤防改善工程\n財產編號 EQ-2026-099', state='ready',
        )

        # Case-insensitive English + Traditional Chinese multi-term search
        r = self.client.post(
            '/api/semantic/ocr_search',
            data=json.dumps({'query': '淡水河 eq-2026-099'}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['matches'], 1)
        self.assertEqual([p['id'] for p in body['photos']], [p1.id])
        self.assertIn('EQ-2026-099', body['photos'][0]['ocr_snippet'])

        # Validation error on empty query
        bad = self.client.post(
            '/api/semantic/ocr_search',
            data=json.dumps({'query': '   '}),
            content_type='application/json',
        )
        self.assertEqual(bad.status_code, 422)
