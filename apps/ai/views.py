import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.services import require_access
from apps.errors import DomainError

from . import services
from .forms import ProfileForm
from .models import AIInvocation, ModelProfile, SandboxCheck


def _error(request, error):
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"error": error.code, "message": error.message}, status=error.status)
    return render(request, "error.html", {"error": error.code, "message": error.message}, status=error.status)


@login_required
@require_GET
def settings_view(request):
    try:
        require_access(request.user, "model.manage")
    except DomainError as error:
        return _error(request, error)
    profiles = [profile for key in services.DEFAULT_PROFILES if (profile := ModelProfile.objects.filter(key=key).order_by("-version").first()) is not None]
    return render(request, "ai/settings.html", {
        "profiles": profiles, "form": ProfileForm(), "key": uuid.uuid4(),
        "sandbox": SandboxCheck.objects.filter(checks__0__id="docker_runtime").first(), "invocations": AIInvocation.objects.order_by("-created_at")[:15],
    })


@login_required
@require_POST
def connection_view(request):
    try:
        require_access(request.user, "model.manage")
        result = services.check_connection()
    except DomainError as error:
        return _error(request, error)
    return JsonResponse(result)


@login_required
@require_POST
def save_profile_view(request):
    form = ProfileForm(request.POST)
    if not form.is_valid():
        return _error(request, DomainError("INVALID_INPUT"))
    try:
        services.save_profile(request.user, **form.cleaned_data)
    except DomainError as error:
        return _error(request, error)
    messages.success(request, "모델 프로필을 저장했습니다. 새 조합은 호출 검증 이후 사용할 수 있습니다.")
    return redirect("ai:settings")


@login_required
@require_POST
def validate_profile_view(request, profile_id):
    try:
        invocation = services.validate_profile(request.user, profile_id, request.POST.get("consent") == "yes", request.POST.get("key"))
    except DomainError as error:
        return _error(request, error)
    messages.success(request, f"프로필 점검을 대기열에 등록했습니다: {invocation.id}")
    return redirect("ai:settings")


@login_required
@require_POST
def sandbox_view(request):
    try:
        services.enqueue_sandbox_check(request.user, request.POST.get("key"))
    except DomainError as error:
        return _error(request, error)
    messages.success(request, "모델을 호출하지 않는 Docker 환경 점검을 대기열에 등록했습니다")
    return redirect("ai:settings")
