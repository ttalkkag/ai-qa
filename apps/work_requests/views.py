import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.services import require_access
from apps.errors import DomainError

from . import services
from .forms import DraftForm


def _display_spec(fields):
    form = DraftForm()
    return [{"label": field.label, "value": "\n".join(value) if isinstance(value, list) else value}
            for key, field in form.fields.items()
            if key not in {"expected_draft_version", "attachment_ids"}
            for value in [fields.get(key, "")]]


def _context(actor, work_request, form=None):
    from apps.runs.models import Run
    from apps.artifacts.models import ResultReview
    latest_run = Run.objects.filter(request_revision__request=work_request).first()
    review = ResultReview.objects.filter(bundle_id=latest_run.result_bundle_id).first() if latest_run and latest_run.result_bundle_id else None
    can_edit = False
    try:
        require_access(actor, "request.edit", project_id=work_request.project_id, obj=work_request)
        can_edit = work_request.project.state == "ACTIVE"
    except DomainError:
        pass
    basis, basis_error = None, None
    if can_edit:
        try:
            basis = services.get_confirmation_basis(actor, work_request.id)
        except DomainError as error:
            basis_error = error.message
    suggestions = list(work_request.suggestions.select_related("invocation").order_by("-created_at"))
    for suggestion in suggestions:
        suggestion.display_rows = _display_spec(suggestion.proposed_fields)
    revisions = list(work_request.revisions.select_related("submission__primary_run"))
    for revision in revisions:
        revision.display_rows = _display_spec(revision.snapshot)
    return {"work_request": work_request, "latest_run": latest_run, "result_review": review, "form": form or DraftForm.for_request(work_request),
            "can_edit": can_edit, "basis": basis, "basis_error": basis_error,
            "operation_key": str(uuid.uuid4()), "submission_key": str(uuid.uuid4()),
            "suggestions": suggestions, "revisions": revisions, "saved_rows": _display_spec(work_request.draft)}


@login_required
@require_GET
def index(request):
    return render(request, "work_requests/list.html", {"work_requests": services.list_requests(request.user)})


@login_required
@require_GET
def detail(request, request_id):
    work_request = services.get_request(request.user, request_id)
    return render(request, "work_requests/detail.html", _context(request.user, work_request))


@login_required
@require_GET
def progress(request, request_id):
    work_request = services.get_request(request.user, request_id)
    suggestion = work_request.suggestions.select_related("invocation").order_by("-created_at").first()
    response = JsonResponse({"draft_version": work_request.draft_version,
        "suggestion_state": suggestion.invocation.state if suggestion else ""})
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def save_draft(request, request_id):
    work_request = services.get_request(request.user, request_id, "request.edit")
    form = DraftForm(request.POST, conversation=work_request.conversation)
    status = 400
    if form.is_valid():
        try:
            services.save_draft(request.user, request_id, form.cleaned_data["expected_draft_version"], form.spec_fields())
            return redirect("work_requests:detail", request_id=request_id)
        except DomainError as error:
            form.add_error(None, error.message)
            status = error.status
    return render(request, "work_requests/detail.html", _context(request.user, work_request, form), status=status)


@login_required
@require_POST
def refresh_basis(request, request_id):
    services.refresh_basis(request.user, request_id, request.POST.get("expected_draft_version"), request.POST.get("idempotency_key"))
    return redirect("work_requests:detail", request_id=request_id)


@login_required
@require_POST
def clarify(request, request_id):
    services.suggest_revision(request.user, request_id, request.POST.get("expected_draft_version"), request.POST.get("idempotency_key"))
    return redirect("work_requests:detail", request_id=request_id)


@login_required
@require_POST
def apply_suggestion(request, request_id, suggestion_id):
    services.apply_suggestion(request.user, request_id, suggestion_id, request.POST.get("expected_draft_version"))
    return redirect("work_requests:detail", request_id=request_id)


@login_required
@require_POST
def ready(request, request_id):
    services.confirm_revision(request.user, request_id, request.POST.get("expected_draft_version"),
        request.POST.get("expected_project_config_version"), request.POST.get("expected_base_commit_sha"))
    return redirect("work_requests:detail", request_id=request_id)


@login_required
@require_POST
def submit(request, request_id):
    submission = services.submit(request.user, request_id, request.POST.get("revision_id"), request.POST.get("idempotency_key"))
    return redirect(f"/runs/{submission.primary_run_id}/")
