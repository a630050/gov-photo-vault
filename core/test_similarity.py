import io
import tempfile
from datetime import timedelta
from django.test import TestCase, override_settings
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image
from . import tests as core_tests
from .models import Photo,Dataset,Membership,SimilarityRun,SimilarityPair,Anomaly
from .similarity import fingerprint,distances

class SimilarityTests(TestCase):
    post=core_tests.CoreTests.post
    def setUp(self):
        core_tests.CoreTests.setUp(self)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=override_settings(MEDIA_ROOT=self.tmp.name);self.settings.enable();self.addCleanup(self.settings.disable)
        self.p.captured=timezone.now();self.p.time_confirmed=True
        self.image(self.p)
        self.b=Photo.objects.create(dataset=self.ds,uploader=self.member,name='copy.jpg',sha='different-container',status='submitted',captured=self.p.captured-timedelta(hours=25),time_confirmed=True)
        self.image(self.b)
    def image(self,p,invert=False):
        im=Image.new('RGB',(80,80));im.putdata([((255-(x*31+y*17)%256) if invert else (x*31+y*17)%256,)*3 for y in range(80) for x in range(80)])
        stream=io.BytesIO();im.save(stream,'JPEG');p.file.save(p.name,ContentFile(stream.getvalue()),save=False);p.save()
    def start(self,**data):
        response=self.post('similarity/start',{'dataset':self.ds.id,'hours':24,**data});self.assertEqual(response.status_code,200,response.content)
        return response.json()['id']
    def complete(self,**data):
        id=self.start(**data);r=self.post('similarity/step',{'run':id});self.assertEqual(r.status_code,200,r.content);self.assertEqual(r.json()['state'],'completed');return id
    def test_manual_only_and_same_dataset(self):
        second=Dataset.objects.create(item=self.item,name='other',creator=self.member)
        outsider=Photo.objects.create(dataset=second,uploader=self.member,name='outside',sha='x',status='submitted');self.image(outsider)
        self.assertFalse(SimilarityRun.objects.exists())
        id=self.complete();run=SimilarityRun.objects.get(id=id)
        self.assertEqual({m['id'] for m in run.members},{self.p.id,self.b.id});self.assertEqual(run.pairs.count(),1)
        self.assertEqual(self.post('similarity/start',{'dataset':self.ds.id,'photos':[self.p.id,outsider.id]}).status_code,422)
    def test_visual_hash_and_unrelated_image(self):
        self.assertEqual(distances(fingerprint(self.p.file),fingerprint(self.b.file)),{'dhash':0,'ahash':0})
        self.image(self.b,invert=True);self.complete();self.assertFalse(SimilarityPair.objects.exists())
    def test_time_threshold_strict_and_custom(self):
        self.b.captured=self.p.captured-timedelta(hours=24);self.b.save()
        self.complete();self.assertEqual(SimilarityPair.objects.last().time_state,'within')
        self.complete(hours=23.5);self.assertEqual(SimilarityPair.objects.last().time_state,'over')
        for hours in [-1,'NaN',True]:self.assertEqual(self.post('similarity/start',{'dataset':self.ds.id,'hours':hours}).status_code,422)
    def test_missing_or_unconfirmed_time_needs_confirmation(self):
        self.b.captured=None;self.b.save();self.complete();p=SimilarityPair.objects.last();self.assertEqual(p.time_state,'needs_time');self.assertIsNone(p.gap_hours)
        self.b.captured=self.p.captured-timedelta(hours=99);self.b.time_confirmed=False;self.b.save();self.complete();self.assertEqual(SimilarityPair.objects.last().time_state,'needs_time')
    def test_independent_permission_and_revocation(self):
        m=Membership.objects.get(user=self.member);m.permissions={'analyze':False};m.save()
        self.assertEqual(self.post('similarity/start',{'dataset':self.ds.id}).status_code,403)
        m.permissions={'analyze':True,'organize':False};m.save();id=self.complete();pair=SimilarityPair.objects.get(run_id=id)
        body={'pair':pair.id,'review':'anomaly','note':'test','events':0}
        self.assertEqual(self.post('similarity/review',body).status_code,403)
        m.delete();self.assertEqual(self.post('similarity/results',{'run':id}).status_code,403)
    def test_cancel_and_snapshot_change(self):
        id=self.start();self.post('similarity/cancel',{'run':id});r=self.post('similarity/step',{'run':id});self.assertEqual(r.json()['state'],'cancelled');self.assertFalse(SimilarityPair.objects.exists())
        id=self.start();self.b.version+=1;self.b.save();r=self.post('similarity/step',{'run':id});self.assertEqual(r.json()['state'],'failed')
    def test_bad_image_is_explicit_partial_result(self):
        self.b.file.save('bad.jpg',ContentFile(b'not an image'));id=self.complete();r=self.post('similarity/results',{'run':id}).json();self.assertEqual(r['run']['failed'],1);self.assertEqual(r['failures'][0]['id'],self.b.id);self.assertEqual(r['pairs'],[])
    def test_reviews_create_manual_pending_anomalies_and_audit_history(self):
        id=self.complete();p=SimilarityPair.objects.get(run_id=id)
        self.assertEqual(self.post('similarity/review',{'pair':p.id,'review':'clear','note':'same scene','events':0}).status_code,200)
        self.assertEqual(self.post('similarity/review',{'pair':p.id,'review':'anomaly','note':'suspect','events':0}).status_code,409)
        self.assertEqual(self.post('similarity/review',{'pair':p.id,'review':'anomaly','note':'suspect','events':1}).status_code,200)
        self.assertEqual(Anomaly.objects.count(),2);self.assertEqual(set(Anomaly.objects.values_list('state',flat=True)),{'pending'})
        p.refresh_from_db();self.assertEqual(len(p.events),2)
        self.assertEqual(self.post('similarity/review',{'pair':p.id,'review':'anomaly','note':'again','events':2}).status_code,409)
        new=self.complete();self.assertEqual(SimilarityPair.objects.get(run_id=new).review,'unreviewed')
    def test_stale_results_and_pair_authorization(self):
        id=self.complete();pair=SimilarityPair.objects.get(run_id=id)
        self.assertEqual(self.post('similarity/pair',{'pair':pair.id}).status_code,200) # no GPS needed
        self.b.version+=1;self.b.save();r=self.post('similarity/results',{'run':id}).json();self.assertTrue(r['run']['stale']);self.assertTrue(r['pairs'][0]['stale'])
        self.assertEqual(self.post('similarity/review',{'pair':pair.id,'review':'clear','events':0}).status_code,409)
        self.b.status='recycled';self.b.save();r=self.post('similarity/results',{'run':id}).json();self.assertTrue(r['pairs'][0]['unavailable']);self.assertNotIn('a',r['pairs'][0]);self.assertEqual(self.post('similarity/pair',{'pair':pair.id}).status_code,409)
    def test_step_is_idempotent_and_membership_fixed(self):
        id=self.start();self.assertEqual(self.start(),id)
        extra=Photo.objects.create(dataset=self.ds,uploader=self.member,name='new.jpg',sha='new',status='submitted');self.image(extra)
        for _ in range(2):self.assertEqual(self.post('similarity/step',{'run':id}).status_code,200)
        run=SimilarityRun.objects.get(id=id);self.assertEqual(len(run.members),2);self.assertEqual(run.pairs.count(),1)
