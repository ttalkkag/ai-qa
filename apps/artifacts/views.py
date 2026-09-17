import json
import re
import uuid

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .services import get_result, open_artifact, read_bundle_file, store_attachment
from .models import ResultReview
from .reviews import decide_result, is_current_result


def _summary_sections(summary):
    sections = []
    for paragraph in re.split(r"\n\s*\n|\n(?=\*\*[^\n]+\*\*(?:\n|$))", summary):
        heading = re.fullmatch(r"\*\*(.+)\*\*", paragraph.strip())
        text = re.sub(r"\[([^\]\n]+)\]\([^\n]+?\)", r"\1", paragraph)
        sections.append({"heading": heading.group(1) if heading else "", "text": text})
    return sections


@login_required
@require_GET
def result_detail(request, bundle_id):
    bundle = get_result(request.user, bundle_id)
    checks = []
    display_patch = ""
    diff_truncated = False
    if not getattr(bundle, "unavailable_reason", "") and bundle.visibility == "NORMAL":
        checks = json.loads(read_bundle_file(bundle.id, "checks.json"))
        for check in checks:
            check["display_outcome"] = check.get("outcome", check.get("status", "UNVERIFIED"))
            check["display_count"] = check.get("collected_count")
        patch = read_bundle_file(bundle.id, "canonical.patch")
        remaining = 1024 * 1024
        portions = []
        for section in patch.split(b"diff --git "):
            if not section:
                continue
            portion = b"diff --git " + section
            limit = min(remaining, 200 * 1024)
            portions.append(portion[:limit])
            remaining -= min(len(portion), limit)
            diff_truncated = diff_truncated or len(portion) > limit
            if remaining <= 0:
                diff_truncated = True
                break
        display_patch = b"\n".join(portions).decode("utf-8", errors="replace")
    review = ResultReview.objects.filter(bundle=bundle).first()
    work_request = bundle.request_revision.request
    current = is_current_result(bundle, work_request)
    response = render(request, "artifacts/detail.html", {"bundle": bundle, "review": review, "current_result": current,
        "owner_review": current and request.user.pk == work_request.owner_id and (review is None or review.state == "REJECTED"),
        "work_request": work_request, "summary_sections": _summary_sections(bundle.summary), "artifacts": bundle.artifacts.exclude(kind="SOURCE_BASELINE"), "changes": bundle.changed_files.all(), "checks": checks, "display_patch": display_patch, "diff_truncated": diff_truncated, "publication_key": str(uuid.uuid4())})
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def review_result(request, bundle_id):
    review = decide_result(request.user, bundle_id, request.POST.get("action"), request.POST.get("manifest_sha256"), request.POST.get("reason", ""))
    if review.state == "CHANGES_REQUESTED":
        return redirect("work_requests:detail", request_id=review.bundle.request_revision.request_id)
    return redirect("artifacts:detail", bundle_id=bundle_id)


@login_required
@require_GET
def download(request, artifact_id):
    stream = open_artifact(request.user, artifact_id)
    artifact = stream.artifact
    response = FileResponse(stream, as_attachment=True, filename=artifact.display_name, content_type="application/octet-stream")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def upload(request, conversation_id):
    files = request.FILES
    if getattr(request, "attachment_upload_rejected", False) or "file" not in files:
        from apps.errors import DomainError
        raise DomainError("ATTACHMENT_TOO_LARGE" if getattr(request, "attachment_upload_rejected", False) else "ATTACHMENT_REQUIRED")
    artifact = store_attachment(request.user, conversation_id, files["file"])
    return JsonResponse({"id": str(artifact.id), "sha256": artifact.sha256, "size_bytes": artifact.size_bytes, "mime_type": artifact.mime_type, "visibility": artifact.visibility}, status=201)
