import uuid

from django.conf import settings
from django.db import models


class ResultBundle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey("runs.Run", on_delete=models.PROTECT, related_name="result_bundles")
    request_revision = models.ForeignKey("work_requests.RequestRevision", on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    baseline_commit_sha = models.CharField(max_length=64)
    project_config_version = models.PositiveIntegerField()
    manifest_sha256 = models.CharField(max_length=64)
    result_tree_sha256 = models.CharField(max_length=64)
    completeness = models.CharField(max_length=16, default="INCOMPLETE")
    visibility = models.CharField(max_length=16, default="NORMAL")
    corruption_reason = models.CharField(max_length=64, blank=True)
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sealed_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "version"], name="unique_result_run_version")]


class Artifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    conversation = models.ForeignKey("conversations.Conversation", null=True, blank=True, on_delete=models.PROTECT)
    bundle = models.ForeignKey(ResultBundle, null=True, blank=True, on_delete=models.PROTECT, related_name="artifacts")
    kind = models.CharField(max_length=32)
    storage_key = models.CharField(max_length=1024, unique=True)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    mime_type = models.CharField(max_length=100)
    display_name = models.CharField(max_length=255)
    visibility = models.CharField(max_length=16, default="NORMAL")
    required_for_publication = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(conversation__isnull=False, bundle__isnull=True) | models.Q(conversation__isnull=True, bundle__isnull=False)), name="artifact_exactly_one_parent")]


class ResultReview(models.Model):
    class State(models.TextChoices):
        AWAITING_ADMIN = "AWAITING_ADMIN", "관리자 승인 대기"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "재수정 요청됨"
        APPROVED = "APPROVED", "관리자 승인 완료"
        REJECTED = "REJECTED", "관리자 보완 요청"

    bundle = models.OneToOneField(ResultBundle, on_delete=models.PROTECT, related_name="review")
    manifest_sha256 = models.CharField(max_length=64)
    state = models.CharField(max_length=24, choices=State.choices)
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="accepted_results")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="decided_results")
    feedback = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ChangedFile(models.Model):
    bundle = models.ForeignKey(ResultBundle, on_delete=models.PROTECT, related_name="changed_files")
    path = models.TextField()
    change = models.CharField(max_length=8)
    before_mode = models.PositiveIntegerField(null=True)
    after_mode = models.PositiveIntegerField(null=True)
    before_sha256 = models.CharField(max_length=64, blank=True)
    after_sha256 = models.CharField(max_length=64, blank=True)
    binary = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["bundle", "path"], name="unique_bundle_changed_path")]


class ArtifactPin(models.Model):
    artifact = models.ForeignKey(Artifact, null=True, blank=True, on_delete=models.CASCADE, related_name="pins")
    bundle = models.ForeignKey(ResultBundle, null=True, blank=True, on_delete=models.CASCADE, related_name="pins")
    reference = models.CharField(max_length=160)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(models.Q(artifact__isnull=False, bundle__isnull=True) | models.Q(artifact__isnull=True, bundle__isnull=False)), name="pin_exactly_one_parent"),
            models.UniqueConstraint(fields=["artifact", "reference"], name="unique_artifact_pin_reference"),
            models.UniqueConstraint(fields=["bundle", "reference"], name="unique_bundle_pin_reference"),
        ]


class ArtifactExpiry(models.Model):
    artifact = models.OneToOneField(Artifact, on_delete=models.PROTECT, related_name="expiry_record")
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    scheduled_at = models.DateTimeField()
    expired_at = models.DateTimeField()
    reason = models.CharField(max_length=64)


class ArtifactAccessEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    project = models.ForeignKey("projects.Project", null=True, on_delete=models.SET_NULL)
    artifact = models.ForeignKey(Artifact, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=24)
    reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
