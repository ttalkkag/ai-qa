import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "관리자"
        MANAGER = "MANAGER", "매니저"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    display_name = models.CharField(max_length=100)
    role = models.CharField(max_length=7, choices=Role.choices, default=Role.MANAGER)
    must_change_password = models.BooleanField(default=False)
    authz_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)


class ProjectMembership(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="memberships")
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="memberships")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="granted_memberships")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "project"], name="unique_project_membership")]


class AccountAudit(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="account_actions")
    bootstrap_identity = models.CharField(max_length=30, blank=True)
    action = models.CharField(max_length=80)
    subject = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="account_audits")
    project = models.ForeignKey("projects.Project", null=True, on_delete=models.PROTECT)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    failures = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(auto_now=True)
