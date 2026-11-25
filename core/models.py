from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid
class Department(models.Model):
    name=models.CharField(max_length=100,unique=True)
class Membership(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE)
    department=models.ForeignKey(Department,on_delete=models.CASCADE)
    role=models.CharField(max_length=20,default='member')
    permissions=models.JSONField(default=dict)
    class Meta: unique_together=[('user','department')]
class Item(models.Model):
    department=models.ForeignKey(Department,on_delete=models.PROTECT)
    name=models.CharField(max_length=100)
    fields=models.JSONField(default=list)
    version=models.PositiveIntegerField(default=1)
    active=models.BooleanField(default=True)
class Dataset(models.Model):
    item=models.ForeignKey(Item,on_delete=models.PROTECT)
    name=models.CharField(max_length=120)
    creator=models.ForeignKey(User,on_delete=models.PROTECT)
    archived=models.BooleanField(default=False)
    version=models.PositiveIntegerField(default=1)
    class Meta: unique_together=[('item','name')]
class Photo(models.Model):
    dataset=models.ForeignKey(Dataset,on_delete=models.PROTECT)
    uploader=models.ForeignKey(User,on_delete=models.PROTECT)
    file=models.FileField(upload_to='originals/%Y/%m')
    preview=models.FileField(upload_to='previews',blank=True)
    name=models.CharField(max_length=255)
    sha=models.CharField(max_length=64)
    status=models.CharField(max_length=20,default='draft')
    previous_status=models.CharField(max_length=20,default='submitted')
    captured=models.DateTimeField(null=True,blank=True)
    time_confirmed=models.BooleanField(default=False)
    lat=models.FloatField(null=True,blank=True)
    lon=models.FloatField(null=True,blank=True)
    attrs=models.JSONField(default=dict)
    form_snapshot=models.JSONField(default=list)
    form_version=models.PositiveIntegerField(default=1)
    tags=models.JSONField(default=list)
    version=models.PositiveIntegerField(default=1)
    created=models.DateTimeField(auto_now_add=True)
    extracted=models.JSONField(default=dict)
    dhash=models.CharField(max_length=16,blank=True,default='')
    burst_group=models.CharField(max_length=36,blank=True,default='',db_index=True)
    burst_cover=models.BooleanField(default=False)
    burst_cover_at=models.DateTimeField(null=True,blank=True)
    burst_algo=models.CharField(max_length=40,blank=True,default='')
    burst_params=models.JSONField(default=dict)
    admin_county=models.CharField(max_length=20,blank=True,default='',db_index=True)
    admin_town=models.CharField(max_length=20,blank=True,default='',db_index=True)
    admin_village=models.CharField(max_length=30,blank=True,default='')
    admin_status=models.CharField(max_length=24,blank=True,default='',db_index=True)
    admin_version=models.CharField(max_length=60,blank=True,default='')
    admin_updated_at=models.DateTimeField(null=True,blank=True)
    admin_error=models.CharField(max_length=200,blank=True,default='')
    class Meta:
        constraints=[
            models.UniqueConstraint(
                fields=['dataset','burst_group'],
                condition=models.Q(burst_cover=True,status='submitted')&~models.Q(burst_group=''),
                name='uniq_submitted_burst_cover_per_group',
            )
        ]
        indexes=[
            models.Index(fields=['status','-captured','-id'],name='photo_status_cap_idx'),
            models.Index(fields=['dataset','status'],name='photo_ds_status_idx'),
            models.Index(fields=['lat','lon'],name='photo_lat_lon_idx'),
        ]
class Star(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE)
    photo=models.ForeignKey(Photo,on_delete=models.CASCADE)
    class Meta: unique_together=[('user','photo')]
class Request(models.Model):
    photo=models.ForeignKey(Photo,on_delete=models.PROTECT)
    author=models.ForeignKey(User,on_delete=models.PROTECT,related_name='+')
    kind=models.CharField(max_length=20)
    reason=models.TextField()
    payload=models.JSONField(default=dict)
    base_version=models.PositiveIntegerField()
    state=models.CharField(max_length=20,default='pending')
    reviewer=models.ForeignKey(User,null=True,on_delete=models.PROTECT,related_name='+')
    decision_reason=models.TextField(blank=True)
    created=models.DateTimeField(auto_now_add=True)
    before=models.JSONField(default=dict)
    closed_at=models.DateTimeField(null=True)
