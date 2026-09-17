import hashlib
import json
import re
import uuid
from functools import wraps

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F, Max
from django.utils import timezone

from apps.accounts.services import require_access
from apps.errors import DomainError

from .models import Approval, Publication, PublicationAction, PublicationAttempt, PublicationPolicy


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _atomic(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            with transaction.atomic():
                return function(*args, **kwargs)
        except (IntegrityError, OperationalError):
            raise DomainError("STATE_CONFLICT", status=409) from None
    return call


def _get(model, pk):
    try:
        return model.objects.get(pk=pk)
    except (model.DoesNotExist, ValidationError, ValueError, TypeError):
        raise DomainError("NOT_FOUND", status=404) from None


def _receipt(actor, key, operation, payload):
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise DomainError("INVALID_IDEMPOTENCY_KEY")
    digest = _hash(payload)
    previous = PublicationAction.objects.filter(actor=actor, key=key).first()
    if previous and (previous.operation != operation or previous.payload_hash != digest):
        raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
    return previous, digest


def _record(actor, key, operation, digest, publication=None, job=None):
    return PublicationAction.objects.create(actor=actor, key=key, operation=operation, payload_hash=digest, publication=publication, job=job)


def _active_project(project):
    project.refresh_from_db()
    if project.state != "ACTIVE":
        raise DomainError("PROJECT_ARCHIVED", status=409)


def _policy_data(policy):
    return {field: getattr(policy, field) for field in (
        "version", "repository_host", "repository_id", "repository_full_name", "remote_url", "base_branch", "mode", "commit_author_name", "commit_author_email",
    )}


def _validate_policy(data):
    name = data.get("repository_full_name", "")
    branch = data.get("base_branch", "")
    if data.get("repository_host") != "github.com" or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise DomainError("INVALID_PUBLICATION_TARGET")
    if data.get("remote_url") != f"https://github.com/{name}.git":
        raise DomainError("INVALID_PUBLICATION_TARGET")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(data.get("repository_id", ""))):
        raise DomainError("INVALID_PUBLICATION_TARGET")
    if (not branch or len(branch) > 200 or branch.startswith(("-", "/")) or branch.endswith(("/", ".", ".lock"))
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
            or re.search(r"[\s~^:?*\[\\\x00-\x1f\x7f]", branch) or any(v in branch for v in ("..", "@{", "//")) or branch == "@"):
        raise DomainError("INVALID_BASE_BRANCH")
    if data.get("mode") not in PublicationPolicy.Mode.values:
        raise DomainError("INVALID_PUBLICATION_MODE")
    author = data.get("commit_author_name", "")
    if not author.strip() or len(author) > 100 or re.search(r"[<>\r\n\x00]", author):
        raise DomainError("INVALID_COMMIT_AUTHOR")
    try:
        validate_email(data.get("commit_author_email", ""))
    except ValidationError:
        raise DomainError("INVALID_COMMIT_AUTHOR") from None


@_atomic
def configure_policy(actor, project_id, expected_version, **values):
    from apps.projects.models import Project
    from apps.runs.models import Job

    actor = require_access(actor, "project.manage", project_id=project_id)
    project = _get(Project, project_id)
    _active_project(project)
    if Job.objects.filter(project=project, kind__in=["publication.prepare", "publication.execute"], state__in=["QUEUED", "RUNNING"]).exists():
        raise DomainError("PROJECT_BUSY", status=409)
    allowed = {"enabled", "repository_host", "repository_id", "repository_full_name", "remote_url", "base_branch", "mode", "commit_author_name", "commit_author_email"}
    if set(values) - allowed or not isinstance(values.get("enabled", False), bool):
        raise DomainError("INVALID_INPUT")
    policy = PublicationPolicy.objects.filter(project=project).first()
    current = policy.version if policy else 0
    if current != expected_version:
        raise DomainError("POLICY_CHANGED", status=409)
    if policy is None:
        policy = PublicationPolicy(project=project, updated_by=actor)
    for field, value in values.items():
        setattr(policy, field, value)
    if policy.enabled:
        _validate_policy(_policy_data(policy))
    policy.version = current + 1
    policy.updated_by = actor
    policy.save()
    for publication in Publication.objects.filter(project=project, abandoned_at__isnull=True).exclude(state__in=["PUBLISHED", "REJECTED", "INVALIDATED"]):
        _invalidate(publication, "POLICY_CHANGED")
    return policy


