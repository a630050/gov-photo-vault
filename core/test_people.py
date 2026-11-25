import io,tempfile
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from . import tests as core_tests
from .models import *
from .permissions import allowed,capabilities_data
from .face_engine import MODEL_KEY

def vector(n=0):return [int(i==n) for i in range(128)]
def face(n=0):return {'vector':vector(n),'bbox':[.1,.2,.3,.4],'quality':{'usable':True,'blur':100,'confidence':.99}}

class PeopleTests(TestCase):
    post=core_tests.CoreTests.post
    def setUp(self):
        core_tests.CoreTests.setUp(self)
        self.p.file='originals/test.jpg';self.p.save(update_fields=['file'])
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.override=override_settings(MEDIA_ROOT=self.tmp.name);self.override.enable();self.addCleanup(self.override.disable)
        m=Membership.objects.get(user=self.member);m.permissions={'people_view':True,'people_manage':True,'face_search':True};m.save()
        self.person=Person.objects.create(department=self.d,code='A001',name='同名人物',creator=self.member)
        self.reference=PersonReference.objects.create(person=self.person,creator=self.member,sha='r',vector=vector(),model_key=MODEL_KEY,bbox=[0,0,1,1],quality={'usable':True})
        self.observation=FaceObservation.objects.create(photo=self.p,source_sha=self.p.sha,model_key=MODEL_KEY,ordinal=0,vector=vector(),bbox=[.1,.1,.5,.5],quality={'usable':True})
        FacePhotoIndex.objects.create(photo=self.p,source_sha=self.p.sha,model_key=MODEL_KEY,state='ready')
    def search(self,**kw):return self.post('people/search',{'person':str(self.person.id),**kw})
    def candidate(self):
        r=self.search();self.assertEqual(r.status_code,200,r.content);return r.json()['candidates'][0]['token']
    def confirm(self,token):return self.post('people/confirm',{'token':token,'reason':'人工核對參考照片與候選人臉'})
    def uploaded(self,name='ref.jpg'):
        out=io.BytesIO();Image.new('RGB',(100,100),'white').save(out,'JPEG');return SimpleUploadedFile(name,out.getvalue(),content_type='image/jpeg')
    def test_special_permissions_not_implicit_even_for_admin(self):
        m=Membership.objects.get(user=self.member);m.permissions={};m.save()
        self.assertEqual(self.post('people/list',{'department':self.d.id}).status_code,403)
        admin=User.objects.create_superuser('admin',password='test');Membership.objects.create(user=admin,department=self.d,role='supervisor')
        self.assertFalse(allowed(admin,self.d.id,'people_view'));self.assertFalse(capabilities_data(admin)[str(self.d.id)]['face_search'])
        self.client.force_login(admin);self.assertEqual(self.search().status_code,403)
    def test_roster_same_name_and_department_code(self):
        r=self.post('people/save',{'department':self.d.id,'code':'A002','name':'同名人物'});self.assertEqual(r.status_code,200);self.assertNotEqual(r.json()['person']['id'],str(self.person.id))
        self.assertEqual(self.post('people/save',{'department':self.d.id,'code':'A001','name':'其他'}).status_code,409)
        self.assertEqual(self.post('people/list',{'department':self.other.id}).status_code,403)
    def test_search_requires_view_and_search_not_organize(self):
        m=Membership.objects.get(user=self.member);m.permissions={'people_view':True};m.save();self.assertEqual(self.search().status_code,403)
        m.permissions={'people_view':True,'face_search':True,'organize':False};m.save();token=self.candidate();self.assertEqual(self.confirm(token).status_code,403)
        m.permissions={'face_search':True,'people_manage':True};m.save();self.assertEqual(self.search().status_code,403)
    def test_manager_without_search_can_manage_but_not_search(self):
        m=Membership.objects.get(user=self.member);m.permissions={'people_view':True,'people_manage':True};m.save()
        self.assertEqual(self.post('people/save',{'department':self.d.id,'name':'第二人','code':'A002'}).status_code,200);self.assertEqual(self.search().status_code,403)
    def test_photo_scope_and_no_automatic_link(self):
        i=Item.objects.create(department=self.other,name='他科');ds=Dataset.objects.create(item=i,name='他科',creator=self.out)
        for dataset,status in [(ds,'submitted'),(self.ds,'draft'),(self.ds,'recycled')]:
            p=Photo.objects.create(dataset=dataset,uploader=self.member,name='private',sha='private',status=status)
            FaceObservation.objects.create(photo=p,source_sha=p.sha,model_key=MODEL_KEY,ordinal=0,vector=vector(),bbox=[0,0,1,1])
        r=self.search().json();self.assertEqual([x['photo']['id'] for x in r['candidates']],[self.p.id]);self.assertFalse(PersonPhotoLink.objects.exists())
        self.assertEqual(self.search(datasets=[ds.id]).json()['candidates'],[])
    def test_confirmation_is_manual_idempotent_and_historical(self):
        token=self.candidate();self.assertEqual(self.post('people/confirm',{'token':token,'reason':''}).status_code,422)
        self.assertEqual(self.confirm(token).status_code,200);self.assertEqual(self.confirm(token).status_code,200);self.assertEqual(PersonPhotoLink.objects.count(),1);self.assertEqual(PersonLinkEvent.objects.count(),1)
        link=PersonPhotoLink.objects.get();r=self.post('people/unlink',{'person':str(self.person.id),'link':link.id,'version':link.version,'reason':'人工發現關聯錯誤'});self.assertEqual(r.status_code,200)
        self.assertEqual(PersonLinkEvent.objects.count(),2);link.refresh_from_db();self.assertFalse(link.active)
        self.assertEqual(self.confirm(token).status_code,200);self.assertEqual(PersonLinkEvent.objects.count(),3)
    def test_stale_reference_photo_and_revocation_reject_candidates(self):
        token=self.candidate();self.person.version+=1;self.person.save();self.assertEqual(self.confirm(token).status_code,409)
        token=self.candidate();self.p.version+=1;self.p.save();self.assertEqual(self.confirm(token).status_code,409)
        token=self.candidate();Membership.objects.filter(user=self.member).delete();self.assertEqual(self.confirm(token).status_code,403)
        self.assertEqual(self.confirm('forged').status_code,409)
    def test_photo_serialization_does_not_expose_names_without_grant(self):
        self.confirm(self.candidate());r=self.post('search',{}).json();self.assertEqual(r['photos'][0]['people'][0]['name'],self.person.name)
        m=Membership.objects.get(user=self.member);m.permissions={};m.save();self.assertEqual(self.post('search',{}).json()['photos'][0]['people'],[])
    def test_recycled_moved_or_changed_photos_hidden(self):
        token=self.candidate();self.p.status='recycled';self.p.save();self.assertEqual(self.search().json()['candidates'],[]);self.assertEqual(self.confirm(token).status_code,409)
        self.assertEqual(self.post('people/links',{'person':str(self.person.id)}).json()['links'],[])
    def test_multiple_reference_upload_and_rejection(self):
        with patch('core.people.extract',return_value=[{'id':'reference','faces':[face()]}]):
            r=self.client.post('/api/people/reference-upload',{'person':str(self.person.id),'files':[self.uploaded()]})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json()['results'][0]['ok']);self.assertEqual(PersonReference.objects.filter(person=self.person,active=True).count(),2)
        with patch('core.people.extract',return_value=[{'id':'reference','faces':[face(),face()]}]):
            r=self.client.post('/api/people/reference-upload',{'person':str(self.person.id),'files':[self.uploaded('multi.jpg')]})
        self.assertIn('error',r.json()['results'][0]);self.assertEqual(PersonReference.objects.count(),2)
        self.assertNotIn('vector',str(self.post('people/list',{'department':self.d.id}).json()))
    def test_reference_asset_has_independent_access(self):
        # Reference IDs alone never authorize the underlying biometric image.
        m=Membership.objects.get(user=self.member);m.permissions={'people_view':True};m.save()
        self.assertEqual(self.client.get('/people/reference/'+str(self.reference.id)).status_code,403)
        self.client.force_login(self.out);self.assertEqual(self.client.get('/people/reference/'+str(self.reference.id)).status_code,403)
    def test_index_keeps_multiple_faces_and_scopes_before_inference(self):
        FaceObservation.objects.all().delete();FacePhotoIndex.objects.all().delete()
        with patch('core.people.extract',return_value=[{'id':str(self.p.id),'faces':[face(),face(1)]}]) as extract:
            r=self.post('people/index',{'person':str(self.person.id),'datasets':[self.ds.id]})
        self.assertEqual(r.status_code,200,r.content);self.assertEqual(FaceObservation.objects.count(),2);self.assertEqual(len(extract.call_args[0][0]),1)
        self.assertEqual(self.search().json()['matches'],1);self.assertFalse(PersonPhotoLink.objects.exists())
    def test_index_rechecks_revocation_after_inference(self):
        FaceObservation.objects.all().delete();FacePhotoIndex.objects.all().delete()
        def result(rows):Membership.objects.filter(user=self.member).delete();return [{'id':str(self.p.id),'faces':[face()]}]
        with patch('core.people.extract',side_effect=result):r=self.post('people/index',{'person':str(self.person.id)})
        self.assertEqual(r.status_code,403);self.assertFalse(FaceObservation.objects.exists())
    def test_deactivation_and_archive_block_new_links(self):
        token=self.candidate();self.ds.archived=True;self.ds.save();self.assertEqual(self.confirm(token).status_code,409)
        self.person.active=False;self.person.save();self.assertEqual(self.search().status_code,409)
    def test_conflicting_person_on_same_face_requires_removal(self):
        self.confirm(self.candidate());other=Person.objects.create(department=self.d,code='B',name='另一人',creator=self.member)
        PersonReference.objects.create(person=other,creator=self.member,sha='other',vector=vector(),model_key=MODEL_KEY)
        r=self.post('people/search',{'person':str(other.id)});token=r.json()['candidates'][0]['token'];self.assertEqual(self.confirm(token).status_code,409)
    def test_generic_audit_does_not_leak_person_links(self):
        self.confirm(self.candidate())
        admin=User.objects.create_superuser('audit-admin',password='test');self.client.force_login(admin)
        rows=self.client.get('/api/state').json()['audits']
        self.assertFalse(any(x['action'].startswith(('person_','face_')) for x in rows))
    def test_bad_reference_id_is_validation_error(self):
        r=self.post('people/reference-state',{'person':str(self.person.id),'reference':'not-a-uuid','version':self.person.version,'active':False})
        self.assertEqual(r.status_code,422)
    def test_account_disabled_during_index_discards_results(self):
        FaceObservation.objects.all().delete();FacePhotoIndex.objects.all().delete()
        def result(rows):User.objects.filter(id=self.member.id).update(is_active=False);return [{'id':str(self.p.id),'faces':[face()]}]
        with patch('core.people.extract',side_effect=result):r=self.post('people/index',{'person':str(self.person.id)})
        self.assertEqual(r.status_code,403);self.assertFalse(FaceObservation.objects.exists())
