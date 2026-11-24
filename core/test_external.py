import hashlib,io,tempfile,uuid
from unittest.mock import patch
from datetime import timedelta
from PIL import Image
from django.test import TestCase,override_settings
from django.utils import timezone
from . import tests as core_tests
from .models import *
from .external_upload import receive,BridgeUnavailable,publish_grants

CFG={'url':'http://127.0.0.1:8002','bridge_key':'b'*48,'session_key':'s'*48}
class ExternalTests(TestCase):
    post=core_tests.CoreTests.post
    def setUp(self):
        core_tests.CoreTests.setUp(self)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.override=override_settings(MEDIA_ROOT=self.temp.name);self.override.enable();self.addCleanup(self.override.disable)
        self.cfg=patch('core.external_upload.config',return_value=CFG);self.cfg.start();self.addCleanup(self.cfg.stop)
        out=io.BytesIO();Image.new('RGB',(120,80),'orange').save(out,'JPEG');self.raw=out.getvalue()
    def issue(self,**kw):
        r=self.post('external/issue',{'dataset':self.ds.id,**kw});self.assertEqual(r.status_code,200,r.content);return r.json()
    def meta(self,g):return {'id':str(uuid.uuid4()),'grant':g['grant']['id'],'name':'external.jpg','sha':hashlib.sha256(self.raw).hexdigest(),'size':len(self.raw),'attrs':{'note':'外部填寫'}}
    def import_file(self,meta):
        with patch('core.external_upload.remote',return_value=self.raw):return receive(meta)
    def test_employee_scope_quotas_and_secret_not_in_history(self):
        g=self.issue(max_files=3,hours=2);record=ExternalUploadGrant.objects.get(id=g['grant']['id'])
        self.assertEqual(record.token_sha,hashlib.sha256(g['code'].encode()).hexdigest());self.assertNotIn(g['code'],str(self.post('external/list',{}).json()))
        self.client.force_login(self.out);self.assertEqual(self.post('external/list',{}).json()['grants'],[]);self.assertEqual(self.post('external/revoke',{'id':str(record.id)}).status_code,404)
        self.assertEqual(self.post('external/issue',{'dataset':self.ds.id}).status_code,403)
    def test_read_only_archived_and_invalid_targets_rejected(self):
        m=Membership.objects.get(user=self.member);m.role='viewer';m.save();self.assertEqual(self.post('external/issue',{'dataset':self.ds.id}).status_code,403)
        m.role='member';m.save();self.ds.archived=True;self.ds.save();self.assertEqual(self.post('external/issue',{'dataset':self.ds.id}).status_code,409)
        self.ds.archived=False;self.ds.save()
        for data in [{'hours':0},{'hours':25},{'max_files':51},{'hours':'NaN'}]:self.assertEqual(self.post('external/issue',{'dataset':self.ds.id,**data}).status_code,422)
    def test_actual_image_import_creates_draft_once(self):
        g=self.issue();meta=self.meta(g);state,message=self.import_file(meta);self.assertEqual(state,'imported')
        p=ExternalImportReceipt.objects.get(id=meta['id']).photo;self.assertEqual(p.status,'draft');self.assertEqual(p.uploader,self.member);self.assertEqual(p.attrs,{'note':'外部填寫'});self.assertEqual(p.file.read(),self.raw);p.file.close()
        self.assertIsNone(p.captured);self.assertEqual(p.form_snapshot,self.item.fields)
        with patch('core.external_upload.remote') as fetch:self.assertEqual(receive(meta)[0],'imported');fetch.assert_not_called()
        self.assertEqual(Photo.objects.filter(sha=meta['sha']).count(),1)
    def test_dataset_duplicates_do_not_create_second_photo(self):
        g=self.issue();one=self.meta(g);self.import_file(one);two={**one,'id':str(uuid.uuid4())};self.assertEqual(self.import_file(two)[0],'duplicate');self.assertEqual(Photo.objects.filter(sha=one['sha']).count(),1)
    def test_revoke_expiry_and_membership_change_before_pull(self):
        for action in ['revoke','expire','permission']:
            g=self.issue();meta=self.meta(g);record=ExternalUploadGrant.objects.get(id=g['grant']['id'])
            if action=='revoke':record.revoked=True;record.save()
            elif action=='expire':record.expires=timezone.now()-timedelta(seconds=1);record.save()
            else:Membership.objects.filter(user=self.member).update(permissions={'upload':False})
            with patch('core.external_upload.remote') as fetch:self.assertEqual(receive(meta)[0],'rejected');fetch.assert_not_called()
        self.assertFalse(Photo.objects.filter(sha=hashlib.sha256(self.raw).hexdigest()).exists())
    def test_form_changes_are_rejected(self):
        g=self.issue();self.item.version+=1;self.item.save();self.assertEqual(self.import_file(self.meta(g))[0],'rejected')
    def test_hash_and_form_validation_are_authoritative_inside(self):
        g=self.issue();meta=self.meta(g);meta['sha']='0'*64;self.assertEqual(self.import_file(meta)[0],'rejected')
        g=self.issue();meta=self.meta(g);meta['attrs']={};self.assertEqual(self.import_file(meta)[0],'rejected')
        self.assertFalse(Photo.objects.filter(sha=hashlib.sha256(self.raw).hexdigest()).exists())
    def test_revoke_during_fetch_discards_payload(self):
        g=self.issue();meta=self.meta(g)
        def fetch(*args,**kwargs):ExternalUploadGrant.objects.filter(id=g['grant']['id']).update(revoked=True);return self.raw
        with patch('core.external_upload.remote',side_effect=fetch):self.assertEqual(receive(meta)[0],'rejected')
        self.assertFalse(Photo.objects.filter(sha=meta['sha']).exists())
    def test_transient_fetch_failure_is_retryable(self):
        g=self.issue();meta=self.meta(g)
        with patch('core.external_upload.remote',side_effect=BridgeUnavailable('offline')):
            with self.assertRaises(BridgeUnavailable):receive(meta)
        self.assertEqual(ExternalImportReceipt.objects.get(id=meta['id']).state,'retry');self.assertEqual(self.import_file(meta)[0],'imported')
    def test_live_lease_prevents_duplicate_fetch(self):
        g=self.issue();meta=self.meta(g);ExternalImportReceipt.objects.create(id=meta['id'],grant_id=g['grant']['id'],sha=meta['sha'],size=meta['size'],state='importing',lease_until=timezone.now()+timedelta(minutes=1))
        with patch('core.external_upload.remote') as fetch:self.assertEqual(receive(meta),(None,None));fetch.assert_not_called()
        ExternalImportReceipt.objects.filter(id=meta['id']).update(lease_until=timezone.now()-timedelta(seconds=1));self.assertEqual(self.import_file(meta)[0],'imported')
    def test_internal_file_count_quota_cannot_be_bypassed(self):
        g=self.issue(max_files=1);first=self.meta(g);self.import_file(first);second={**first,'id':str(uuid.uuid4())}
        with patch('core.external_upload.remote') as fetch:self.assertEqual(receive(second)[0],'rejected');fetch.assert_not_called()
    def test_publish_mirrors_revocation_and_current_scope(self):
        g=self.issue();ExternalUploadGrant.objects.filter(id=g['grant']['id']).update(revoked=True)
        with patch('core.external_upload.remote',return_value={'ok':True}) as push:publish_grants()
        self.assertFalse(push.call_args[0][1]['grants'][0]['active'])