def _conditions(actor, run, bundle=None, snapshot=None):
    from apps.ai.services import require_sandbox_ready
    from apps.artifacts.services import verify_bundle
    from apps.artifacts.models import ResultBundle
    from apps.projects.services import resolve_base_sha, validate_repository_path

    actor = require_access(actor, "publication.publish", project_id=run.project_id)
    run.refresh_from_db()
    project = run.project
    _active_project(project)
    if run.status != "SUCCEEDED" or not run.result_bundle_id:
        raise DomainError("RESULT_NOT_PUBLISHABLE", status=409)
    bundle = bundle or _get(ResultBundle, run.result_bundle_id)
    if bundle.run_id != run.id or bundle.id != run.result_bundle_id:
        raise DomainError("RESULT_CHANGED", status=409)
    expected = snapshot["manifest_sha256"] if snapshot else bundle.manifest_sha256
    bundle = verify_bundle(bundle.id, expected)
    if bundle.completeness != "COMPLETE" or bundle.visibility != "NORMAL" or bundle.corruption_reason:
        raise DomainError("RESULT_NOT_PUBLISHABLE", status=409)
    from apps.artifacts.models import ResultReview
    from apps.artifacts.reviews import is_current_result
    if not ResultReview.objects.filter(bundle=bundle, state="APPROVED", manifest_sha256=bundle.manifest_sha256).exists() or not is_current_result(bundle, bundle.request_revision.request):
        raise DomainError("RESULT_REVIEW_REQUIRED", "사용자 결과 확인과 관리자 로컬 승인을 먼저 완료해 주세요", status=409)
    if bundle.baseline_commit_sha != run.baseline_commit_sha or bundle.project_config_version != project.project_config_version or run.project_config_version != project.project_config_version:
        raise DomainError("PROJECT_CONFIG_CHANGED", status=409)
    source_root = validate_repository_path(project.canonical_repo_path)
    if resolve_base_sha(source_root, project.current_revision.source_ref) != bundle.baseline_commit_sha:
        raise DomainError("BASE_CHANGED", status=409)
    required = list(project.current_revision.verification_commands.filter(required=True))
    checks = {item.check_id: item for item in run.checks.filter(phase="AFTER")}
    if not required:
        raise DomainError("CHECKS_INCOMPLETE", status=409)
    for command in required:
        check = checks.get(command.command_id)
        if (check is None or check.config_version != run.project_config_version or check.outcome != "PASS"
                or check.exit_code != 0 or not check.output_complete or check.checked_source_tree_sha256 != bundle.result_tree_sha256):
            raise DomainError("CHECKS_INCOMPLETE", status=409)
        if command.kind == "TEST" and (not check.passed_count or check.failed_count != 0):
            raise DomainError("CHECKS_INCOMPLETE", status=409)
    policy = PublicationPolicy.objects.filter(project=project, enabled=True).first()
    if policy is None:
        raise DomainError("PUBLICATION_DISABLED", status=409)
    _validate_policy(_policy_data(policy))
    if snapshot:
        if (snapshot["run_id"] != str(run.id) or snapshot["project_id"] != str(project.id) or snapshot["bundle_id"] != str(bundle.id)
                or snapshot["policy"] != _policy_data(policy) or snapshot["policy_id"] != policy.id
                or snapshot["bundle_version"] != bundle.version or snapshot["project_config_version"] != project.project_config_version
                or snapshot["baseline_commit_sha"] != bundle.baseline_commit_sha or snapshot["result_tree_sha256"] != bundle.result_tree_sha256):
            raise DomainError("SNAPSHOT_CHANGED", status=409)
    require_sandbox_ready(project)
    return actor, bundle, policy


def _enqueue(actor, kind, subject_id, project_id, payload, key):
    from apps.runs.services import enqueue_job
    return enqueue_job(kind=kind, subject_id=str(subject_id), actor_id=actor.pk, project_id=project_id, payload=payload, key=key, slot="EXECUTION")


