from django.db import transaction

from apps.accounts.models import AccountAudit
from apps.accounts.services import require_access
from apps.errors import DomainError
from apps.runs.models import Run
from apps.work_requests.models import WorkRequest
from apps.work_requests.services import save_draft

from .models import ResultReview
from .services import get_result, pin_bundle, unpin_bundle, verify_bundle


def is_current_result(bundle, work_request):
    latest = Run.objects.filter(request_revision__request=work_request).first()
    return (
        latest is not None and latest.id == bundle.run_id
        and work_request.draft_version == bundle.request_revision.source_draft_version
        and not bundle.run.result_bundles.filter(version__gt=bundle.version).exists()
    )


@transaction.atomic
def decide_result(actor, bundle_id, action, manifest_sha256, reason=""):
    if action not in {"accept", "rework", "approve", "reject"} or not isinstance(reason, str) or len(reason) > 2000:
        raise DomainError("INVALID_INPUT")
    bundle = get_result(actor, bundle_id)
    work_request = WorkRequest.objects.select_for_update().get(pk=bundle.request_revision.request_id)
    actor = require_access(actor, "result.approve" if action in {"approve", "reject"} else "request.edit", obj=work_request)
    if manifest_sha256 != bundle.manifest_sha256:
        raise DomainError("RESULT_CHANGED", "최신 결과를 다시 확인해 주세요", status=409)
    review = ResultReview.objects.select_for_update().filter(bundle=bundle).first()
    target = {"accept": "AWAITING_ADMIN", "rework": "CHANGES_REQUESTED", "approve": "APPROVED", "reject": "REJECTED"}[action]
    reason = reason.strip()
    if review and review.state == target and (action not in {"rework", "reject"} or review.feedback == reason):
        return review
    if not is_current_result(bundle, work_request):
        raise DomainError("RESULT_CHANGED", "새 명세 또는 실행이 있습니다. 최신 결과를 확인해 주세요", status=409)
    if bundle.run.status != "SUCCEEDED" or bundle.completeness != "COMPLETE" or bundle.visibility != "NORMAL":
        raise DomainError("RESULT_NOT_READY", "수정과 검증이 완료된 결과만 확인할 수 있습니다", status=409)
    verify_bundle(bundle.id, manifest_sha256)
    previous = review.state if review else "PENDING_USER"
    allowed = {"accept": {"PENDING_USER"}, "rework": {"PENDING_USER", "REJECTED"}, "approve": {"AWAITING_ADMIN"}, "reject": {"AWAITING_ADMIN"}}
    if previous not in allowed[action]:
        raise DomainError("STATE_CONFLICT", "현재 결과 상태에서는 이 작업을 할 수 없습니다", status=409)
    if action in {"rework", "reject"} and not reason:
        raise DomainError("INVALID_INPUT", "보완할 내용을 입력해 주세요")
    if action == "rework":
        fields = dict(bundle.request_revision.snapshot)
        fields["expected_behavior"] = fields.get("expected_behavior", "") + "\n\n재수정 요청: " + reason
        save_draft(actor, work_request.id, work_request.draft_version, fields)
    if review is None:
        review = ResultReview(bundle=bundle, manifest_sha256=manifest_sha256)
    review.state = target
    if action == "accept":
        review.accepted_by = actor
    if action in {"approve", "reject"}:
        review.decided_by = actor
    if reason:
        review.feedback = reason
    review.save()
    if target == "AWAITING_ADMIN":
        pin_bundle(bundle.id, f"result-review:{review.pk}", expires_at=bundle.expires_at)
    else:
        unpin_bundle(bundle.id, f"result-review:{review.pk}")
    AccountAudit.objects.create(actor=actor, subject_id=work_request.owner_id, project_id=work_request.project_id,
        action="result." + action, before={"state": previous},
        after={"state": target, "bundle_id": str(bundle.id), "manifest_sha256": manifest_sha256}, reason=reason[:500])
    return review
