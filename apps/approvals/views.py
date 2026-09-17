import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.accounts.services import require_access
from apps.errors import DomainError
from apps.projects.models import Project

from . import services
from .forms import DecisionForm, PublicationPolicyForm
from .models import Publication, PublicationPolicy


def _error(request, error):
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"error": {"code": error.code, "message": error.message}}, status=error.status)
    return render(request, "approvals/error.html", {"error": error}, status=error.status)


@login_required
@require_GET
def list_view(request):
    from apps.accounts.services import filter_owned_queryset

    try:
        publications = filter_owned_queryset(request.user, Publication.objects.all(), owner_lookup="run__requested_by").select_related("project", "run").order_by("-created_at")
        from apps.artifacts.models import ResultReview
        from apps.artifacts.reviews import is_current_result
        reviews = ResultReview.objects.filter(state="AWAITING_ADMIN").select_related("bundle__run__project", "bundle__request_revision__request", "accepted_by") if request.user.role == "ADMIN" else []
        pending = [review for review in reviews if is_current_result(review.bundle, review.bundle.request_revision.request)]
        return render(request, "approvals/list.html", {"publications": publications[:100], "pending_reviews": pending})
    except DomainError as error:
        return _error(request, error)


@login_required
@require_http_methods(["GET", "POST"])
def policy_view(request, project_id):
    try:
        require_access(request.user, "project.manage", project_id=project_id)
        project = services._get(Project, project_id)
        policy = PublicationPolicy.objects.filter(project=project).first()
        form = PublicationPolicyForm(request.POST or None, instance=policy, initial={"expected_version": policy.version if policy else 0})
        if request.method == "POST" and form.is_valid():
            values = form.cleaned_data.copy()
            expected = values.pop("expected_version")
            values["repository_host"] = "github.com"
            services.configure_policy(request.user, project_id, expected, **values)
            return redirect("approvals:policy", project_id=project_id)
        return render(request, "approvals/policy.html", {"project": project, "form": form})
    except DomainError as error:
        return _error(request, error)


@login_required
@require_POST
def prepare_view(request, run_id):
    try:
        job = services.prepare_publication(request.user, run_id, request.POST.get("idempotency_key", ""))
        return render(request, "approvals/preparing.html", {"job": job}, status=202)
    except DomainError as error:
        return _error(request, error)


@login_required
@require_GET
def preparation_status_view(request, job_id):
    from apps.runs.models import Job

    try:
        require_access(request.user, "publication.publish")
        job = services._get(Job, job_id)
        if job.kind != "publication.prepare":
            raise DomainError("NOT_FOUND", status=404)
        receipt = job.publicationaction_set.filter(operation="prepare", publication__isnull=False).first()
        if receipt:
            return redirect("approvals:detail", publication_id=receipt.publication_id)
        return render(request, "approvals/preparing.html", {"job": job, "retry_key": str(uuid.uuid4())})
    except DomainError as error:
        return _error(request, error)


@login_required
@require_GET
def detail_view(request, publication_id):
    from apps.runs.models import Job

    try:
        publication = services.get_publication(request.user, publication_id)
        form = DecisionForm(initial={"snapshot_hash": publication.snapshot_hash, "idempotency_key": str(uuid.uuid4())})
        try:
            require_access(request.user, "publication.approve")
            admin = True
        except DomainError:
            admin = False
        processing = Job.objects.filter(subject_id=str(publication.id), kind__startswith="publication.", state__in=["QUEUED", "RUNNING"]).exists()
        return render(request, "approvals/detail.html", {"publication": publication, "form": form, "admin": admin, "processing": processing})
    except DomainError as error:
        return _error(request, error)


@login_required
@require_POST
def action_view(request, publication_id, action):
    form = DecisionForm(request.POST)
    if not form.is_valid():
        return _error(request, DomainError("INVALID_INPUT"))
    values = form.cleaned_data
    try:
        if action in {"approve", "reject"}:
            services.decide_publication(request.user, publication_id, values["snapshot_hash"], "APPROVE" if action == "approve" else "REJECT", values["idempotency_key"], values["reason"])
        elif action == "reconcile":
            services.reconcile_publication(request.user, publication_id, values["idempotency_key"])
        elif action in {"remaining", "retry"}:
            function = services.approve_remaining if action == "remaining" else services.retry_publication
            function(request.user, publication_id, values["snapshot_hash"], values["idempotency_key"])
        elif action == "abandon":
            services.abandon_remaining(request.user, publication_id, values["snapshot_hash"], values["reason"], values["idempotency_key"])
        else:
            raise DomainError("NOT_FOUND", status=404)
        return redirect("approvals:detail", publication_id=publication_id)
    except DomainError as error:
        return _error(request, error)