@_atomic
def prepare_publication(actor, run_id, idempotency_key):
    from apps.runs.models import Run

    actor = require_access(actor, "publication.publish")
    previous, digest = _receipt(actor, idempotency_key, "prepare", {"run_id": str(run_id)})
    if previous:
        return previous.job
    run = _get(Run, run_id)
    _conditions(actor, run)
    if Publication.objects.filter(run=run, abandoned_at__isnull=True).exclude(state__in=["INVALIDATED", "REJECTED"]).exists() or Publication.objects.filter(run=run, remote_branch_sha__gt="").exists():
        raise DomainError("PUBLICATION_EXISTS", status=409)
    job = _enqueue(actor, "publication.prepare", run.id, run.project_id, {}, f"publication-prepare:{_hash([str(actor.pk), idempotency_key])}")
    _record(actor, idempotency_key, "prepare", digest, job=job)
    return job


def _transition(publication, state, **values):
    changed = Publication.objects.filter(pk=publication.pk, state=publication.state, state_version=publication.state_version).update(
        state=state, state_version=F("state_version") + 1, updated_at=timezone.now(), **values,
    )
    if changed != 1:
        raise DomainError("STATE_CONFLICT", status=409)
    publication.refresh_from_db()


def _unpin(publication):
    from apps.artifacts.services import unpin_bundle
    unpin_bundle(publication.bundle_id, f"publication:{publication.id}")


def _invalidate(publication, code):
    Approval.objects.filter(publication=publication, valid=True).update(valid=False, invalid_reason=code)
    if publication.state == "UNKNOWN":
        _transition(publication, "UNKNOWN", error_code=code)
    elif publication.remote_effects_observed or publication.remote_branch_sha or publication.remote_pr_number:
        _transition(publication, "PARTIAL", error_code=code)
    else:
        _transition(publication, "INVALIDATED", error_code=code)
        _unpin(publication)


@_atomic
def decide_publication(actor, publication_id, snapshot_hash, decision, idempotency_key, reason=""):
    actor = require_access(actor, "publication.approve" if decision == "APPROVE" else "publication.reject")
    if decision not in {"APPROVE", "REJECT"} or not isinstance(reason, str) or len(reason) > 500:
        raise DomainError("INVALID_INPUT")
    previous, digest = _receipt(actor, idempotency_key, "decide", {"publication_id": str(publication_id), "snapshot_hash": snapshot_hash, "decision": decision, "reason": reason})
    if previous:
        return previous.publication
    publication = _get(Publication, publication_id)
    if publication.snapshot_hash != snapshot_hash or _hash(publication.snapshot) != snapshot_hash:
        raise DomainError("SNAPSHOT_CHANGED", status=409)
    if publication.state != "PENDING_APPROVAL" or publication.abandoned_at:
        raise DomainError("STATE_CONFLICT", status=409)
    if decision == "APPROVE":
        _conditions(actor, publication.run, publication.bundle, publication.snapshot)
    _transition(publication, "APPROVED" if decision == "APPROVE" else "REJECTED")
    approval = Approval.objects.create(publication=publication, actor=actor, decision=decision, snapshot_hash=snapshot_hash, reason=reason)
    job = None
    if decision == "APPROVE":
        job = _enqueue(actor, "publication.execute", publication.id, publication.project_id, {"approval_id": str(approval.id)}, f"publication-approval:{approval.id}")
    else:
        _unpin(publication)
    _record(actor, idempotency_key, "decide", digest, publication, job)
    return publication


@_atomic
def reconcile_publication(actor, publication_id, idempotency_key=None):
    actor = require_access(actor, "publication.publish")
    publication = _get(Publication, publication_id)
    if publication.abandoned_at or publication.state in {"PENDING_APPROVAL", "APPROVED", "PUBLISHING"}:
        raise DomainError("STATE_CONFLICT", status=409)
    key = idempotency_key or str(uuid.uuid4())
    previous, digest = _receipt(actor, key, "reconcile", {"publication_id": str(publication.id)})
    if previous:
        return previous.job
    _ensure_no_live_jobs(publication)
    job = _enqueue(actor, "publication.reconcile", publication.id, publication.project_id, {}, f"publication-reconcile:{key}")
    _record(actor, key, "reconcile", digest, publication, job)
    return job


