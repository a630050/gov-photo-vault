from unittest.mock import patch
from django.test import TestCase
from . import tests as core_tests
from .models import Photo,Dataset,Item,Membership,PhotoEmbedding,TagAssertion,Audit
from .semantic import MODEL_KEY,DIMENSIONS,index_batch,pending_photos
from .semantic_engine import ModelUnavailable

def vector(first=1,second=0):return [first,second]+[0]*(DIMENSIONS-2)

class SemanticTests(TestCase):
    post=core_tests.CoreTests.post
    def setUp(self):
        core_tests.CoreTests.setUp(self)
        self.model=patch('core.semantic.text_vector',return_value=vector());self.encode=self.model.start();self.addCleanup(self.model.stop)
        self.embedding(self.p)
    def embedding(self,p,**kw):return PhotoEmbedding.objects.create(photo=p,model_key=MODEL_KEY,source_sha=p.sha,vector=vector(),**kw)
    def search(self,**kw):return self.post('semantic/search',{'query':'道路積水','minimum':0,**kw})
    def test_scope_before_ranking_and_dataset_filter(self):
        i=Item.objects.create(department=self.other,name='other');ds=Dataset.objects.create(item=i,name='hidden',creator=self.out)
        hidden=Photo.objects.create(dataset=ds,uploader=self.out,name='secret',sha='h',status='submitted');self.embedding(hidden)
        e=PhotoEmbedding.objects.get(photo=self.p);e.vector=vector(.6,.8);e.save()
        r=self.search(limit=1).json();self.assertEqual(r['photos'][0]['id'],self.p.id);self.assertEqual(r['total'],1);self.assertEqual(r['matches'],1)
        r=self.search(datasets=[ds.id]).json();self.assertEqual(r['photos'],[]);self.assertEqual(r['total'],0)
    def test_revocation_during_inference_rechecks_scope(self):
        self.encode.side_effect=lambda text:(Membership.objects.filter(user=self.member).delete(),vector())[1]
        r=self.search().json();self.assertEqual(r['photos'],[]);self.assertEqual(r['indexed'],0)
    def test_only_submitted_current_model_and_source(self):
        for status in ['draft','recycled']:
            p=Photo.objects.create(dataset=self.ds,uploader=self.member,name=status,sha=status,status=status);self.embedding(p)
        stale=Photo.objects.create(dataset=self.ds,uploader=self.member,name='stale',sha='new',status='submitted')
        PhotoEmbedding.objects.create(photo=stale,model_key=MODEL_KEY,source_sha='old',vector=vector())
        r=self.search().json();self.assertEqual([x['id'] for x in r['photos']],[self.p.id]);self.assertEqual(r['pending'],1);self.assertEqual(r['total'],2)
    def test_coverage_empty_scope_no_inference(self):
        PhotoEmbedding.objects.all().delete();r=self.search().json();self.assertEqual(r['pending'],1);self.assertEqual(r['photos'],[]);self.encode.assert_not_called()
    def test_invalid_query_threshold_and_unavailable_model(self):
        for query in ['',[], '字'*101]:self.assertEqual(self.search(query=query).status_code,422)
        self.assertEqual(self.search(minimum=2).status_code,422)
        self.assertEqual(self.search(datasets='1').status_code,422)
        self.encode.side_effect=ModelUnavailable('offline failure');r=self.search();self.assertEqual(r.status_code,503)
    def test_minimum_limit_and_explicit_truncation(self):
        p=Photo.objects.create(dataset=self.ds,uploader=self.member,name='two',sha='2',status='submitted');e=self.embedding(p);e.vector=vector(.6,.8);e.save()
        r=self.search(limit=1).json();self.assertEqual(r['returned'],1);self.assertTrue(r['limited']);self.assertEqual(r['matches'],2)
        self.assertEqual(self.search(minimum=.7).json()['matches'],1)
    def test_tags_transaction_rechecks_all_permissions(self):
        p=Photo.objects.create(dataset=self.ds,uploader=self.member,name='two',sha='2',status='submitted')
        self.assertEqual(self.post('semantic/tag',{'photos':[self.p.id,p.id],'tags':['巡查','巡查']}).status_code,200)
        self.p.refresh_from_db();self.assertEqual(self.p.tags,['巡查'])
        m=Membership.objects.get(user=self.member);m.permissions={'organize':False};m.save()
        self.assertEqual(self.post('semantic/tag',{'photos':[self.p.id],'tags':['不可寫']}).status_code,403)
        self.assertNotIn('不可寫',Photo.objects.get(id=self.p.id).tags)
    def test_invalid_scope_or_archive_is_atomic(self):
        second=Dataset.objects.create(item=self.item,name='archived',creator=self.member,archived=True)
        p=Photo.objects.create(dataset=second,uploader=self.member,name='archived',sha='2',status='submitted')
        self.assertEqual(self.post('semantic/tag',{'photos':[self.p.id,p.id],'tags':['test']}).status_code,409)
        self.assertEqual(Photo.objects.get(id=self.p.id).tags,[])
        self.assertEqual(self.post('semantic/tag',{'photos':[self.p.id,99999],'tags':['test']}).status_code,409)
        self.assertFalse(TagAssertion.objects.exists())
    def test_batch_undo_preserves_later_source_and_audit(self):
        first=self.post('semantic/tag',{'photos':[self.p.id],'tags':['巡查']}).json()
        second=self.post('semantic/tag',{'photos':[self.p.id],'tags':['巡查','待查']}).json()
        self.post('undo_tags',{'batch':first['batch']});self.p.refresh_from_db();self.assertEqual(set(self.p.tags),{'巡查','待查'})
        self.post('undo_tags',{'batch':second['batch']});self.p.refresh_from_db();self.assertEqual(self.p.tags,[])
        self.assertEqual(Audit.objects.filter(action='semantic_batch_tag').count(),2)
    def test_read_only_user_can_search_but_not_tag(self):
        m=Membership.objects.get(user=self.member);m.role='viewer';m.save()
        self.assertEqual(self.search().json()['returned'],1)
        self.assertEqual(self.post('semantic/tag',{'photos':[self.p.id],'tags':['test']}).status_code,403)
    def test_index_and_failed_files_are_explicit(self):
        PhotoEmbedding.objects.all().delete()
        with patch('core.semantic.image_vectors',return_value=[{'id':self.p.id,'error':'bad image'}]):self.assertEqual(index_batch(),1)
        self.assertFalse(pending_photos(Photo.objects.all()).exists())
        e=PhotoEmbedding.objects.get(photo=self.p);self.assertEqual(e.state,'failed');self.assertEqual(self.search().json()['failed'],1)
        with patch('core.semantic.image_vectors',return_value=[{'id':self.p.id,'vector':vector()}]):index_batch(retry=True)
        self.assertEqual(self.search().json()['indexed'],1)
    def test_index_does_not_publish_recycled_or_changed_source(self):
        PhotoEmbedding.objects.all().delete()
        def infer(rows):
            Photo.objects.filter(id=self.p.id).update(status='recycled');return [{'id':self.p.id,'vector':vector()}]
        with patch('core.semantic.image_vectors',side_effect=infer):index_batch()
        self.assertFalse(PhotoEmbedding.objects.exists())
