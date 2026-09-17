import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Run(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "대기"
        PREPARING = "PREPARING", "준비"
        RUNNING = "RUNNING", "수정"
        VERIFYING = "VERIFYING", "검증"
        CANCEL_REQUESTED = "CANCEL_REQUESTED", "종료 확인 중"
        SUCCEEDED = "SUCCEEDED", "로컬 결과 완료"
        FAILED = "FAILED", "실패"
        CANCELLED = "CANCELLED", "취소됨"
        INTERRUPTED = "INTERRUPTED", "중단됨"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    request_revision = models.ForeignKey("work_requests.RequestRevision", on_delete=models.PROTECT, related_name="runs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    state_version = models.PositiveIntegerField(default=1)
    baseline_commit_sha = models.CharField(max_length=64)
    snapshot_base_commit_sha = models.CharField(max_length=64, blank=True)
    result_branch = models.CharField(max_length=100, blank=True)
    result_commit_sha = models.CharField(max_length=64, blank=True)
    project_config_version = models.PositiveIntegerField()
    model_profile_version = models.PositiveIntegerField()
    profile_snapshot = models.JSONField(default=dict)
    previous_run = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="retries")
    idempotency_key = models.CharField(max_length=128)
    workspace_id = models.CharField(max_length=36, blank=True)
    current_stage = models.CharField(max_length=64, default="QUEUED")
    lease_owner = models.CharField(max_length=64, blank=True)
    lease_epoch = models.PositiveIntegerField(default=0)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    stop_reason = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    user_message = models.TextField(blank=True)
    result_bundle_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["requested_by", "idempotency_key"], name="run_actor_key"),
            models.UniqueConstraint(fields=["request_revision"], condition=Q(previous_run__isnull=True), name="run_primary_revision"),
        ]

    @property
    def owner_id(self):
        return self.requested_by_id


class Job(models.Model):
    class State(models.TextChoices):
        QUEUED = "QUEUED", "대기"
        RUNNING = "RUNNING", "진행"
        DONE = "DONE", "단계 완료"
        FAILED = "FAILED", "단계 실패"
        CANCELLED = "CANCELLED", "취소"

    class Slot(models.TextChoices):
        CONVERSATION = "CONVERSATION", "대화"
        EXECUTION = "EXECUTION", "실행"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=48)
    subject_id = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, null=True, blank=True)
    payload = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    slot = models.CharField(max_length=16, choices=Slot.choices, default=Slot.EXECUTION)
    instance_id = models.CharField(max_length=64)
    lease_owner = models.CharField(max_length=64, blank=True)
    lease_epoch = models.PositiveIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=64, blank=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["queued_at", "id"]
        constraints = [models.UniqueConstraint(fields=["actor", "kind", "idempotency_key"], name="job_actor_kind_key")]


class WorkerSlot(models.Model):
    name = models.CharField(max_length=16, primary_key=True, choices=Job.Slot.choices)
    job = models.OneToOneField(Job, null=True, blank=True, on_delete=models.PROTECT, related_name="worker_slot")
    owner = models.CharField(max_length=64, blank=True)
    epoch = models.PositiveIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=64, blank=True)


class ProcessRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="processes")
    operation_id = models.UUIDField(default=uuid.uuid4, unique=True)
    run = models.ForeignKey(Run, null=True, blank=True, on_delete=models.PROTECT, related_name="processes")
    stage = models.CharField(max_length=64)
    container_name = models.CharField(max_length=80, blank=True)
    pid = models.PositiveIntegerField(null=True, blank=True)
    process_start_identity = models.CharField(max_length=160, blank=True)
    process_group_id = models.PositiveIntegerField(null=True, blank=True)
    worker_identity = models.CharField(max_length=64)
    instance_id = models.CharField(max_length=64)
    host_boot_id = models.CharField(max_length=160)
    descendants = models.JSONField(default=dict)
    state = models.CharField(max_length=16, default="RESERVED")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    exit_code = models.IntegerField(null=True, blank=True)


class CheckResult(models.Model):
    run = models.ForeignKey(Run, on_delete=models.PROTECT, related_name="checks")
    phase = models.CharField(max_length=16)
    check_id = models.CharField(max_length=64)
    config_version = models.PositiveIntegerField()
    argv = models.JSONField()
    cwd = models.TextField(default=".")
    environment_profile_id = models.CharField(max_length=64, blank=True)
    required = models.BooleanField(default=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    duration = models.FloatField(default=0)
    exit_code = models.IntegerField(null=True)
    checked_source_tree_sha256 = models.CharField(max_length=64)
    outcome = models.CharField(max_length=16)
    parser_id = models.CharField(max_length=32, blank=True)
    parser_version = models.PositiveIntegerField(null=True)
    report_path = models.TextField(blank=True)
    report_sha256 = models.CharField(max_length=64, blank=True)
    collected_count = models.PositiveIntegerField(null=True)
    passed_count = models.PositiveIntegerField(null=True)
    failed_count = models.PositiveIntegerField(null=True)
    skipped_count = models.PositiveIntegerField(null=True)
    output_complete = models.BooleanField(default=False)
    log_artifact_id = models.UUIDField(null=True)
    reason_code = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "phase", "check_id"], name="run_check_phase")]


class RunEvent(models.Model):
    run = models.ForeignKey(Run, on_delete=models.PROTECT, related_name="events")
    sequence = models.PositiveIntegerField()
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24)
    kind = models.CharField(max_length=32)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    worker_identity = models.CharField(max_length=64, blank=True)
    message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["run", "sequence"], name="run_event_sequence")]


class RunOperation(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    run = models.ForeignKey(Run, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    key = models.CharField(max_length=128)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "kind", "key"], name="run_operation_key")]