def _ensure_no_live_jobs(publication):
    from apps.runs.models import Job, ProcessRecord
    jobs = Job.objects.filter(subject_id=str(publication.id), kind__startswith="publication.")
    if jobs.filter(state__in=["QUEUED", "RUNNING"]).exists() or ProcessRecord.objects.filter(job__in=jobs, ended_at__isnull=True).exists():
        raise DomainError("PROCESS_STATE_UNKNOWN", status=409)


@_atomic
def approve_remaining(actor, publication_id, snapshot_hash, idempotency_key):
    return _approve_recovery(actor, publication_id, snapshot_hash, idempotency_key, remaining=True)


@_atomic
def retry_publication(actor, publication_id, snapshot_hash, idempotency_key):
    return _approve_recovery(actor, publication_id, snapshot_hash, idempotency_key, remaining=False)


def _approve_recovery(actor, publication_id, snapshot_hash, key, remaining):
    actor = require_access(actor, "publication.approve")
    operation = "remaining" if remaining else "retry"
    previous, digest = _receipt(actor, key, operation, {"publication_id": str(publication_id), "snapshot_hash": snapshot_hash})
    if previous:
        return previous.publication
    publication = _get(Publication, publication_id)
    _ensure_no_live_jobs(publication)
    if (publication.state != ("PARTIAL" if remaining else "FAILED") or not publication.reconciled_at or publication.abandoned_at
            or publication.snapshot_hash != snapshot_hash or _hash(publication.snapshot) != snapshot_hash):
        raise DomainError("RECOVERY_NOT_READY", status=409)
    last_reconcile = publication.attempts.filter(stage="RECONCILE", finished_at__isnull=False).order_by("-number").first()
    if not last_reconcile or last_reconcile.error_code or last_reconcile.evidence.get("base_sha") != publication.snapshot["baseline_commit_sha"]:
        raise DomainError("RECOVERY_NOT_READY", status=409)
    if remaining and (publication.snapshot["policy"]["mode"] != "PUSH_AND_PR" or publication.remote_branch_sha != publication.snapshot["commit_sha"] or publication.remote_pr_number):
        raise DomainError("RECOVERY_NOT_READY", status=409)
    if last_reconcile.confirmed_commit_sha != (publication.snapshot["commit_sha"] if remaining else ""):
        raise DomainError("RECOVERY_NOT_READY", status=409)
    _conditions(actor, publication.run, publication.bundle, publication.snapshot)
    _transition(publication, "APPROVED", error_code="")
    Approval.objects.filter(publication=publication, valid=True).update(valid=False, invalid_reason="REAPPROVED")
    approval = Approval.objects.create(publication=publication, actor=actor, decision="APPROVE", snapshot_hash=snapshot_hash, scope="REMAINING_PR" if remaining else "FULL")
    job = _enqueue(actor, "publication.execute", publication.id, publication.project_id, {"approval_id": str(approval.id)}, f"publication-approval:{approval.id}")
    _record(actor, key, operation, digest, publication, job)
    return publication


@_atomic
def abandon_remaining(actor, publication_id, snapshot_hash, reason, idempotency_key):
    actor = require_access(actor, "publication.reject")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise DomainError("REASON_REQUIRED")
    previous, digest = _receipt(actor, idempotency_key, "abandon", {"publication_id": str(publication_id), "snapshot_hash": snapshot_hash, "reason": reason})
    if previous:
        return previous.publication
    publication = _get(Publication, publication_id)
    _ensure_no_live_jobs(publication)
    if publication.state not in {"PARTIAL", "FAILED", "INVALIDATED"} or not publication.reconciled_at or publication.snapshot_hash != snapshot_hash or publication.abandoned_at:
        raise DomainError("RECOVERY_NOT_READY", status=409)
    _transition(publication, publication.state, abandoned_by=actor, abandoned_at=timezone.now(), abandonment_reason=reason.strip())
    Approval.objects.filter(publication=publication, valid=True).update(valid=False, invalid_reason="ABANDONED")
    _unpin(publication)
    _record(actor, idempotency_key, "abandon", digest, publication)
    return publication


def get_publication(actor, publication_id):
    publication = _get(Publication, publication_id)
    require_access(actor, "artifact.view", obj=publication.run)
    return publication


