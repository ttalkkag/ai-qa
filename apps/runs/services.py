import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.errors import DomainError
from .models import Job, Run, RunEvent, RunOperation


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
TRANSITIONS = {
    "QUEUED": {"PREPARING", "CANCELLED", "FAILED"},
    "PREPARING": {"RUNNING", "FAILED", "CANCEL_REQUESTED", "INTERRUPTED"},
    "RUNNING": {"VERIFYING", "FAILED", "CANCEL_REQUESTED", "INTERRUPTED"},
    "VERIFYING": {"SUCCEEDED", "FAILED", "CANCEL_REQUESTED", "INTERRUPTED"},
    "CANCEL_REQUESTED": {"CANCELLED", "FAILED", "INTERRUPTED"},
}


@dataclass(frozen=True)
class RunCreation:
    run: Run
    created: bool


def _key(key):
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise DomainError("INVALID_IDEMPOTENCY_KEY")
    return key


def require_execution_space():
    try:
        free = shutil.disk_usage(settings.AIQA_DATA_DIR).free
    except OSError:
        raise DomainError("STORAGE_UNAVAILABLE", "실행 데이터 저장소의 여유 공간을 확인할 수 없습니다.", status=503) from None
    if free < 2 * 1024 ** 3:
        raise DomainError("DISK_SPACE_LOW", "새 작업에는 최소 2 GiB의 여유 공간이 필요합니다. 기존 결과는 삭제하지 않았습니다.", status=507)
    return free


