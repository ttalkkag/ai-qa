import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.errors import DomainError
from . import services
from .models import Run


def _error(request, error):
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"error": {"code": error.code, "message": error.message}}, status=error.status)
    return render(request, "runs/error.html", {"error": error}, status=error.status)


@login_required
@require_GET
def detail(request, run_id):
    try:
        result = services.get_run(request.user, run_id)
    except DomainError as error:
        return _error(request, error)
    work_request = Run.objects.select_related("request_revision__request").get(pk=result["id"]).request_revision.request
    return render(request, "runs/detail.html", {"run": result, "work_request": work_request, "cancel_key": uuid.uuid4(), "retry_key": uuid.uuid4()})


@login_required
@require_GET
def status(request, run_id):
    try:
        result = services.get_run(request.user, run_id, request.GET.get("after_sequence", 0))
    except DomainError as error:
        return _error(request, error)
    response = JsonResponse(result)
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def cancel(request, run_id):
    try:
        services.cancel_run(request.user, run_id, request.POST.get("key"), operational=request.POST.get("operational") == "1")
    except DomainError as error:
        return _error(request, error)
    return redirect("runs:detail", run_id=run_id)


@login_required
@require_POST
def retry(request, run_id):
    try:
        result = services.retry_run(request.user, run_id, request.POST.get("key"))
    except DomainError as error:
        return _error(request, error)
    return redirect("runs:detail", run_id=result.run.pk)