def _check_observation(snapshot, observed, require_base=True):
    policy = snapshot["policy"]
    if (observed.repository_id != policy["repository_id"] or observed.repository_full_name != policy["repository_full_name"]):
        raise DomainError("REMOTE_TARGET_CHANGED", status=409)
    if require_base and observed.base_sha != snapshot["baseline_commit_sha"]:
        raise DomainError("BASE_CHANGED", status=409)
    if observed.branch_sha and observed.branch_sha != snapshot["commit_sha"]:
        raise DomainError("REMOTE_REF_CONFLICT", status=409)
    if observed.pr_number and (observed.pr_head_sha != snapshot["commit_sha"] or not observed.pr_draft):
        raise DomainError("REMOTE_PR_CONFLICT", status=409)


def _remember(publication, observed):
    publication.remote_branch_sha = observed.branch_sha or publication.remote_branch_sha
    publication.remote_pr_number = observed.pr_number or publication.remote_pr_number
    publication.remote_pr_url = observed.pr_url or publication.remote_pr_url
    publication.remote_effects_observed = publication.remote_effects_observed or bool(observed.branch_sha or observed.pr_number)
    publication.last_observed_base_sha = observed.base_sha
    publication.last_observed_at = timezone.now()
    publication.save(update_fields=["remote_branch_sha", "remote_pr_number", "remote_pr_url", "remote_effects_observed", "last_observed_base_sha", "last_observed_at", "updated_at"])


def _attempt(publication, approval, job, stage):
    number = (publication.attempts.aggregate(value=Max("number"))["value"] or 0) + 1
    return PublicationAttempt.objects.create(publication=publication, approval=approval, job=job, number=number, stage=stage, requested_commit_sha=publication.snapshot["commit_sha"])


def _current_approval(publication, approval):
    approval.refresh_from_db()
    publication.refresh_from_db()
    if publication.state not in {"APPROVED", "PUBLISHING", "PARTIAL", "UNKNOWN", "PUBLISHED"} or publication.abandoned_at or not approval.valid or approval.decision != "APPROVE" or approval.snapshot_hash != publication.snapshot_hash or _hash(publication.snapshot) != publication.snapshot_hash:
        raise DomainError("APPROVAL_INVALID", status=409)
    _conditions(approval.actor, publication.run, publication.bundle, publication.snapshot)


def _prepare_job(job, transport):
    from apps.artifacts.services import pin_bundle
    from apps.runs.models import Run

    run = _get(Run, job.subject_id)
    actor, bundle, policy = _conditions(job.actor, run)
    created = timezone.now().replace(microsecond=0)
    snapshot = {
        "run_id": str(run.id), "project_id": str(run.project_id), "bundle_id": str(bundle.id), "bundle_version": bundle.version,
        "manifest_sha256": bundle.manifest_sha256, "baseline_commit_sha": bundle.baseline_commit_sha,
        "project_config_version": bundle.project_config_version, "result_tree_sha256": bundle.result_tree_sha256,
        "policy_id": policy.id, "policy": _policy_data(policy), "branch": f"aiqa/run-{run.id}",
        "commit_timestamp": created.isoformat(), "commit_message": f"AI QA result {run.id}",
    }
    snapshot["commit_sha"] = transport.prepare(snapshot, bundle, job)
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", snapshot["commit_sha"]):
        raise DomainError("INVALID_PREPARED_COMMIT")
    observed = transport.observe(snapshot, job)
    _check_observation(snapshot, observed)
    if observed.branch_sha or observed.pr_number:
        raise DomainError("REMOTE_REF_CONFLICT", status=409)
    with transaction.atomic():
        _conditions(actor, run, bundle, snapshot)
        publication = Publication.objects.create(project=run.project, run=run, bundle=bundle, policy=policy, snapshot=snapshot, snapshot_hash=_hash(snapshot), created_by=actor)
        _remember(publication, observed)
        pin_bundle(bundle.id, f"publication:{publication.id}")
        PublicationAction.objects.filter(job=job, operation="prepare").update(publication=publication)
    return publication


