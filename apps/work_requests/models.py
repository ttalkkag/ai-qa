import uuid

from django.conf import settings
from django.db import models


class WorkRequest(models.Model):
    class State(models.TextChoices):
        DRAFT = "DRAFT", "요구 보완 중"
        READY = "READY", "명세 확인 완료"
        SUBMITTED = "SUBMITTED", "실행 접수됨"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    conversation = models.OneToOneField("conversations.Conversation", on_delete=models.PROTECT, related_name="work_request")
    state = models.CharField(max_length=16, choices=State.choices, default=State.DRAFT)
    draft_version = models.PositiveIntegerField(default=1)
    draft = models.JSONField(default=dict)
    ready_revision = models.ForeignKey("RequestRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="ready_requests")
    latest_submitted_revision = models.ForeignKey("RequestRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="submitted_requests")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def title(self):
        return self.draft.get("title", "새 요청")

    @property
    def progress_label(self):
        from apps.artifacts.models import ResultReview
        from apps.runs.models import Run
        run = Run.objects.filter(request_revision__request=self).first()
        if run is None or self.draft_version != run.request_revision.source_draft_version:
            return self.get_state_display()
        review = ResultReview.objects.filter(bundle_id=run.result_bundle_id).first()
        if review:
            return review.get_state_display()
        return "사용자 결과 확인 대기" if run.status == "SUCCEEDED" else run.get_status_display()

    class Meta:
        ordering = ["-updated_at", "id"]


class ImmutableRevisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("확정 명세는 변경할 수 없습니다.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("확정 명세는 변경할 수 없습니다.")


class RequestRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(WorkRequest, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField()
    source_draft_version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    content_hash = models.CharField(max_length=64)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(auto_now_add=True)
    project_config_version = models.PositiveIntegerField()
    base_commit_sha = models.CharField(max_length=64)
    attachment_snapshot = models.JSONField(default=list)
    message_ids = models.JSONField(default=list)
    objects = ImmutableRevisionQuerySet.as_manager()

    @property
    def project_id(self):
        return self.request.project_id

    @property
    def owner_id(self):
        return self.request.owner_id

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("확정 명세는 변경할 수 없습니다.")
        return super().save(*args, **kwargs)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["request", "number"], name="request_revision_number"),
            models.UniqueConstraint(fields=["request", "source_draft_version"], name="request_revision_draft"),
        ]


class RequestSubmission(models.Model):
    revision = models.OneToOneField(RequestRevision, on_delete=models.PROTECT, related_name="submission")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)
    primary_run = models.OneToOneField("runs.Run", on_delete=models.PROTECT, related_name="primary_submission")
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "idempotency_key"], name="request_submission_key")]


class RequestOperation(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    request = models.ForeignKey(WorkRequest, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    key = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    resulting_draft_version = models.PositiveIntegerField()
    result_id = models.CharField(max_length=36, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "kind", "key"], name="request_operation_key")]


class RevisionSuggestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(WorkRequest, on_delete=models.PROTECT, related_name="suggestions")
    source_draft_version = models.PositiveIntegerField()
    invocation = models.OneToOneField("ai.AIInvocation", on_delete=models.PROTECT)
    proposed_fields = models.JSONField(default=dict)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
