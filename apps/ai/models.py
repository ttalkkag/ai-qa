import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ModelProfile(models.Model):
    class ValidationStatus(models.TextChoices):
        UNVERIFIED = "UNVERIFIED", "미확인"
        QUEUED = "QUEUED", "점검 대기"
        VALIDATED = "VALIDATED", "호출 확인"
        FAILED = "FAILED", "점검 실패"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=32)
    version = models.PositiveIntegerField()
    model_id = models.CharField(max_length=100)
    effort = models.CharField(max_length=12)
    enabled = models.BooleanField(default=True)
    validation_status = models.CharField(max_length=12, choices=ValidationStatus.choices, default=ValidationStatus.UNVERIFIED)
    validated_cli_version = models.CharField(max_length=100, blank=True)
    validated_binary_hash = models.CharField(max_length=64, blank=True)
    validated_at = models.DateTimeField(null=True)
    validation_error = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "version"], name="ai_model_profile_version")]
        ordering = ["key", "-version"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            if any(getattr(original, field) != getattr(self, field) for field in ("key", "version", "model_id", "effort")):
                raise ValidationError("모델과 effort 변경은 새 프로필 버전으로 저장해야 합니다.")
        return super().save(*args, **kwargs)


class AIInvocation(models.Model):
    class State(models.TextChoices):
        QUEUED = "QUEUED", "대기"
        RUNNING = "RUNNING", "호출 중"
        SUCCEEDED = "SUCCEEDED", "완료"
        FAILED = "FAILED", "실패"
        CANCELLED = "CANCELLED", "취소"
        INTERRUPTED = "INTERRUPTED", "중단"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purpose = models.CharField(max_length=32)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    conversation = models.ForeignKey("conversations.Conversation", null=True, blank=True, on_delete=models.PROTECT)
    run = models.ForeignKey("runs.Run", null=True, blank=True, on_delete=models.PROTECT)
    profile = models.ForeignKey(ModelProfile, null=True, blank=True, on_delete=models.PROTECT)
    profile_snapshot = models.JSONField(default=dict)
    prompt_version = models.PositiveIntegerField(default=1)
    prompt = models.TextField(blank=True)
    context_manifest = models.JSONField(default=dict)
    context_manifest_hash = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.QUEUED)
    cli_version = models.CharField(max_length=100, blank=True)
    requested_model = models.CharField(max_length=100, blank=True)
    requested_effort = models.CharField(max_length=12, blank=True)
    effective_model = models.CharField(max_length=100, default="not_reported")
    effective_effort = models.CharField(max_length=12, default="not_reported")
    reported_tokens = models.JSONField(default=dict)
    final_response = models.TextField(blank=True)
    execution_events = models.JSONField(default=list)
    trace_complete = models.BooleanField(default=False)
    error_code = models.CharField(max_length=64, blank=True)
    exit_code = models.IntegerField(null=True)
    idempotency_key = models.CharField(max_length=128, blank=True)
    started_at = models.DateTimeField(null=True)
    ended_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["actor", "purpose", "idempotency_key"], condition=~Q(idempotency_key=""), name="ai_invocation_request_key")]


class SandboxCheck(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cli_binary_hash = models.CharField(max_length=64)
    cli_version = models.CharField(max_length=100)
    policy_hash = models.CharField(max_length=64)
    host_os_version = models.CharField(max_length=200)
    native_passed = models.BooleanField(default=False)
    file_tools_passed = models.BooleanField(default=False)
    checks = models.JSONField(default=list)
    error_code = models.CharField(max_length=64, blank=True)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    checked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-checked_at"]