def _execute_job(job, transport):
    publication = _get(Publication, job.subject_id)
    approval = _get(Approval, job.payload.get("approval_id"))
    if approval.publication_id != publication.id or publication.state != "APPROVED":
        raise DomainError("APPROVAL_INVALID", status=409)
    current_attempt = None
    dispatched = False
    try:
        with transaction.atomic():
            _current_approval(publication, approval)
            _transition(publication, "PUBLISHING", reconciled_at=None)
        observed = transport.observe(publication.snapshot, job)
        _remember(publication, observed)
        _check_observation(publication.snapshot, observed)
        if approval.scope == "REMAINING_PR" and observed.branch_sha != publication.snapshot["commit_sha"]:
            raise DomainError("REMOTE_REF_CONFLICT", status=409)
        if not observed.branch_sha:
            with transaction.atomic():
                _current_approval(publication, approval)
                current_attempt = _attempt(publication, approval, job, "PUSH_DISPATCH")
            dispatched = True
            transport.push(publication.snapshot, job)
            observed = transport.observe(publication.snapshot, job)
            _remember(publication, observed)
            if observed.branch_sha == publication.snapshot["commit_sha"]:
                dispatched = False
            _check_observation(publication.snapshot, observed)
            if observed.branch_sha != publication.snapshot["commit_sha"]:
                raise DomainError("REMOTE_OUTCOME_UNKNOWN")
            current_attempt.confirmed_commit_sha = observed.branch_sha
            current_attempt.finished_at = timezone.now()
            current_attempt.save(update_fields=["confirmed_commit_sha", "finished_at"])
            dispatched = False
        _current_approval(publication, approval)
        if publication.snapshot["policy"]["mode"] == "PUSH_AND_PR" and not observed.pr_number:
            observed = transport.observe(publication.snapshot, job)
            _remember(publication, observed)
            _check_observation(publication.snapshot, observed)
            if not observed.pr_number:
                with transaction.atomic():
                    _current_approval(publication, approval)
                    current_attempt = _attempt(publication, approval, job, "PR_DISPATCH")
                dispatched = True
                transport.create_pr(publication.snapshot, job)
        observed = transport.observe(publication.snapshot, job)
        _remember(publication, observed)
        if observed.branch_sha == publication.snapshot["commit_sha"] and (publication.snapshot["policy"]["mode"] == "PUSH" or (observed.pr_number and observed.pr_head_sha == publication.snapshot["commit_sha"])):
            dispatched = False
        _check_observation(publication.snapshot, observed)
        if observed.branch_sha != publication.snapshot["commit_sha"] or (publication.snapshot["policy"]["mode"] == "PUSH_AND_PR" and not observed.pr_number):
            raise DomainError("REMOTE_OUTCOME_UNKNOWN")
        with transaction.atomic():
            _current_approval(publication, approval)
            _transition(publication, "PUBLISHED", error_code="")
            if current_attempt:
                current_attempt.finished_at = timezone.now()
                current_attempt.confirmed_commit_sha = observed.branch_sha
                current_attempt.evidence = {"pr_number": observed.pr_number, "base_sha": observed.base_sha}
                current_attempt.save(update_fields=["finished_at", "confirmed_commit_sha", "evidence"])
            _unpin(publication)
    except Exception as exc:
        code = exc.code if isinstance(exc, DomainError) else "REMOTE_OUTCOME_UNKNOWN"
        with transaction.atomic():
            publication.refresh_from_db()
            if current_attempt:
                current_attempt.error_code = code
                current_attempt.finished_at = timezone.now()
                current_attempt.save(update_fields=["error_code", "finished_at"])
            if dispatched or code == "REMOTE_OUTCOME_UNKNOWN":
                _transition(publication, "UNKNOWN", error_code=code)
            elif publication.remote_effects_observed or publication.remote_branch_sha or publication.remote_pr_number:
                _transition(publication, "PARTIAL", error_code=code)
                Approval.objects.filter(publication=publication, valid=True).update(valid=False, invalid_reason=code)
            else:
                _invalidate(publication, code)
    return publication


