import hashlib,io,json,tempfile,uuid
from datetime import timedelta
from django.test import TestCase,Client,override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from .models import Admission,Upload

class GatewayTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.override=override_settings(MEDIA_ROOT=self.tmp.name);self.override.enable();self.addCleanup(self.override.disable)
        self.token=str(uuid.uuid4())+'.test-credential';self.a=Admission.objects.create(id=self.token.split('.')[0],token_sha=hashlib.sha256(self.token.encode()).hexdigest(),label='指定科室／指定資料集',username='member',fields=[{'key':'note','label':'現場說明','type':'text','required':True}],expires=timezone.now()+timedelta(hours=1),max_files=5,max_bytes=50*1024*1024)
    def post(self,path,data,client=None):return (client or self.client).post('/api/'+path,json.dumps(data),content_type='application/json')
    def login(self):self.assertEqual(self.post('login',{'code':self.token}).status_code,200)
    def file(self,name='test.jpg'):
        out=io.BytesIO();Image.new('RGB',(60,60),'red').save(out,'JPEG');return SimpleUploadedFile(name,out.getvalue(),content_type='image/jpeg')
    def stage(self):
        r=self.client.post('/api/stage',{'files':[self.file()]});self.assertEqual(r.status_code,200,r.content);return r.json()['results'][0]['id']
    def test_login_only_and_no_internal_routes(self):
        self.assertEqual(self.client.get('/api/me').status_code,401)
        for path in ['/admin/','/api/state','/api/search','/api/people/list','/media/1/original','/static/app.js']:
            self.assertIn(self.client.get(path).status_code,[401,404,405])
        self.login();self.assertEqual(self.client.get('/api/me').json()['destination'],'指定科室／指定資料集')
        self.assertEqual(self.post('login',{'code':self.token},client=Client()).status_code,401)
    def test_stage_preview_form_and_commit(self):
        self.login();id=self.stage();u=Upload.objects.get(id=id);self.assertEqual(u.state,'staged');self.assertIsNone(u.metadata['captured'])
        self.assertEqual(self.post('commit',{'uploads':[{'id':id,'attrs':{}}]}).status_code,422)
        r=self.post('commit',{'uploads':[{'id':id,'attrs':{'note':'確認後接收'}}]});self.assertEqual(r.status_code,200);u.refresh_from_db();self.assertEqual(u.state,'pending')
        self.assertEqual(self.post('commit',{'uploads':[{'id':id,'attrs':{'note':'確認後接收'}}]}).status_code,200)
        self.assertEqual(self.post('commit',{'uploads':[{'id':id,'attrs':{'note':'外網不能更改已提交資料'}}]}).status_code,409)
    def test_only_own_progress_and_preview(self):
        self.login();id=self.stage();other=Client();other_session=other.session;other_session['admission']=str(uuid.uuid4());other_session.save()
        self.assertEqual(other.get('/preview/'+id).status_code,401)
        response=self.client.get('/preview/'+id);self.assertEqual(response.status_code,200);self.assertEqual(response['Cache-Control'],'private, no-store');response.close()
    def test_duplicate_files_and_quotas(self):
        self.login();id=self.stage();self.assertEqual(self.stage(),id);self.assertEqual(Upload.objects.count(),1)
        self.a.max_files=1;self.a.save();out=io.BytesIO();Image.new('RGB',(50,50),'blue').save(out,'PNG')
        r=self.client.post('/api/stage',{'files':[SimpleUploadedFile('other.png',out.getvalue())]});self.assertIn('error',r.json()['results'][0])
    def test_revoked_and_expired_session_stops_access(self):
        self.login();id=self.stage();self.a.active=False;self.a.save();self.assertEqual(self.client.get('/api/me').status_code,401);self.assertEqual(self.client.get('/preview/'+id).status_code,401)
    def test_bridge_requires_separate_secret(self):
        self.login();self.assertEqual(self.client.get('/bridge/pending').status_code,403);self.assertEqual(self.client.get('/bridge/blob/'+str(uuid.uuid4())).status_code,403)
    def test_bridge_receipt_is_idempotent_and_cannot_be_overwritten(self):
        from django.conf import settings
        self.login();id=self.stage();self.post('commit',{'uploads':[{'id':id,'attrs':{'note':'ready'}}]});u=Upload.objects.get(id=id)
        headers={'HTTP_AUTHORIZATION':'Bearer '+settings.GATEWAY_CONFIG['bridge_key']};payload={'id':id,'grant':str(self.a.id),'sha':u.sha,'state':'imported','message':'已匯入內網草稿'}
        for _ in range(2):self.assertEqual(self.client.post('/bridge/ack',json.dumps(payload),content_type='application/json',**headers).status_code,200)
        payload['state']='rejected';self.assertEqual(self.client.post('/bridge/ack',json.dumps(payload),content_type='application/json',**headers).status_code,409)
    def test_csrf_is_required_for_browser_upload(self):
        strict=Client(enforce_csrf_checks=True);self.assertEqual(self.post('login',{'code':self.token},client=strict).status_code,403)
    def test_invalid_images_and_unknown_fields_rejected(self):
        self.login();r=self.client.post('/api/stage',{'files':[SimpleUploadedFile('x.jpg',b'bad')]});self.assertIn('error',r.json()['results'][0]);self.assertEqual(Upload.objects.count(),0)
        id=self.stage();self.assertEqual(self.post('commit',{'uploads':[{'id':id,'attrs':{'note':'ok','owner':'forged'}}]}).status_code,422)
    def test_other_valid_admission_cannot_read_or_commit_my_upload(self):
        self.login();id=self.stage();other=Admission.objects.create(id=uuid.uuid4(),token_sha='x'*64,label='other',username='other',fields=[],expires=timezone.now()+timedelta(hours=1),max_files=2,max_bytes=10000)
        client=Client();s=client.session;s['admission']=str(other.id);s.save()
        self.assertEqual(client.get('/preview/'+id).status_code,404);self.assertEqual(client.get('/api/me').json()['uploads'],[])
        self.assertEqual(self.post('commit',{'uploads':[{'id':id,'attrs':{}}]},client=client).status_code,404)
    def test_invalid_codes_are_throttled(self):
        for _ in range(10):self.assertEqual(self.post('login',{'code':'invalid'}).status_code,401)
        self.assertEqual(self.post('login',{'code':'invalid'}).status_code,429)
    def test_non_ascii_bridge_header_is_rejected_without_server_error(self):
        self.assertEqual(self.client.get('/bridge/pending',HTTP_AUTHORIZATION='Bearer é').status_code,403)
