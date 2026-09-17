import importlib
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.errors import DomainError
from .models import Job, Run, WorkerSlot
from .processes import confirm_stopped
from .services import TERMINAL, require_execution_space, transition_run


LEASE_SECONDS = 45
HEARTBEAT_SECONDS = 5


def claim_job(slot_name, owner):
    now = timezone.now()
    with transaction.atomic():
        slot, _ = WorkerSlot.objects.get_or_create(name=slot_name)
        if slot.blocked_reason or slot.job_id:
            return None
        job = Job.objects.filter(state="QUEUED", slot=slot_name, instance_id=settings.AIQA_INSTANCE_ID).order_by("queued_at", "id").first()
        if not job:
            return None
        epoch = slot.epoch + 1
        expires = now + timedelta(seconds=LEASE_SECONDS)
        if not WorkerSlot.objects.filter(name=slot_name, epoch=slot.epoch, job__isnull=True, blocked_reason="").update(
            job=job, owner=owner, epoch=epoch, lease_expires_at=expires, heartbeat_at=now,
        ):
            return None
        if not Job.objects.filter(pk=job.pk, state="QUEUED").update(state="RUNNING", lease_owner=owner, lease_epoch=epoch,
                lease_expires_at=expires, heartbeat_at=now, started_at=now):
            raise DomainError("JOB_CLAIM_CONFLICT", status=409)
        if job.kind == "RUN":
            run = Run.objects.get(pk=job.subject_id)
            if run.status != "QUEUED":
                raise DomainError("JOB_CLAIM_CONFLICT", status=409)
            transition_run(run.pk, "PREPARING", lease_owner=owner, lease_epoch=None, started_at=now)
            Run.objects.filter(pk=run.pk).update(lease_epoch=epoch, lease_expires_at=expires, heartbeat_at=now)
    return Job.objects.get(pk=job.pk)


def heartbeat(job):
    now = timezone.now()
    expires = now + timedelta(seconds=LEASE_SECONDS)
    with transaction.atomic():
        if not WorkerSlot.objects.filter(name=job.slot, job=job, owner=job.lease_owner, epoch=job.lease_epoch,
                                         lease_expires_at__gt=now, blocked_reason="").update(heartbeat_at=now, lease_expires_at=expires):
            raise DomainError("STALE_WORKER", status=409)
        Job.objects.filter(pk=job.pk, lease_owner=job.lease_owner, lease_epoch=job.lease_epoch).update(heartbeat_at=now, lease_expires_at=expires)
        if job.kind == "RUN":
            Run.objects.filter(pk=job.subject_id, lease_owner=job.lease_owner, lease_epoch=job.lease_epoch).exclude(status__in=TERMINAL).update(heartbeat_at=now, lease_expires_at=expires)


def abandon_local_processes(job):
    if job.kind.startswith("publication."):
        return False
    job.processes.filter(ended_at__isnull=True).update(state="ABANDONED")
    WorkerSlot.objects.filter(pk=job.slot, job=job).update(blocked_reason="")
    return True


def recover_stale():
    now = timezone.now()
    for slot in WorkerSlot.objects.exclude(job__isnull=True):
        if slot.lease_expires_at and slot.lease_expires_at > now:
            continue
        job = slot.job
        records = list(job.processes.filter(ended_at__isnull=True))
        unconfirmed = False
        for record in records:
            if record.container_name:
                from integrations.codex.container import remove
                try:
                    remove(record.container_name)
                except DomainError:
                    unconfirmed = True
                    continue
            if not confirm_stopped(record):
                unconfirmed = True
        if unconfirmed:
            if not abandon_local_processes(job):
                WorkerSlot.objects.filter(pk=slot.pk).update(blocked_reason="EXECUTION_BLOCKED")
                continue
        with transaction.atomic():
            if job.kind.startswith("publication."):
                from apps.approvals.services import mark_job_interrupted
                mark_job_interrupted(job, "WORKER_LOST")
            if job.kind == "RUN":
                run = Run.objects.get(pk=job.subject_id)
                if run.status not in TERMINAL:
                    transition_run(run.pk, "INTERRUPTED", stop_reason="WORKER_LOST", message="작업자가 중단되었습니다. 자동 재실행하지 않습니다.")
            Job.objects.filter(pk=job.pk).update(state="FAILED", error_code="WORKER_LOST", finished_at=now)
            if job.kind in {"CONVERSATION", "CLARIFICATION", "MODEL_VALIDATION", "SANDBOX_CHECK"}:
                from apps.ai.models import AIInvocation
                AIInvocation.objects.filter(pk=job.subject_id, state__in=["QUEUED", "RUNNING"]).update(
                    state="FAILED", error_code="WORKER_LOST", ended_at=now)
            WorkerSlot.objects.filter(pk=slot.pk, job=job, epoch=slot.epoch).update(job=None, owner="", lease_expires_at=None, blocked_reason="")