def _reconcile_job(job, transport):
    publication = _get(Publication, job.subject_id)
    actor = require_access(job.actor, "publication.publish")
    attempt = _attempt(publication, None, job, "RECONCILE")
    try:
        observed = transport.observe(publication.snapshot, job)
        _remember(publication, observed)
        _check_observation(publication.snapshot, observed, require_base=False)
        if observed.branch_sha:
            state = "PARTIAL"
            complete = publication.snapshot["policy"]["mode"] == "PUSH" or bool(observed.pr_number)
            approval = publication.approvals.filter(decision="APPROVE", valid=True).order_by("-decided_at").first()
            if complete and approval:
                try:
                    _current_approval(publication, approval)
                    _check_observation(publication.snapshot, observed)
                    state = "PUBLISHED"
                except DomainError:
                    pass
        else:
            state = "PARTIAL" if publication.remote_effects_observed else "FAILED"
            if observed.pr_number:
                state = "UNKNOWN"
        with transaction.atomic():
            _transition(publication, state, reconciled_at=timezone.now() if state != "UNKNOWN" else None, error_code="")
            if state == "PUBLISHED":
                _unpin(publication)
            attempt.confirmed_commit_sha = observed.branch_sha
            attempt.evidence = {"base_sha": observed.base_sha, "pr_number": observed.pr_number, "actor_id": str(actor.pk)}
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=["confirmed_commit_sha", "evidence", "finished_at"])
    except Exception as exc:
        code = exc.code if isinstance(exc, DomainError) else "REMOTE_OUTCOME_UNKNOWN"
        _transition(publication, "UNKNOWN", error_code=code, reconciled_at=None)
        attempt.error_code = code
        attempt.finished_at = timezone.now()
        attempt.save(update_fields=["error_code", "finished_at"])
    return publication


def process_job(job, transport=None):
    from .transport import GitHubTransport

    if job.state != "RUNNING" or job.slot != "EXECUTION":
        raise DomainError("WORKER_LEASE_REQUIRED", status=409)
    transport = transport or GitHubTransport()
    handler = {"publication.prepare": _prepare_job, "publication.execute": _execute_job, "publication.reconcile": _reconcile_job}.get(job.kind)
    if handler is None:
        raise DomainError("UNKNOWN_JOB_KIND")
    return handler(job, transport)


def authorize_transport_write(job, snapshot):
    from apps.runs.models import Job

    with transaction.atomic():
        current = _get(Job, job.id)
        if current.state != "RUNNING" or current.lease_owner != job.lease_owner or current.lease_epoch != job.lease_epoch or current.instance_id != job.instance_id:
            raise DomainError("WORKER_LEASE_REQUIRED", status=409)
        publication = _get(Publication, job.subject_id)
        approval = _get(Approval, job.payload.get("approval_id"))
        if publication.state != "PUBLISHING" or approval.publication_id != publication.id or publication.snapshot != snapshot:
            raise DomainError("APPROVAL_INVALID", status=409)
        _current_approval(publication, approval)


@_atomic
def mark_job_interrupted(job, code="WORKER_LOST"):
    if job.kind not in {"publication.execute", "publication.reconcile"}:
        return
    publication = Publication.objects.filter(pk=job.subject_id).first()
    if publication is None or publication.abandoned_at or publication.state in {"PUBLISHED", "REJECTED", "INVALIDATED"}:
        return
    Approval.objects.filter(publication=publication, valid=True).update(valid=False, invalid_reason=code)
    if publication.state in {"PUBLISHING", "UNKNOWN"} or publication.attempts.filter(job=job, stage__endswith="DISPATCH").exists():
        _transition(publication, "UNKNOWN", error_code=code, reconciled_at=None)
    else:
        _invalidate(publication, code)


@_atomic
def revoke_actor_publications(user_id, project_id=None):
    approvals = Approval.objects.filter(actor_id=user_id, valid=True, decision="APPROVE")
    if project_id is not None:
        approvals = approvals.filter(publication__project_id=project_id)
    publication_ids = list(approvals.values_list("publication_id", flat=True).distinct())
    approvals.update(valid=False, invalid_reason="ACCESS_REVOKED")
    for publication in Publication.objects.filter(pk__in=publication_ids).exclude(state__in=["PUBLISHED", "REJECTED", "INVALIDATED"]):
        if publication.state in {"PUBLISHING", "UNKNOWN"}:
            _transition(publication, "UNKNOWN", error_code="ACCESS_REVOKED", reconciled_at=None)
        else:
            _invalidate(publication, "ACCESS_REVOKED")