class RequestEvent(models.Model):
    request=models.ForeignKey(Request,on_delete=models.PROTECT,related_name='events')
    actor=models.ForeignKey(User,null=True,on_delete=models.PROTECT)
    action=models.CharField(max_length=30)
    detail=models.JSONField(default=dict)
    created=models.DateTimeField(default=timezone.now)
class Anomaly(models.Model):
    photo=models.ForeignKey(Photo,on_delete=models.PROTECT)
    author=models.ForeignKey(User,on_delete=models.PROTECT)
    text=models.TextField()
    state=models.CharField(max_length=20,default='pending')
    resolution=models.TextField(blank=True)
class Audit(models.Model):
    actor=models.ForeignKey(User,null=True,on_delete=models.PROTECT)
    action=models.CharField(max_length=100)
    target=models.CharField(max_length=100)
    detail=models.JSONField(default=dict)
    created=models.DateTimeField(auto_now_add=True)
class Notice(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE)
    text=models.TextField()
    read=models.BooleanField(default=False)
class Config(models.Model):
    radius=models.PositiveIntegerField(default=100)
    burst_gap_seconds=models.PositiveIntegerField(default=5)
    burst_dhash_max=models.PositiveIntegerField(default=14)
    burst_span_seconds=models.PositiveIntegerField(default=15)
class Revision(models.Model):
    photo=models.ForeignKey(Photo,on_delete=models.PROTECT)
    version=models.PositiveIntegerField()
    snapshot=models.JSONField(default=dict)
    actor=models.ForeignKey(User,on_delete=models.PROTECT)
    created=models.DateTimeField(auto_now_add=True)
class TagAssertion(models.Model):
    photo=models.ForeignKey(Photo,on_delete=models.PROTECT)
    tag=models.CharField(max_length=60)
    batch=models.CharField(max_length=36)
    actor=models.ForeignKey(User,on_delete=models.PROTECT)
    active=models.BooleanField(default=True)
class BackgroundJob(models.Model):
    """Persistent job record managed by the unified background job center."""

    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    kind=models.CharField(max_length=40,db_index=True)
    actor=models.ForeignKey(User,on_delete=models.PROTECT)
    dataset=models.ForeignKey(Dataset,null=True,blank=True,on_delete=models.PROTECT)
    scope=models.JSONField(default=dict)
    params=models.JSONField(default=dict)
    algorithm=models.CharField(max_length=80,blank=True,default='')
    state=models.CharField(max_length=20,default='pending',db_index=True)
    total=models.PositiveIntegerField(default=0)
    completed_count=models.PositiveIntegerField(default=0)
    failed_count=models.PositiveIntegerField(default=0)
    cancel_requested=models.BooleanField(default=False)
    lease_until=models.DateTimeField(null=True,blank=True)
    worker_id=models.CharField(max_length=80,blank=True,default='')
    cursor=models.JSONField(default=dict)
    result_data=models.JSONField(default=dict)
    error=models.TextField(blank=True,default='')
    created=models.DateTimeField(auto_now_add=True)
    started_at=models.DateTimeField(null=True,blank=True)
    finished_at=models.DateTimeField(null=True,blank=True)
    updated=models.DateTimeField(auto_now=True)

    class Meta:
        indexes=[
            models.Index(fields=['state','lease_until','created'],name='job_claim_idx'),
            models.Index(fields=['actor','-created'],name='job_actor_created_idx'),
        ]

class Export(models.Model):
    actor=models.ForeignKey(User,on_delete=models.PROTECT)
    job=models.ForeignKey(BackgroundJob,null=True,blank=True,on_delete=models.SET_NULL,related_name='exports')
    photo_ids=models.JSONField(default=list)
    file=models.FileField(upload_to='exports')
    created=models.DateTimeField(auto_now_add=True)
