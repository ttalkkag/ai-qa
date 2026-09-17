from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.services import list_accessible_projects, require_access
from apps.errors import DomainError
from .forms import DependencyBundleForm, ProjectConfigurationForm, ProjectDetailsForm, ProjectStateForm, RegisterProjectForm
from .services import configuration_snapshot, get_project, inspect_project, register_dependency_bundle, register_project, set_project_state, update_configuration, update_project_details


REASON_LABELS = {
    "REQUIRED_CHECK_MISSING": "관리자가 필수 검증을 등록해야 합니다.",
    "WRITE_SCOPE_EMPTY": "수정 가능한 파일 범위가 없습니다.",
    "BASE_REF_NOT_FOUND": "기준 commit을 찾을 수 없습니다. 관리자가 Git 기준을 확인해야 합니다.",
    "INVALID_SOURCE_REF": "작업 정책 설정에서 기준 브랜치명을 확인하세요. 커밋 SHA는 사용할 수 없습니다.",
    "REPOSITORY_UNAVAILABLE": "호스트에서 저장소를 읽을 수 없습니다.",
    "INVALID_REPOSITORY": "정상 Git 프로젝트 위치를 확인해야 합니다.",
    "PATH_NOT_ALLOWED": "프로젝트 경로 또는 파일 접근 정책을 확인해야 합니다.",
    "PROJECT_ARCHIVED": "아카이브된 프로젝트입니다. 과거 기록만 조회할 수 있습니다.",
    "COMMAND_CWD_MISSING": "등록된 검증의 작업 디렉터리가 기준 코드에 없습니다.",
    "COMMAND_SOURCE_NOT_READABLE": "검증에 필요한 코드가 읽기 허용 범위 밖에 있습니다.",
    "INVALID_COMMAND": "현재 기준 코드와 검증 명령 설정을 다시 확인해야 합니다.",
    "RESERVED_PATH": "프로젝트 코드에 앱 예약 경로가 있습니다.",
    "UNSUPPORTED_SOURCE": "현재 로컬 실행에서 지원하지 않는 저장소 구성이 있습니다.",
}


@login_required
@require_GET
def project_list(request):
    actor = require_access(request.user, "account.password")
    projects = list_accessible_projects(actor).select_related("health")
    return render(request, "projects/list.html", {"projects": projects, "can_manage": actor.role == "ADMIN"})


@login_required
@require_http_methods(["GET", "POST"])
def project_create(request):
    require_access(request.user, "project.manage")
    form = RegisterProjectForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            project = register_project(request.user, data["name"], data["local_path"], data["source_ref"], data["description"])
        except DomainError as error:
            form.add_error(None, error.message)
            status = error.status
        else:
            messages.success(request, "프로젝트를 등록했습니다. 수정 실행 전에 필수 검증 명령을 등록하세요.")
            return redirect("projects:detail", project_id=project.pk)
    return render(request, "projects/form.html", {"form": form, "title": "프로젝트 추가", "submit_label": "프로젝트 등록", "is_registration": True}, status=status)


@login_required
@require_GET
def project_detail(request, project_id):
    actor = require_access(request.user, "project.view", project_id=project_id)
    project = get_project(project_id)
    health = getattr(project, "health", None)
    revision = project.current_revision
    checks = list(revision.verification_commands.values("command_id", "display_name", "required", "kind"))
    reasons = [REASON_LABELS.get(reason, "관리자가 프로젝트 설정을 확인해야 합니다.") for reason in health.blocking_reasons] if health else ["프로젝트 진단을 실행해 주세요."]
    if project.state == "ARCHIVED":
        detail_status = {"label": "아카이브", "tone": "neutral"}
    elif not health:
        detail_status = {"label": "상태 확인 필요", "tone": "warning"}
    elif health.readiness == "UNAVAILABLE":
        detail_status = {"label": "저장소 접근 불가", "tone": "danger"}
    elif health.modification_ready:
        detail_status = {"label": "프로젝트 준비 완료", "tone": "ready"}
    else:
        detail_status = {"label": "프로젝트 설정 필요", "tone": "warning"}
    return render(request, "projects/detail.html", {
        "project": project, "health": health, "checks": checks, "reasons": reasons,
        "detail_status": detail_status, "required_check_count": sum(check["required"] for check in checks),
        "can_manage": actor.role == "ADMIN", "revision": revision,
        "state_form": ProjectStateForm(initial={"state": project.state}),
    })