def _dispatch(job):
    if job.kind == "RUN":
        from .pipeline import execute_run
        return execute_run(job)
    if job.kind in {"CONVERSATION", "CLARIFICATION", "MODEL_VALIDATION", "SANDBOX_CHECK"}:
        return importlib.import_module("apps.ai.services").process_job(job)
    if job.kind.startswith("publication."):
        return importlib.import_module("apps.approvals.services").process_job(job)
    raise DomainError("UNKNOWN_JOB_KIND")


def process_claimed(job):
    stop = threading.Event()
    heartbeat_errors = []

    def beat():
        close_old_connections()
        try:
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    heartbeat(job)
                except Exception:
                    heartbeat_errors.append(True)
                    return
        finally:
            close_old_connections()

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    error_code = ""
    try:
        require_execution_space()
        _dispatch(job)
    except DomainError as exc:
        error_code = exc.code
    except Exception:
        error_code = "JOB_FAILED"
    finally:
        stop.set()
        thread.join(timeout=HEARTBEAT_SECONDS + 1)
    if not Job.objects.filter(pk=job.pk, state="RUNNING", lease_epoch=job.lease_epoch, lease_owner=job.lease_owner).exists():
        return
    if job.processes.filter(ended_at__isnull=True).exists():
        if not abandon_local_processes(job):
            WorkerSlot.objects.filter(pk=job.slot).update(blocked_reason="EXECUTION_BLOCKED")
            return
        error_code = error_code or "PROCESS_TERMINATION_UNCONFIRMED"
    if heartbeat_errors:
        error_code = "WORKER_LEASE_LOST"
    if error_code and job.kind.startswith("publication."):
        from apps.approvals.services import mark_job_interrupted
        mark_job_interrupted(job, error_code)
    if error_code and job.kind in {"CONVERSATION", "CLARIFICATION", "MODEL_VALIDATION", "SANDBOX_CHECK"}:
        from apps.ai.models import AIInvocation
        AIInvocation.objects.filter(pk=job.subject_id, state__in=["QUEUED", "RUNNING"]).update(state="FAILED", error_code=error_code, ended_at=timezone.now())
    with transaction.atomic():
        if job.kind == "RUN":
            run = Run.objects.get(pk=job.subject_id)
            if run.status not in TERMINAL:
                if run.status == "CANCEL_REQUESTED":
                    status = "CANCELLED" if run.stop_reason in {"USER_CANCELLED", "ACCESS_REVOKED"} else "FAILED"
                else:
                    status = "FAILED"
                transition_run(run.pk, status, lease_epoch=job.lease_epoch, error_code=error_code or run.stop_reason,
                               user_message="실행이 완료되지 않았습니다. 현재 상태와 실행 환경을 확인해 주세요.")
            if run.status == "FAILED" and not error_code:
                error_code = run.error_code or "RUN_FAILED"
        current = Job.objects.get(pk=job.pk)
        state = "CANCELLED" if current.cancel_requested_at else "FAILED" if error_code else "DONE"
        Job.objects.filter(pk=job.pk, lease_epoch=job.lease_epoch).exclude(state="CANCELLED").update(
            state=state, error_code=error_code or current.cancel_reason, finished_at=timezone.now())
        WorkerSlot.objects.filter(pk=job.slot, job=job, owner=job.lease_owner, epoch=job.lease_epoch).update(job=None, owner="", lease_expires_at=None)


def worker_loop(owner, stop, once=False):
    recover_stale()
    active = {}
    next_idle_heartbeat = 0

    def run_job(job):
        close_old_connections()
        try:
            process_claimed(job)
        finally:
            close_old_connections()

    while not stop.is_set():
        if time.monotonic() >= next_idle_heartbeat:
            now = timezone.now()
            for name in Job.Slot.values:
                WorkerSlot.objects.get_or_create(name=name)
            WorkerSlot.objects.filter(job__isnull=True).update(owner=owner, heartbeat_at=now, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
            next_idle_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
        for slot in Job.Slot.values:
            thread = active.get(slot)
            if thread and thread.is_alive():
                continue
            job = claim_job(slot, owner)
            if job:
                thread = threading.Thread(target=run_job, args=(job,), daemon=False)
                active[slot] = thread
                thread.start()
        if once:
            for thread in active.values():
                thread.join()
            return
        stop.wait(0.5)
    for thread in active.values():
        thread.join()