class FormPublication(models.Model):
    template=models.ForeignKey(Item,on_delete=models.PROTECT)
    version=models.PositiveIntegerField()
    name=models.CharField(max_length=100)
    fields=models.JSONField(default=list)
    actor=models.ForeignKey(User,null=True,on_delete=models.PROTECT)
    created=models.DateTimeField(auto_now_add=True)
    class Meta: unique_together=[('template','version')]

class SimilarityRun(models.Model):
    dataset=models.ForeignKey(Dataset,on_delete=models.PROTECT)
    actor=models.ForeignKey(User,on_delete=models.PROTECT)
    job=models.ForeignKey(BackgroundJob,null=True,blank=True,on_delete=models.SET_NULL,related_name='similarity_runs')
    created=models.DateTimeField(auto_now_add=True)
    updated=models.DateTimeField(auto_now=True)
    state=models.CharField(max_length=20,default='pending')
    hours=models.FloatField(default=24)
    algorithm=models.CharField(max_length=80,default='dhash64-ahash64-v1')
    thresholds=models.JSONField(default=dict)
    members=models.JSONField(default=list)
    fingerprints=models.JSONField(default=list)
    error=models.TextField(blank=True)

class SimilarityPair(models.Model):
    run=models.ForeignKey(SimilarityRun,on_delete=models.PROTECT,related_name='pairs')
    a=models.ForeignKey(Photo,on_delete=models.PROTECT,related_name='+')
    b=models.ForeignKey(Photo,on_delete=models.PROTECT,related_name='+')
    distances=models.JSONField(default=dict)
    gap_hours=models.FloatField(null=True)
    time_state=models.CharField(max_length=20)
    review=models.CharField(max_length=20,default='unreviewed')
    events=models.JSONField(default=list)
    class Meta: unique_together=[('run','a','b')]

class PhotoEmbedding(models.Model):
    photo=models.ForeignKey(Photo,on_delete=models.CASCADE,related_name='embeddings')
    model_key=models.CharField(max_length=120)
    source_sha=models.CharField(max_length=64)
    vector=models.JSONField(default=list)
    state=models.CharField(max_length=20,default='ready')
    error=models.CharField(max_length=250,blank=True)
    updated=models.DateTimeField(auto_now=True)
    class Meta: unique_together=[('photo','model_key')]