@login_required
@require_http_methods(["GET", "POST"])
def project_configuration(request, project_id):
    require_access(request.user, "project.manage", project_id=project_id)
    project = get_project(project_id)
    form = ProjectConfigurationForm(request.POST or None, initial=ProjectConfigurationForm.initial_for(project, configuration_snapshot(project.current_revision)))
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            update_configuration(request.user, project_id, form.cleaned_data["expected_version"], form.policy(), local_path=form.cleaned_data["local_path"])
        except DomainError as error:
            form.add_error(None, error.message)
            status = error.status
        else:
            messages.success(request, "새 설정 버전을 저장했습니다. 기존 명세는 자동으로 변경되지 않습니다.")
            return redirect("projects:detail", project_id=project_id)
    if request.method == "POST" and form.errors:
        form.data = form.data.copy()
        form.data["verification_commands"] = "[]"
        form.data["preparation_commands"] = "[]"
        status = status if status != 200 else 400
    return render(request, "projects/configuration.html", {"form": form, "project": project, "title": "작업 정책 설정", "submit_label": "새 설정 버전 저장"}, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def project_edit(request, project_id):
    require_access(request.user, "project.manage", project_id=project_id)
    project = get_project(project_id)
    form = ProjectDetailsForm(request.POST or None, initial={"name": project.name, "description": project.description})
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            update_project_details(request.user, project_id, **form.cleaned_data)
        except DomainError as error:
            form.add_error(None, error.message)
            status = error.status
        else:
            messages.success(request, "프로젝트 이름과 설명을 저장했습니다. 실행 정책은 변경되지 않았습니다.")
            return redirect("projects:detail", project_id=project_id)
    return render(request, "projects/form.html", {"form": form, "project": project, "title": "프로젝트 설명 변경", "submit_label": "저장"}, status=status)


@login_required
@require_POST
def project_inspect(request, project_id):
    health = inspect_project(request.user, project_id)
    messages.info(request, "읽기 전용 진단을 완료했습니다. 준비 상태는 실제 검사 통과를 의미하지 않습니다.")
    return redirect("projects:detail", project_id=health.project_id)


@login_required
@require_POST
def project_state(request, project_id):
    require_access(request.user, "project.manage", project_id=project_id)
    form = ProjectStateForm(request.POST)
    if not form.is_valid():
        raise DomainError("INVALID_INPUT", "상태와 변경 사유를 확인하세요.")
    set_project_state(request.user, project_id, **form.cleaned_data)
    messages.success(request, "프로젝트 상태를 변경했습니다. 과거 기록은 보존합니다.")
    return redirect("projects:detail", project_id=project_id)


@login_required
@require_http_methods(["GET", "POST"])
def project_dependency(request, project_id):
    require_access(request.user, "project.manage", project_id=project_id)
    project = get_project(project_id)
    form = DependencyBundleForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            bundle = register_dependency_bundle(request.user, project_id, **form.cleaned_data)
        except DomainError as error:
            form.add_error(None, error.message)
            status = error.status
        else:
            messages.success(request, "의존성 자료를 등록했습니다. 준비 명령의 dependency_bundle_id로 선택하세요: " + str(bundle.pk))
            return redirect("projects:dependencies", project_id=project_id)
    return render(request, "projects/dependencies.html", {"form": form, "project": project, "bundles": project.dependency_bundles.all(), "title": "오프라인 의존성 자료", "submit_label": "자료 해시 등록"}, status=status)
