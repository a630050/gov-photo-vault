import uuid
from django.db import models

class Admission(models.Model):
    id=models.UUIDField(primary_key=True)
    token_sha=models.CharField(max_length=64)
    label=models.CharField(max_length=250)
    username=models.CharField(max_length=150)
    fields=models.JSONField(default=list)
    expires=models.DateTimeField()
    max_files=models.PositiveIntegerField()
    max_bytes=models.PositiveBigIntegerField()
    active=models.BooleanField(default=True)
    redeemed=models.BooleanField(default=False)
    updated=models.DateTimeField(auto_now=True)

class Upload(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    admission=models.ForeignKey(Admission,on_delete=models.PROTECT,related_name='uploads')
    name=models.CharField(max_length=255)
    sha=models.CharField(max_length=64)
    size=models.PositiveIntegerField()
    file=models.FileField(upload_to='incoming')
    preview=models.FileField(upload_to='previews')
    metadata=models.JSONField(default=dict)
    attrs=models.JSONField(default=dict)
    state=models.CharField(max_length=20,default='staged')
    message=models.CharField(max_length=250,blank=True)
    created=models.DateTimeField(auto_now_add=True)
    updated=models.DateTimeField(auto_now=True)
    class Meta: unique_together=[('admission','sha')]

class LoginThrottle(models.Model):
    key=models.CharField(primary_key=True,max_length=64)
    since=models.DateTimeField()
    failures=models.PositiveIntegerField(default=0)
