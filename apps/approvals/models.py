import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class PublicationPolicy(models.Model):
    class Mode(models.TextChoices):
        PUSH = "PUSH", "새 브랜치"
        PUSH_AND_PR = "PUSH_AND_PR", "새 브랜치와 Draft PR"

    project = models.OneToOneField("projects.Project", on_delete=models.PROTECT, related_name="publication_policy")
    version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=False)
    repository_host = models.CharField(max_length=80, default="github.com")
    repository_id = models.CharField(max_length=64, blank=True)
    repository_full_name = models.CharField(max_length=200, blank=True)
    remote_url = models.URLField(blank=True)
    base_branch = models.CharField(max_length=200, default="main")
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.PUSH_AND_PR)
    commit_author_name = models.CharField(max_length=100, blank=True)
    commit_author_email = models.EmailField(blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)


class PublicationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs) & {"snapshot", "snapshot_hash", "run", "run_id", "bundle", "bundle_id", "project", "project_id", "policy", "policy_id", "created_by", "created_by_id"}:
            raise ValidationError("발행 승인 대상은 변경할 수 없습니다.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("발행 이력은 삭제할 수 없습니다.")


class Publication(models.Model):
    class State(models.TextChoices):
        PENDING_APPROVAL = "PENDING_APPROVAL", "승인 대기"
        APPROVED = "APPROVED", "승인됨"
        PUBLISHING = "PUBLISHING", "발행 중"
        PUBLISHED = "PUBLISHED", "발행 확인"
        REJECTED = "REJECTED", "거절"
        INVALIDATED = "INVALIDATED", "승인 무효"
        FAILED = "FAILED", "원격 변경 없는 실패"
        UNKNOWN = "UNKNOWN", "원격 결과 확인 필요"
        PARTIAL = "PARTIAL", "부분 발행"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    run = models.ForeignKey("runs.Run", on_delete=models.PROTECT, related_name="publications")
    bundle = models.ForeignKey("artifacts.ResultBundle", on_delete=models.PROTECT)
    policy = models.ForeignKey(PublicationPolicy, on_delete=models.PROTECT)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING_APPROVAL)
    state_version = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField()
    snapshot_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="prepared_publications")
    remote_branch_sha = models.CharField(max_length=64, blank=True)
    remote_effects_observed = models.BooleanField(default=False)
    remote_pr_number = models.PositiveIntegerField(null=True)
    remote_pr_url = models.URLField(blank=True)
    last_observed_at = models.DateTimeField(null=True)
    last_observed_base_sha = models.CharField(max_length=64, blank=True)
    reconciled_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=64, blank=True)
    abandoned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="abandoned_publications")
    abandoned_at = models.DateTimeField(null=True)
    abandonment_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = PublicationQuerySet.as_manager()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["run"],
            condition=models.Q(state__in=["PENDING_APPROVAL", "APPROVED", "PUBLISHING", "PUBLISHED", "FAILED", "UNKNOWN", "PARTIAL"], abandoned_at__isnull=True),
            name="publication_one_live_target_per_run",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            for field in ("snapshot", "snapshot_hash", "run_id", "bundle_id", "project_id", "policy_id", "created_by_id"):
                if getattr(self, field) != getattr(previous, field):
                    raise ValidationError("발행 승인 대상은 변경할 수 없습니다.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("발행 이력은 삭제할 수 없습니다.")


class ApprovalQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs) - {"valid", "invalid_reason"}:
            raise ValidationError("승인 결정은 변경할 수 없습니다.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("승인 이력은 삭제할 수 없습니다.")


class Approval(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="approvals")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decision = models.CharField(max_length=8, choices=[("APPROVE", "승인"), ("REJECT", "거절")])
    snapshot_hash = models.CharField(max_length=64)
    reason = models.CharField(max_length=500, blank=True)
    scope = models.CharField(max_length=16, default="FULL", choices=[("FULL", "전체"), ("REMAINING_PR", "남은 PR")])
    valid = models.BooleanField(default=True)
    invalid_reason = models.CharField(max_length=64, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)
    objects = ApprovalQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            for field in ("publication_id", "actor_id", "decision", "snapshot_hash", "reason", "scope", "decided_at"):
                if getattr(old, field) != getattr(self, field):
                    raise ValidationError("승인 결정은 변경할 수 없습니다.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("승인 이력은 삭제할 수 없습니다.")


class PublicationAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, related_name="attempts")
    approval = models.ForeignKey(Approval, on_delete=models.PROTECT, null=True)
    job = models.ForeignKey("runs.Job", on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    stage = models.CharField(max_length=32)
    requested_commit_sha = models.CharField(max_length=64, blank=True)
    confirmed_commit_sha = models.CharField(max_length=64, blank=True)
    evidence = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["publication", "number"], name="publication_attempt_number_unique")]


class PublicationAction(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.CharField(max_length=128)
    operation = models.CharField(max_length=32)
    payload_hash = models.CharField(max_length=64)
    publication = models.ForeignKey(Publication, on_delete=models.PROTECT, null=True)
    job = models.ForeignKey("runs.Job", on_delete=models.PROTECT, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "key"], name="publication_actor_action_key_unique")]