def enqueue_job(*, kind, subject_id, actor_id, project_id=None, payload=None, key=None, slot="EXECUTION"):
    if slot not in Job.Slot.values:
        raise DomainError("INVALID_JOB_SLOT")
    key = _key(key or str(uuid.uuid4()))
    payload = payload or {}
    body = json.dumps({"subject": str(subject_id), "project": str(project_id), "payload": payload, "slot": slot}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()
    with transaction.atomic():
        existing = Job.objects.filter(actor_id=actor_id, kind=kind, idempotency_key=key).first()
        if existing:
            if existing.payload_hash != digest:
                raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
            return existing
        require_execution_space()
        job, created = Job.objects.get_or_create(actor_id=actor_id, kind=kind, idempotency_key=key, defaults={
            "subject_id": str(subject_id), "project_id": project_id, "payload": payload,
            "payload_hash": digest, "slot": slot, "instance_id": settings.AIQA_INSTANCE_ID,
        })
        if not created and job.payload_hash != digest:
            raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
    return job


def _revision(actor, revision_id, action):
    from apps.accounts.services import require_access
    from apps.work_requests.models import RequestRevision

    try:
        revision = RequestRevision.objects.select_related("request__project", "request__owner").get(pk=revision_id)
    except (RequestRevision.DoesNotExist, ValueError, ValidationError):
        raise DomainError("NOT_FOUND", status=404)
    require_access(actor, action, project_id=revision.request.project_id, obj=revision.request)
    if revision.request.owner_id != actor.pk:
        raise DomainError("NOT_FOUND", status=404)
    return revision


def _create(actor, revision, key, previous=None):
    from apps.ai.services import get_profile_snapshot
    from apps.projects.services import resolve_execution_input

    key = _key(key)
    existing = Run.objects.filter(requested_by=actor, idempotency_key=key).first()
    if existing:
        if existing.request_revision_id != revision.pk or existing.previous_run_id != (previous.pk if previous else None):
            raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
        return RunCreation(existing, False)
    if Run.objects.filter(request_revision__request_id=revision.request_id).exclude(status__in=TERMINAL).exists():
        primary = Run.objects.filter(request_revision=revision, previous_run__isnull=True).first()
        if primary and previous is None:
            return RunCreation(primary, False)
        raise DomainError("RUN_ACTIVE", status=409)
    resolve_execution_input(actor, revision.request.project_id, revision.project_config_version, revision.base_commit_sha)
    if previous is None:
        existing = Run.objects.filter(request_revision=revision, previous_run__isnull=True).first()
        if existing:
            return RunCreation(existing, False)
        if revision.request.state != "READY" or revision.request.ready_revision_id != revision.pk:
            raise DomainError("REQUEST_NOT_CONFIRMED", status=409)
    profile = get_profile_snapshot("implementation")
    run = Run.objects.create(
        project_id=revision.request.project_id, request_revision=revision, requested_by=actor,
        baseline_commit_sha=revision.base_commit_sha, project_config_version=revision.project_config_version,
        model_profile_version=profile["version"], profile_snapshot=profile,
        previous_run=previous, idempotency_key=key,
    )
    RunEvent.objects.create(run=run, sequence=1, to_status=run.status, kind="QUEUED", actor=actor, message="수정 요청을 접수했습니다.")
    enqueue_job(kind="RUN", subject_id=run.pk, actor_id=actor.pk, project_id=run.project_id, key=key)
    return RunCreation(run, True)


def create_run(actor, revision_id, key):
    with transaction.atomic():
        return _create(actor, _revision(actor, revision_id, "request.submit"), key)


def _authorized_run(actor, run_id, action):
    from apps.accounts.services import require_access

    try:
        run = Run.objects.select_related("project", "request_revision__request").get(pk=run_id)
    except (Run.DoesNotExist, ValueError, ValidationError):
        raise DomainError("NOT_FOUND", status=404)
    require_access(actor, action, project_id=run.project_id, obj=run)
    return run


def get_run(actor, run_id, after_sequence=0):
    run = _authorized_run(actor, run_id, "run.view")
    try:
        after_sequence = max(0, int(after_sequence))
    except (TypeError, ValueError):
        raise DomainError("INVALID_SEQUENCE")
    now = run.finished_at or timezone.now()
    events = list(run.events.filter(sequence__gt=after_sequence).values("sequence", "to_status", "kind", "message", "created_at")[:100])
    return {
        "id": str(run.pk), "status": run.status, "label": run.get_status_display(),
        "stage": run.current_stage, "state_version": run.state_version,
        "elapsed_seconds": int((now - (run.started_at or run.created_at)).total_seconds()),
        "events": events, "result_bundle_id": str(run.result_bundle_id) if run.result_bundle_id else None,
        "error_code": run.error_code, "message": run.user_message, "terminal": run.status in TERMINAL,
        "is_owner": run.requested_by_id == actor.pk,
    }


def transition_run(run_id, new_status, *, expected_version=None, lease_epoch=None, kind="STATE_CHANGED", actor=None, message="", **fields):
    with transaction.atomic():
        run = Run.objects.get(pk=run_id)
        if expected_version is not None and run.state_version != expected_version:
            raise DomainError("STALE_RUN_STATE", status=409)
        if lease_epoch is not None and run.lease_epoch != lease_epoch:
            raise DomainError("STALE_WORKER", status=409)
        if new_status not in TRANSITIONS.get(run.status, set()):
            raise DomainError("INVALID_RUN_TRANSITION", status=409)
        unfinished = run.processes.filter(ended_at__isnull=True)
        if new_status != "SUCCEEDED":
            unfinished = unfinished.exclude(state="ABANDONED")
        if new_status in TERMINAL and unfinished.exists():
            raise DomainError("PROCESS_TERMINATION_UNCONFIRMED", status=409)
        updates = {"status": new_status, "state_version": run.state_version + 1, "current_stage": new_status, **fields}
        if new_status in TERMINAL:
            updates["finished_at"] = timezone.now()
        if not Run.objects.filter(pk=run.pk, status=run.status, state_version=run.state_version, lease_epoch=run.lease_epoch).update(**updates):
            raise DomainError("STALE_RUN_STATE", status=409)
        sequence = (run.events.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
        RunEvent.objects.create(run=run, sequence=sequence, from_status=run.status, to_status=new_status, kind=kind,
                                actor=actor, worker_identity=run.lease_owner, message=message[:500])
    return Run.objects.get(pk=run_id)


def cancel_run(actor, run_id, key, *, operational=False):
    action = "run.cancel_operational" if operational else "run.cancel"
    with transaction.atomic():
        run = _authorized_run(actor, run_id, action)
        operation = RunOperation.objects.filter(actor=actor, kind="cancel", key=_key(key)).first()
        if operation:
            if operation.run_id != run.pk:
                raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
            return run
        if run.status in TERMINAL:
            raise DomainError("RUN_FINISHED", status=409)
        RunOperation.objects.create(actor=actor, kind="cancel", key=key, run=run)
        if run.status == "QUEUED":
            job = Job.objects.filter(kind="RUN", subject_id=str(run.pk)).first()
            if job and job.state == "QUEUED":
                Job.objects.filter(pk=job.pk, state="QUEUED").update(state="CANCELLED", finished_at=timezone.now())
                return transition_run(run.pk, "CANCELLED", actor=actor, stop_reason="USER_CANCELLED", message="실행 전에 취소했습니다.")
        if run.status != "CANCEL_REQUESTED":
            return transition_run(run.pk, "CANCEL_REQUESTED", actor=actor, stop_reason="USER_CANCELLED", message="프로세스 종료를 확인하고 있습니다.")
        return run


def retry_run(actor, run_id, key):
    with transaction.atomic():
        previous = _authorized_run(actor, run_id, "request.retry")
        revision = _revision(actor, previous.request_revision_id, "request.retry")
        if previous.status not in TERMINAL:
            raise DomainError("RUN_ACTIVE", status=409)
        original_job = Job.objects.filter(kind="RUN", subject_id=str(previous.pk)).first()
        if original_job is None or original_job.instance_id != settings.AIQA_INSTANCE_ID:
            raise DomainError("REQUEST_RECONFIRM_REQUIRED", "복원 이전 명세를 새 기준으로 다시 확인해 주세요.", status=409)
        existing = Run.objects.filter(requested_by=actor, idempotency_key=_key(key)).first()
        if existing:
            if existing.previous_run_id != previous.pk:
                raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
            return RunCreation(existing, False)
        if previous.processes.filter(ended_at__isnull=True).exclude(state="ABANDONED").exists():
            raise DomainError("PROCESS_TERMINATION_UNCONFIRMED", status=409)
        if Run.objects.filter(request_revision__request_id=revision.request_id).exclude(status__in=TERMINAL).exists():
            raise DomainError("RUN_ACTIVE", status=409)
        return _create(actor, revision, key, previous)


def revoke_actor_work(user_id, project_id=None):
    with transaction.atomic():
        runs = Run.objects.filter(requested_by_id=user_id).exclude(status__in=TERMINAL)
        jobs = Job.objects.filter(actor_id=user_id, state__in=["QUEUED", "RUNNING"])
        if project_id is not None:
            runs = runs.filter(project_id=project_id)
            jobs = jobs.filter(project_id=project_id)
        for run in runs:
            if run.status == "QUEUED":
                transition_run(run.pk, "CANCELLED", stop_reason="ACCESS_REVOKED", message="현재 접근 권한이 없어 실행을 취소했습니다.")
            elif run.status != "CANCEL_REQUESTED":
                transition_run(run.pk, "CANCEL_REQUESTED", stop_reason="ACCESS_REVOKED", message="접근 권한 변경으로 실행을 중단합니다.")
        jobs.filter(state="QUEUED").update(state="CANCELLED", error_code="ACCESS_REVOKED", finished_at=timezone.now())
        jobs.filter(state="RUNNING").exclude(kind="RUN").update(cancel_requested_at=timezone.now(), cancel_reason="ACCESS_REVOKED")
        from apps.approvals.services import revoke_actor_publications
        revoke_actor_publications(user_id, project_id)