class Person(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    department=models.ForeignKey(Department,on_delete=models.PROTECT)
    code=models.CharField(max_length=60)
    name=models.CharField(max_length=100)
    notes=models.TextField(blank=True)
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    creator=models.ForeignKey(User,on_delete=models.PROTECT)
    created=models.DateTimeField(auto_now_add=True)
    class Meta: unique_together=[('department','code')]

class PersonReference(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    person=models.ForeignKey(Person,on_delete=models.PROTECT,related_name='references')
    file=models.FileField(upload_to='people/references')
    sha=models.CharField(max_length=64)
    vector=models.JSONField(default=list)
    bbox=models.JSONField(default=list)
    quality=models.JSONField(default=dict)
    model_key=models.CharField(max_length=120)
    active=models.BooleanField(default=True)
    creator=models.ForeignKey(User,on_delete=models.PROTECT)
    created=models.DateTimeField(auto_now_add=True)

class FacePhotoIndex(models.Model):
    photo=models.ForeignKey(Photo,on_delete=models.PROTECT)
    source_sha=models.CharField(max_length=64)
    model_key=models.CharField(max_length=120)
    state=models.CharField(max_length=20)
    error=models.CharField(max_length=250,blank=True)
    updated=models.DateTimeField(auto_now=True)
    class Meta: unique_together=[('photo','model_key')]

class FaceObservation(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    photo=models.ForeignKey(Photo,on_delete=models.PROTECT)
    source_sha=models.CharField(max_length=64)
    model_key=models.CharField(max_length=120)
    ordinal=models.PositiveIntegerField()
    vector=models.JSONField(default=list)
    bbox=models.JSONField(default=list)
    quality=models.JSONField(default=dict)
    class Meta: unique_together=[('photo','source_sha','model_key','ordinal')]

class PersonPhotoLink(models.Model):
    person=models.ForeignKey(Person,on_delete=models.PROTECT)
    face=models.ForeignKey(FaceObservation,on_delete=models.PROTECT)
    active=models.BooleanField(default=True)
    version=models.PositiveIntegerField(default=1)
    actor=models.ForeignKey(User,on_delete=models.PROTECT)
    created=models.DateTimeField(auto_now_add=True)
    class Meta: unique_together=[('person','face')]

class PersonLinkEvent(models.Model):
    link=models.ForeignKey(PersonPhotoLink,on_delete=models.PROTECT,related_name='events')
    actor=models.ForeignKey(User,on_delete=models.PROTECT)
    action=models.CharField(max_length=30)
    reason=models.TextField()
    detail=models.JSONField(default=dict)
    created=models.DateTimeField(auto_now_add=True)

class ExternalUploadGrant(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    user=models.ForeignKey(User,on_delete=models.PROTECT)
    dataset=models.ForeignKey(Dataset,on_delete=models.PROTECT)
    token_sha=models.CharField(max_length=64)
    form_id=models.PositiveIntegerField()
    form_version=models.PositiveIntegerField()
    fields=models.JSONField(default=list)
    expires=models.DateTimeField()
    max_files=models.PositiveIntegerField(default=20)
    max_bytes=models.PositiveBigIntegerField(default=200*1024*1024)
    revoked=models.BooleanField(default=False)
    created=models.DateTimeField(auto_now_add=True)
    synced=models.DateTimeField(null=True)
    sync_error=models.CharField(max_length=250,blank=True)

class ExternalImportReceipt(models.Model):
    id=models.UUIDField(primary_key=True,editable=False)
    grant=models.ForeignKey(ExternalUploadGrant,on_delete=models.PROTECT,related_name='receipts')
    sha=models.CharField(max_length=64)
    size=models.PositiveIntegerField(default=0)
    state=models.CharField(max_length=20,default='importing')
    photo=models.ForeignKey(Photo,null=True,on_delete=models.PROTECT)
    message=models.CharField(max_length=250,blank=True)
    lease_until=models.DateTimeField(null=True)
    updated=models.DateTimeField(auto_now=True)


class PhotoOcr(models.Model):
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name='ocr_records')
    model_key = models.CharField(max_length=120)
    source_sha = models.CharField(max_length=64)
    text = models.TextField(blank=True, default='')
    state = models.CharField(max_length=20, default='ready')
    error = models.CharField(max_length=250, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('photo', 'model_key')]


class AdminBoundaryVersion(models.Model):
    """Metadata and checksum for offline administrative boundary datasets."""

    version = models.CharField(max_length=60, unique=True)
    source = models.CharField(max_length=250)
    published_date = models.CharField(max_length=40, blank=True, default='')
    sha256 = models.CharField(max_length=64)
    crs = models.CharField(max_length=40, default='EPSG:4326 (WGS84)')
    file_path = models.CharField(max_length=300, blank=True, default='')
    active = models.BooleanField(default=True, db_index=True)
    activated_at = models.DateTimeField(default=timezone.now)
    imported_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)


class SavedSearch(models.Model):
    """User-saved search criteria (evaluated live against current data and permissions)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_searches')
    name = models.CharField(max_length=100)
    criteria = models.JSONField(default=dict)
    display_mode = models.CharField(max_length=16, default='map')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)


class WorkCollection(models.Model):
    """Named curated collection of photo links (personal or department-shared)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default='')
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name='owned_collections')
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    shared = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)


class WorkCollectionItem(models.Model):
    collection = models.ForeignKey(WorkCollection, on_delete=models.CASCADE, related_name='items')
    photo = models.ForeignKey(Photo, on_delete=models.CASCADE, related_name='collection_items')
    added_by = models.ForeignKey(User, on_delete=models.PROTECT)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('collection', 'photo')]


class StorageReconcileReport(models.Model):
    """Non-destructive file reconciliation & capacity snapshot report."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT)
    job = models.ForeignKey(BackgroundJob, null=True, blank=True, on_delete=models.SET_NULL)
    mode = models.CharField(max_length=30, default='report')
    grace_hours = models.PositiveIntegerField(default=24)
    summary = models.JSONField(default=dict)
    issues = models.JSONField(default=list)
    created = models.DateTimeField(auto_now_add=True)
