import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils import timezone

from apps.accounts.models import ProjectMembership
from apps.errors import DomainError
from apps.projects import services
from apps.projects.forms import ProjectConfigurationForm, RegisterProjectForm
from apps.projects.models import DependencyBundle, Project, ProjectConfigRevision, VerificationCommand


pytestmark = pytest.mark.django_db


def git(repo, *args):
    return subprocess.run(
        ["/usr/bin/git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True, capture_output=True, env={
            "PATH": "/usr/bin:/bin", "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
        },
    ).stdout


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(username="projects-admin", password="Project-Test-Password-9842", role="ADMIN")


@pytest.fixture
def manager():
    return get_user_model().objects.create_user(username="projects-manager", password="Project-Test-Password-9842", role="MANAGER")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("value = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text("def test_value():\n    assert True\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial fixture")
    return root


@pytest.fixture
def policy():
    return {
        "source_ref": "main", "read_paths": ["."], "write_paths": ["."],
        "excluded_paths": list(services.DEFAULT_EXCLUDED_PATHS),
        "verification_commands": [{
            "command_id": "unit", "display_name": "단위 테스트", "kind": "TEST",
            "parser_id": "pytest-summary", "parser_version": 1,
            "argv": [".venv/bin/python", "-m", "pytest", "-q"], "cwd": ".",
            "timeout_sec": 300, "required": True,
        }],
    }


@pytest.fixture
def project(admin, repo, policy):
    return services.register_project(admin, "예시 프로젝트", str(repo), "main", policy=policy)


def fail(code, operation, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code


def login(client, actor):
    client.force_login(actor)
    session = client.session
    session["authz_version"] = actor.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()


def assert_current_menu(content, menu_url):
    header = re.search(r'<header class="site-header">(.*?)</header>', content, re.S).group(1)
    active_links = re.findall(r'<a\b[^>]*\baria-current="page"[^>]*>', header)
    assert len(active_links) == 1
    assert f'href="{menu_url}"' in active_links[0]


def test_admin_empty_project_list_guides_project_creation(client, admin):
    login(client, admin)
    response = client.get(reverse("projects:list"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "프로젝트를 연결하고 시작하세요." in content
    assert "관리자에게 문의하세요." not in content
    assert f'href="{reverse("projects:create")}"' in content
    assert '첫 프로젝트 시작하기' not in content
    assert '프로젝트 등록 → 작업 준비 → 매니저 할당 → 요청 시작' not in content
    assert 'href="/docs/#project-setup"' in content
    assert 'class="project-empty"' in content
    assert 'site-main container project-empty-main' in content
    assert '<details' not in content
    assert 'navigation-add' not in content
    main = re.search(r'<main\b[^>]*>(.*?)</main>', content, re.S).group(1)
    assert main.count('<p>') == 1
    assert main.count('<a ') == 2


def test_registration_uses_field_guidance_and_trailing_actions(client, admin):
    login(client, admin)
    response = client.get(reverse("projects:create"))
    content = response.content.decode()
    assert '등록 전 준비' not in content
    assert '등록 후 할 일' not in content
    assert '<h1' not in content
    assert 'aria-current="page">Add Project</a>' in content
    assert 'GitHub URL이 아닌' in content
    assert 'id_local_path_helptext' in content
    assert '/Users/your-name/Projects/sample-board' in content
    assert '최소 한 번 커밋된 코드' in content
    assert content.index('GitHub URL이 아닌') < content.index('macOS:') < content.index('Windows:')
    assert '<li>macOS: <code>/Users/your-name/Projects/sample-board</code></li>' in content
    assert r'<li>Windows: <code>C:\Users\your-name\Projects\sample-board</code></li>' in content
    for removed in ['경로 이름으로 복사', '주소 표시줄', 'Windows 서버 실행은 미검증', '서버 Mac의 경로를 입력하세요']:
        assert removed not in content
    assert response.context['form'].fields['source_ref'].label == 'Branch'
    assert not response.context['form'].fields['source_ref'].required
    assert 'placeholder' not in response.context['form'].fields['source_ref'].widget.attrs
    assert 'develop → main → master → 현재 브랜치' in content
    assert 'data-confirm="프로젝트를 등록할까요?"' in content
    assert 'data-confirm-title="프로젝트 생성"' in content
    assert '신뢰하는 저장소를 연결합니다' not in content
    assert 'inert data-confirm=' in content
    form = re.search(r'<form\b[^>]*>(.*?)</form>', content, re.S).group(1)
    actions = re.search(r'<div class="form-actions">(.*?)</div>\s*$', form, re.S).group(1)
    assert actions.index('프로젝트 등록 가이드') < actions.index('<button')
    assert 'href="/docs/#project-setup"' in actions
    for name in ['name', 'description', 'local_path', 'source_ref']:
        assert f'id="id_{name}_helptext"' in form
        assert f'aria-describedby="id_{name}_helptext"' in form
    assert 'confirm_scope' not in response.context['form'].fields
    assert 'type="checkbox"' not in form


@pytest.mark.parametrize('local_path', ['relative/repo', 'https://example.com/repo', '~/repo'])
def test_project_registration_rejects_non_absolute_path(client, admin, local_path):
    login(client, admin)
    response = client.post(reverse('projects:create'), {'name': 'Invalid', 'local_path': local_path})
    assert response.status_code == 200
    assert 'local_path' in response.context['form'].errors
    assert not Project.objects.exists()


def test_manager_start_guide_does_not_expose_admin_setup(client, manager):
    login(client, manager)
    content = client.get(reverse("projects:list")).content.decode()
    main = re.search(r'<main\b[^>]*>(.*?)</main>', content, re.S).group(1)
    assert '할당된 프로젝트가 없습니다. 관리자에게 문의하세요.' in main
    assert 'class="project-empty"' in main
    assert '사용 가이드' in main
    for absent in ['project-setup', 'Git 저장소', '/accounts/manage/', '/ai/']:
        assert absent not in main


def test_projects_page_displays_linked_location_header(client, admin):
    login(client, admin)
    response = client.get(reverse("projects:list"))
    content = response.content.decode()
    breadcrumb = re.search(r'<nav class="site-navigation" aria-label="현재 위치">(.*?)</nav>', content, re.S)
    assert breadcrumb is not None
    assert '<a href="/projects/" aria-current="page">Project</a>' in breadcrumb.group(1)


def test_project_configuration_location_links_to_its_project(client, admin, project):
    login(client, admin)
    response = client.get(reverse("projects:configure", args=[project.pk]))
    content = response.content.decode()
    breadcrumb = re.search(r'<nav class="site-navigation" aria-label="현재 위치">(.*?)</nav>', content, re.S).group(1)
    assert f'<a href="{reverse("projects:detail", args=[project.pk])}">{project.name}</a>' in breadcrumb
    assert f'<a href="{reverse("projects:configure", args=[project.pk])}" aria-current="page">Configuration</a>' in breadcrumb
    assert project.canonical_repo_path not in breadcrumb


@pytest.mark.parametrize("actor_fixture", [None, "admin", "manager"])
def test_pages_display_compact_footer_with_profile_links(client, request, actor_fixture):
    if actor_fixture:
        login(client, request.getfixturevalue(actor_fixture))
    response = client.get(reverse("projects:list"), follow=True)
    assert response.status_code == 200
    content = response.content.decode()
    assert '<footer class="site-footer">' in content
    assert "© kimchanhyung98" in content
    for label, url in [("GitHub", "https://git.chanhyung.kim"), ("LinkedIn", "https://linkedin.chanhyung.kim")]:
        assert f'href="{url}"' in content
        assert f'aria-label="{label} (새 탭)"' in content
    assert "로컬 작업 사본에서 실행합니다." not in content


@pytest.mark.parametrize("view_name,menu_url", [
    ("projects:list", "/projects/"),
    ("projects:create", "/projects/"),
    ("work_requests:list", "/requests/"),
    ("accounts:manage", "/admin/"),
    ("accounts:me", "/accounts/me/"),
    ("accounts:password", "/accounts/me/"),
    ("ai:settings", "/ai/"),
    ("approvals:list", "/admin/"),
    ("operations:dashboard", "/admin/"),
])
def test_admin_page_uses_current_menu_without_redundant_heading(client, admin, view_name, menu_url):
    login(client, admin)
    response = client.get(reverse(view_name))
    assert response.status_code == 200
    content = response.content.decode()
    assert_current_menu(content, menu_url)
    assert "<h1" not in content
    assert ">돌아가기</a>" not in content


@pytest.mark.parametrize("view_name,menu_url", [
    ("projects:list", "/projects/"),
    ("work_requests:list", "/requests/"),
    ("accounts:me", "/accounts/me/"),
    ("accounts:password", "/accounts/me/"),
    ("approvals:list", "/requests/"),
])
def test_manager_page_uses_current_menu_without_redundant_heading_or_admin_links(client, manager, view_name, menu_url):
    login(client, manager)
    response = client.get(reverse(view_name))
    assert response.status_code == 200
    content = response.content.decode()
    assert_current_menu(content, menu_url)
    assert "<h1" not in content
    header = re.search(r'<header class="site-header">(.*?)</header>', content, re.S).group(1)
    for admin_url in ("/admin/", "/accounts/manage/", "/ai/", "/approvals/", "/operations/"):
        assert f'href="{admin_url}"' not in header


@pytest.mark.parametrize("actor_fixture", ["admin", "manager"])
@pytest.mark.parametrize("view_name,menu_url", [
    ("projects:detail", "/projects/"),
    ("projects:configure", "/projects/"),
    ("projects:dependencies", "/projects/"),
    ("conversations:list", "/projects/"),
    ("conversations:detail", "/projects/"),
    ("work_requests:detail", "/requests/"),
    ("runs:detail", "/requests/"),
    ("artifacts:detail", "/requests/"),
    ("approvals:policy", "/projects/"),
    ("approvals:detail", "/admin/"),
    ("approvals:preparation-status", "/admin/"),
])
def test_header_maps_nested_routes_to_their_menu(request, actor_fixture, view_name, menu_url):
    path = reverse(view_name, args=["00000000-0000-0000-0000-000000000001"])
    http_request = RequestFactory().get(path, {"next": "/operations/"})
    http_request.user = request.getfixturevalue(actor_fixture)
    http_request.resolver_match = resolve(path)
    if actor_fixture == "manager" and menu_url == "/admin/":
        menu_url = "/requests/"
    content = render_to_string("base.html", request=http_request)
    assert_current_menu(content, menu_url)


@pytest.mark.parametrize("actor_fixture", ["admin", "manager"])
def test_home_and_logo_lead_to_active_projects_menu(client, request, actor_fixture):
    login(client, request.getfixturevalue(actor_fixture))
    response = client.get("/", follow=True)
    assert response.redirect_chain == [(reverse("projects:list"), 302)]
    assert response.status_code == 200
    content = response.content.decode()
    assert_current_menu(content, reverse("projects:list"))
    assert '<a class="brand" href="/projects/">' in content


def test_anonymous_home_requires_login_without_an_active_menu(client):
    response = client.get("/", follow=True)
    assert response.status_code == 200
    assert response.resolver_match.view_name == "accounts:login"
    header = re.search(r'<header class="site-header">(.*?)</header>', response.content.decode(), re.S).group(1)
    assert 'aria-current="page"' not in header
    assert 'aria-label="로그인"' in response.content.decode()


def test_manager_without_assignment_is_directed_to_admin(client, manager, project):
    login(client, manager)
    response = client.get(reverse("projects:list"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "할당된 프로젝트가 없습니다. 관리자에게 문의하세요." in content
    assert "프로젝트를 추가해 주세요." not in content
    assert reverse("projects:create") not in content
    assert project.name not in content


@pytest.mark.parametrize("actor_fixture", ["admin", "manager"])
def test_accessible_project_list_does_not_show_empty_guidance(client, request, actor_fixture, admin, manager, project):
    if actor_fixture == "manager":
        ProjectMembership.objects.create(user=manager, project=project, granted_by=admin)
    login(client, request.getfixturevalue(actor_fixture))
    response = client.get(reverse("projects:list"))
    assert response.status_code == 200
    content = response.content.decode()
    assert project.name in content
    assert "등록된 프로젝트가 없습니다." not in content
    assert "할당된 프로젝트가 없습니다." not in content
    assert '마지막 확인' in content
    assert 'class="project-table" aria-label="프로젝트 목록"' in content
    assert 'class="project-chip" data-tone="ready"' in content
    assert '<details' not in content
    assert 'project-empty-main' not in content
    main = re.search(r'<main\b[^>]*>(.*?)</main>', content, re.S).group(1)
    header = re.search(r'<header class="site-header">(.*?)</header>', content, re.S).group(1)
    navigation = re.search(r'<nav class="site-navigation"[^>]*>(.*?)</nav>', content, re.S).group(1)
    assert reverse('projects:create') not in main
    assert reverse('projects:create') not in header
    assert '프로젝트 등록 가이드' not in main
    if actor_fixture == 'admin':
        assert 'class="button secondary navigation-add"' in navigation
        assert f'href="{reverse("projects:create")}" title="프로젝트 추가">Add</a>' in navigation
    else:
        assert 'navigation-add' not in content


@pytest.mark.parametrize('view_name', ['projects:create', 'projects:detail', 'projects:configure', 'accounts:me'])
def test_project_add_navigation_is_only_on_list(client, admin, project, view_name):
    login(client, admin)
    args = [project.pk] if view_name in {'projects:detail', 'projects:configure'} else []
    content = client.get(reverse(view_name, args=args)).content.decode()
    assert 'navigation-add' not in content


def test_registration_resolves_git_root_and_immutable_policy(admin, repo, project):
    assert project.canonical_repo_path == str(repo.resolve())
    assert project.project_config_version == 1
    revision = project.current_revision
    assert revision.source_ref == "main"
    assert revision.canonical_repo_path == str(repo.resolve())
    assert revision.verification_commands.count() == 1
    result = services.resolve_execution_input(admin, project.pk, expected_version=1)
    assert result.base_commit_sha == git(repo, "rev-parse", "HEAD").decode().strip()
    assert result.policy["verification_commands"][0]["argv"] == [".venv/bin/python", "-m", "pytest", "-q"]
    assert project.health.modification_ready
    assert result.config_revision.pk == revision.pk


@pytest.mark.parametrize('state,readiness,ready,label', [
    ('ACTIVE', 'READY', True, '프로젝트 준비 완료'),
    ('ACTIVE', 'SETUP_REQUIRED', False, '프로젝트 설정 필요'),
    ('ACTIVE', 'UNAVAILABLE', False, '저장소 접근 불가'),
    ('ACTIVE', None, False, '상태 확인 필요'),
    ('ARCHIVED', 'READY', True, '아카이브'),
])
@pytest.mark.parametrize('page', ['list', 'detail'])
def test_project_status_separates_project_readiness_from_ai(client, admin, project, state, readiness, ready, label, page):
    project.state = state
    project.save(update_fields=['state'])
    if readiness is None:
        project.health.delete()
    else:
        health = project.health
        health.readiness = readiness
        health.modification_ready = ready
        health.source_ready = ready
        health.blocking_reasons = [] if ready else ['REQUIRED_CHECK_MISSING']
        health.save()
    login(client, admin)
    response = client.get(reverse(f'projects:{page}', args=[project.pk] if page == 'detail' else []))
    content = response.content.decode()
    if page == 'list':
        assert label in content
        assert 'data-label="프로젝트 상태"' in content
        assert 'data-label="마지막 확인"' in content
        assert project.canonical_repo_path not in content
        if state == 'ARCHIVED':
            assert 'data-tone="ready"' not in content
        if readiness is None:
            assert '확인 전' in content
        return
    assert response.context['detail_status']['label'] == label
    assert 'AI에서 별도 확인' in content
    assert '마지막 확인' in content
    assert '프로젝트 전체' in content
    assert 'class="project-facts"' in content
    assert 'class="project-management"' in content
    assert '<details class="project-state-settings">' in content
    assert '읽기: .' not in content
    assert '앱은 원본을 정리하거나 덮어쓰지 않습니다' not in content
    assert content.count(f'href="{reverse("projects:configure", args=[project.pk])}"') == 1
    if state == 'ARCHIVED':
        assert '과거 대화·요청 보기' in content
        assert '프로젝트 준비 완료' not in content
    elif readiness is None:
        assert '확인 전' in content


def test_detail_manager_sees_status_without_admin_controls(client, admin, manager, project):
    ProjectMembership.objects.create(user=manager, project=project, granted_by=admin)
    login(client, manager)
    content = client.get(reverse('projects:detail', args=[project.pk])).content.decode()
    assert '프로젝트 준비 완료' in content
    assert '관리자 확인 필요' in content
    assert '문제 상담·내 요청' in content
    assert 'class="project-management"' not in content
    assert project.canonical_repo_path not in content
    assert 'project-state-settings' not in content


def test_detail_shows_one_blocking_instruction_and_dirty_count(client, admin, project):
    health = project.health
    health.modification_ready = False
    health.readiness = 'SETUP_REQUIRED'
    health.blocking_reasons = ['REQUIRED_CHECK_MISSING']
    health.is_dirty = True
    health.dirty_count = 8
    health.save()
    login(client, admin)
    content = client.get(reverse('projects:detail', args=[project.pk])).content.decode()
    assert content.count('관리자가 필수 검증을 등록해야 합니다.') == 1
    assert '8건' in content
    assert '작업에서 제외' in content


def test_metadata_operations_preserve_head_index_dirty_and_untracked(admin, repo, project):
    (repo / "src" / "main.py").write_text("value = 2\n")
    git(repo, "add", "src/main.py")
    (repo / "src" / "main.py").write_text("value = 3\n")
    (repo / "untracked.txt").write_text("retain this\n")
    before = {
        "index": (repo / ".git" / "index").read_bytes(),
        "head": git(repo, "rev-parse", "HEAD"),
        "status": git(repo, "status", "--porcelain=v1", "-z"),
        "source": (repo / "src" / "main.py").read_bytes(),
    }
    health = services.inspect_project(admin, project.pk)
    assert health.is_dirty and health.dirty_count == 2
    assert health.modification_ready
    result = services.resolve_execution_input(admin, project.pk)
    assert result.base_commit_sha == before["head"].decode().strip()
    assert (repo / ".git" / "index").read_bytes() == before["index"]
    assert git(repo, "rev-parse", "HEAD") == before["head"]
    assert git(repo, "status", "--porcelain=v1", "-z") == before["status"]
    assert (repo / "src" / "main.py").read_bytes() == before["source"]
    assert (repo / "untracked.txt").read_text() == "retain this\n"


def test_no_required_check_blocks_modification_but_not_source_read(admin, repo):
    project = services.register_project(admin, "미설정", str(repo), "main")
    assert project.health.blocking_reasons == ["REQUIRED_CHECK_MISSING"]
    fail("PROJECT_NOT_READY", services.resolve_execution_input, admin, project.pk)
    assert services.resolve_execution_input(admin, project.pk, require_modification=False).base_commit_sha


def test_unborn_repository_cannot_create_requested_branch(admin, tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init", "-b", "main")
    fail("BASE_REF_NOT_FOUND", services.register_project, admin, "첫 commit 필요", str(root), "main")
    assert not Project.objects.exists()


def test_ref_move_requires_reconfirmation(admin, repo, project):
    original = services.resolve_execution_input(admin, project.pk)
    (repo / "next.txt").write_text("new committed source\n")
    git(repo, "add", "next.txt")
    git(repo, "commit", "-m", "next fixture")
    fail("BASE_CHANGED", services.resolve_execution_input, admin, project.pk, expected_sha=original.base_commit_sha)


def test_policy_update_creates_new_revision_and_retains_old(admin, project, policy):
    original = project.current_revision
    policy["write_paths"] = ["src"]
    updated = services.update_configuration(admin, project.pk, 1, policy)
    project.refresh_from_db()
    assert project.project_config_version == 2
    assert updated.write_paths == ["src"]
    assert ProjectConfigRevision.objects.get(pk=original.pk).write_paths == ["."]
    fail("VERSION_CONFLICT", services.resolve_execution_input, admin, project.pk, expected_version=1)
    fail("VERSION_CONFLICT", services.update_configuration, admin, project.pk, 1, policy)


def test_label_changes_do_not_change_execution_policy(admin, project):
    services.update_project_details(admin, project.pk, "새 이름", "설명")
    project.refresh_from_db()
    assert project.name == "새 이름"
    assert project.project_config_version == 1
    assert project.config_revisions.count() == 1


def test_policy_revision_and_command_updates_are_refused(project):
    revision = project.current_revision
    revision.source_ref = "changed"
    with pytest.raises(ValidationError):
        revision.save()
    with pytest.raises(ValidationError):
        ProjectConfigRevision.objects.filter(pk=revision.pk).update(source_ref="changed")
    with pytest.raises(ValidationError):
        revision.delete()
    with pytest.raises(ValidationError):
        VerificationCommand.objects.filter(config_revision=revision).update(required=False)


def test_constructed_existing_revision_cannot_overwrite_immutable_record(project, admin):
    existing = project.current_revision
    duplicate = ProjectConfigRevision(id=existing.pk, project=project, created_by=admin, canonical_repo_path=project.canonical_repo_path,
        project_config_version=1, source_ref="different", read_paths=["."], write_paths=["."], excluded_paths=[])
    with pytest.raises(IntegrityError), transaction.atomic():
        duplicate.save()
    existing.refresh_from_db()
    assert existing.source_ref == "main"


@pytest.mark.parametrize("source_ref", ["HEAD", "main~1", "main^{tree}", "main; echo x", "../main", "refs/remotes/origin/main", "@{-1}", "main.lock", "x/.hidden", "main\n"])
def test_revision_expressions_are_rejected(source_ref):
    fail("INVALID_SOURCE_REF", services.validate_source_ref, source_ref)


def test_full_sha_rejected_and_local_branch_ref_supported(admin, repo, policy):
    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    fail("INVALID_SOURCE_REF", services.register_project, admin, "고정 SHA", str(repo), sha)
    fail("INVALID_SOURCE_REF", services.register_project, admin, "정책 SHA", str(repo), "main", policy={**policy, "source_ref": sha})
    fail("INVALID_SOURCE_REF", services.resolve_base_sha, repo, sha)
    assert not Project.objects.exists()
    assert services.validate_source_ref("refs/heads/feature/fix") == "feature/fix"


@pytest.mark.parametrize('form_class', [RegisterProjectForm, ProjectConfigurationForm])
@pytest.mark.parametrize('value', ['a' * 40, 'A' * 64, 'HEAD', 'main~1'])
def test_branch_field_rejects_sha_and_expressions(form_class, value):
    field = form_class().fields['source_ref']
    with pytest.raises(ValidationError):
        field.clean(value)
    assert field.clean('') == ''
    assert field.clean('feature/fix') == 'feature/fix'
    assert 'placeholder' not in field.widget.attrs


@pytest.mark.parametrize('form_class', [RegisterProjectForm, ProjectConfigurationForm])
@pytest.mark.parametrize('value', ['Feature/auth', 'feature/User', 'my-feature', 'temp/_testing'])
def test_branch_field_enforces_hook_naming(form_class, value):
    with pytest.raises(ValidationError):
        form_class().fields['source_ref'].clean(value)


def test_invalid_branch_naming_post_and_service_leave_git_unchanged(client, admin, repo, project, policy):
    login(client, admin)
    response = client.post(reverse('projects:create'), {
        'name': '잘못된 이름', 'local_path': str(repo), 'source_ref': 'Feature/test',
    })
    assert 'source_ref' in response.context['form'].errors
    fail('INVALID_SOURCE_REF', services.register_project, admin, '규칙 검사', str(repo), 'Feature/test')
    fail('INVALID_SOURCE_REF', services.update_configuration, admin, project.pk, 1,
         {**policy, 'source_ref': 'Feature/test'})
    project.refresh_from_db()
    assert project.project_config_version == 1
    assert git(repo, 'branch', '--list', 'Feature/test') == b''


def test_legacy_current_branch_fallback_and_read_only_resolution_are_preserved(admin, repo):
    git(repo, 'branch', '-m', 'ExistingBranch')
    project = services.register_project(admin, '기존 브랜치', str(repo))
    assert project.current_revision.source_ref == 'ExistingBranch'
    assert services.resolve_base_sha(repo, 'ExistingBranch') == git(repo, 'rev-parse', 'HEAD').decode().strip()


@pytest.mark.parametrize('value', ['', 'main', 'master', 'develop', 'staging',
                                  'temp/ai-qa-testing', 'feature/api/user-auth', 'refs/heads/fix/issue-1'])
def test_branch_field_preserves_allowed_input(value):
    for form_class in (RegisterProjectForm, ProjectConfigurationForm):
        field = form_class().fields['source_ref']
        assert field.clean(value) == value
        assert 'pattern' in field.widget.attrs


@pytest.mark.parametrize('branches,current,expected', [
    (['develop', 'main', 'master'], 'feature/work', 'develop'),
    (['main', 'master'], 'feature/work', 'main'),
    (['master'], 'feature/work', 'master'),
    ([], 'feature/work', 'feature/work'),
    (['develop', 'main', 'master'], None, 'develop'),
    (['main'], None, 'main'),
])
def test_blank_branch_registration_selects_local_fallback(client, admin, repo, branches, current, expected):
    git(repo, 'branch', '-m', 'feature/work')
    for branch in branches:
        git(repo, 'branch', branch)
    if current is None:
        git(repo, 'checkout', '--detach')
    head_before = (repo / '.git' / 'HEAD').read_bytes()
    login(client, admin)
    response = client.post(reverse('projects:create'), {'name': '자동 브랜치', 'local_path': str(repo), 'source_ref': ''})
    assert response.status_code == 302
    project = Project.objects.get()
    assert project.current_revision.source_ref == expected
    assert project.health.base_commit_sha == git(repo, 'rev-parse', expected).decode().strip()
    assert (repo / '.git' / 'HEAD').read_bytes() == head_before


def test_detached_head_without_default_branch_requires_branch(admin, repo):
    git(repo, 'branch', '-m', 'feature/work')
    git(repo, 'checkout', '--detach')
    git(repo, 'tag', 'develop')
    git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    fail('BASE_REF_NOT_FOUND', services.register_project, admin, '기준 없음', str(repo))
    assert not Project.objects.exists()
    project = services.register_project(admin, '직접 지정', str(repo), 'feature/work')
    assert project.current_revision.source_ref == 'feature/work'


def test_unborn_branch_is_not_a_default(admin, tmp_path):
    root = tmp_path / 'empty-repo'
    root.mkdir()
    git(root, 'init', '-b', 'develop')
    fail('BASE_REF_NOT_FOUND', services.register_project, admin, '빈 저장소', str(root))
    assert not Project.objects.exists()


def test_explicit_branch_does_not_use_default(admin, repo):
    git(repo, 'branch', 'develop')
    project = services.register_project(admin, '직접 지정', str(repo), 'main')
    assert project.current_revision.source_ref == 'main'


def test_registration_creates_missing_branch_without_checkout_or_dirty_changes(admin, repo):
    baseline = git(repo, 'rev-parse', 'HEAD').decode().strip()
    (repo / 'src/main.py').write_text('value = 2\n')
    git(repo, 'add', 'src/main.py')
    (repo / 'notes.txt').write_text('keep my notes\n')
    before = git(repo, 'status', '--porcelain')
    project = services.register_project(admin, '새 브랜치', str(repo), 'temp/ai-qa-testing')
    assert project.current_revision.source_ref == 'temp/ai-qa-testing'
    assert services.resolve_base_sha(repo, 'temp/ai-qa-testing') == baseline
    assert git(repo, 'symbolic-ref', '--short', 'HEAD').strip() == b'main'
    assert git(repo, 'status', '--porcelain') == before
    assert (repo / 'src/main.py').read_text() == 'value = 2\n'
    assert (repo / 'notes.txt').read_text() == 'keep my notes\n'


def test_blank_configuration_branch_selects_default(admin, repo, project, policy):
    git(repo, 'branch', 'develop')
    revision = services.update_configuration(admin, project.pk, 1, {**policy, 'source_ref': ''})
    assert revision.source_ref == 'develop'
    assert project.config_revisions.get(project_config_version=1).source_ref == 'main'


def test_configuration_creates_branch_from_develop_and_never_resets_it(admin, repo, project, policy):
    git(repo, 'checkout', '-b', 'develop')
    (repo / 'src/main.py').write_text('value = 3\n')
    git(repo, 'add', 'src/main.py')
    git(repo, 'commit', '-m', 'develop change')
    baseline = git(repo, 'rev-parse', 'HEAD').decode().strip()
    git(repo, 'checkout', 'main')
    revision = services.update_configuration(admin, project.pk, 1, {**policy, 'source_ref': 'temp/ai-qa-testing'})
    assert services.resolve_base_sha(repo, revision.source_ref) == baseline
    assert git(repo, 'symbolic-ref', '--short', 'HEAD').strip() == b'main'
    git(repo, 'update-ref', 'refs/heads/temp/ai-qa-testing', 'refs/heads/main')
    services.update_configuration(admin, project.pk, 2, {**policy, 'source_ref': 'temp/ai-qa-testing'})
    assert services.resolve_base_sha(repo, revision.source_ref) == services.resolve_base_sha(repo, 'main')


def test_configuration_form_round_trip_preserves_commands_when_only_branch_changes(client, admin, project):
    login(client, admin)
    url = reverse('projects:configure', args=[project.pk])
    response = client.get(url)
    form = response.context['form']
    data = {field.name: field.value() for field in form}
    data['source_ref'] = 'temp/ai-qa-testing'
    response = client.post(url, data)
    assert response.status_code == 302
    project.refresh_from_db()
    assert project.current_revision.source_ref == 'temp/ai-qa-testing'
    assert project.current_revision.verification_commands.get().command_id == 'unit'


def test_rejected_configuration_and_read_only_checks_do_not_create_branch(admin, manager, repo, project, policy):
    proposed = {**policy, 'source_ref': 'temp/ai-qa-testing'}
    fail('ACCESS_DENIED', services.update_configuration, manager, project.pk, 1, proposed)
    fail('VERSION_CONFLICT', services.update_configuration, admin, project.pk, 99, proposed)
    fail('INVALID_POLICY', services.update_configuration, admin, project.pk, 1, {**proposed, 'read_paths': ['src'], 'write_paths': ['tests']})
    assert git(repo, 'branch', '--list', 'temp/ai-qa-testing') == b''
    services.update_configuration(admin, project.pk, 1, proposed)
    git(repo, 'branch', '-D', 'temp/ai-qa-testing')
    fail('BASE_REF_NOT_FOUND', services.resolve_base_sha, repo, 'temp/ai-qa-testing')
    services.normalize_configuration(repo, 'temp/ai-qa-testing', proposed, project=project)
    assert git(repo, 'branch', '--list', 'temp/ai-qa-testing') == b''


def test_branch_name_prefix_collision_does_not_change_configuration(admin, repo, project, policy):
    git(repo, 'branch', 'temp')
    fail('BRANCH_CREATE_FAILED', services.update_configuration, admin, project.pk, 1, {**policy, 'source_ref': 'temp/ai-qa-testing'})
    project.refresh_from_db()
    assert project.project_config_version == 1
    assert project.current_revision.source_ref == 'main'
    assert git(repo, 'branch', '--list', 'temp/ai-qa-testing') == b''


@pytest.mark.parametrize("path", ["/", "relative/path"])
def test_broad_or_relative_repository_paths_rejected(path):
    fail("PATH_NOT_ALLOWED", services.validate_repository_path, path)


def test_home_runtime_and_nested_repo_paths_rejected(settings, repo, tmp_path):
    fail("PATH_NOT_ALLOWED", services.validate_repository_path, Path.home())
    settings.AIQA_DATA_DIR = repo / "runtime"
    fail("PATH_NOT_ALLOWED", services.validate_repository_path, repo)
    settings.AIQA_DATA_DIR = tmp_path / "independent-data"
    fail("INVALID_REPOSITORY", services.validate_repository_path, repo / "src")


def test_duplicate_repository_is_not_registered_twice(admin, project):
    fail("PROJECT_ALREADY_REGISTERED", services.register_project, admin, "중복", project.canonical_repo_path, "main")
    assert Project.objects.count() == 1


def test_manager_cannot_change_any_project_configuration(manager, project, policy):
    fail("ACCESS_DENIED", services.register_project, manager, "불가", project.canonical_repo_path, "main")
    fail("ACCESS_DENIED", services.update_configuration, manager, project.pk, 1, policy)
    fail("ACCESS_DENIED", services.set_project_state, manager, project.pk, "ARCHIVED", "거부 확인")
    assert project.config_revisions.count() == 1


def test_member_can_inspect_and_resolve_but_removed_membership_blocks(admin, manager, project):
    membership = ProjectMembership.objects.create(user=manager, project=project, granted_by=admin)
    assert services.inspect_project(manager, project.pk).source_ready
    assert services.resolve_execution_input(manager, project.pk).base_commit_sha
    membership.delete()
    fail("NOT_FOUND", services.inspect_project, manager, project.pk)
    fail("NOT_FOUND", services.resolve_execution_input, manager, project.pk)


@pytest.mark.parametrize("read_paths,write_paths", [(["src"], ["tests"]), (["src"], ["."]), (["../escape"], ["src"]), (["/tmp"], [])])
def test_invalid_read_write_paths_rejected(admin, project, policy, read_paths, write_paths):
    policy.update(read_paths=read_paths, write_paths=write_paths)
    with pytest.raises(DomainError):
        services.update_configuration(admin, project.pk, 1, policy)
    assert project.config_revisions.count() == 1


def test_symlink_policy_escape_rejected(admin, project, policy, repo, tmp_path):
    (repo / "outside").symlink_to(tmp_path)
    policy.update(read_paths=["outside"], write_paths=[])
    fail("PATH_NOT_ALLOWED", services.update_configuration, admin, project.pk, 1, policy)


def test_default_exclusions_and_read_write_policy_have_one_matcher(policy):
    assert services.file_allowed("src/main.py", policy)
    assert not services.file_allowed(".env.production", policy)
    assert not services.file_allowed("src/.env", policy)
    assert not services.file_allowed("src/credentials.json", policy)
    assert not services.file_allowed(".git/config", policy)
    assert not services.file_allowed("../outside", policy)
    policy["write_paths"] = ["src"]
    assert services.file_allowed("tests/test_main.py", policy)
    assert not services.file_allowed("tests/test_main.py", policy, write=True)


@pytest.mark.parametrize("updates", [
    {"argv": "pytest -q"}, {"argv": []}, {"argv": ["sh", "-c", "pytest"]},
    {"argv": ["/bin/bash", "-c", "pytest"]}, {"argv": ["env", "pytest"]},
    {"argv": ["pytest", "--token", "secret"]}, {"argv": ["pytest", "token=secret"]},
    {"argv": ["pytest", "$(touch sentinel)"]}, {"argv": ["pytest", "${REQUEST}"]},
    {"argv": ["pytest", "--output=/tmp/output"]}, {"cwd": "../outside"},
    {"timeout_sec": 0}, {"timeout_sec": 1801}, {"timeout_sec": True},
    {"parser_id": "arbitrary"}, {"parser_version": 2}, {"required": "true"},
    {"parser_id": ["pytest-summary"]}, {"parser_id": {"name": "pytest-summary"}},
    {"generated_paths": ["src"]}, {"unknown_setting": "yes"},
])
def test_invalid_commands_never_create_new_revision(admin, project, policy, updates):
    policy["verification_commands"][0].update(updates)
    with pytest.raises(DomainError):
        services.update_configuration(admin, project.pk, 1, policy)
    assert project.config_revisions.count() == 1


def test_original_repository_argument_is_rejected(admin, project, policy):
    policy["verification_commands"][0]["argv"] = ["pytest", project.canonical_repo_path]
    fail("INVALID_COMMAND", services.update_configuration, admin, project.pk, 1, policy)


def test_junit_requires_fixed_per_command_matching_output(admin, project, policy):
    command = policy["verification_commands"][0]
    command.update(parser_id="junit-xml", report_path=".aiqa-output/unit/junit.xml")
    fail("INVALID_COMMAND", services.update_configuration, admin, project.pk, 1, policy)
    command["argv"].append("--junitxml=.aiqa-output/unit/junit.xml")
    revision = services.update_configuration(admin, project.pk, 1, policy)
    assert revision.verification_commands.get().report_path == ".aiqa-output/unit/junit.xml"


def test_junit_report_argument_is_resolved_from_command_cwd(admin, project, policy):
    command = policy["verification_commands"][0]
    command.update(parser_id="junit-xml", cwd="tests", report_path=".aiqa-output/unit/junit.xml",
                   argv=["pytest", "-q", "--junitxml=../.aiqa-output/unit/junit.xml"])
    revision = services.update_configuration(admin, project.pk, 1, policy)
    saved = revision.verification_commands.get()
    assert saved.report_path == ".aiqa-output/unit/junit.xml"
    assert saved.argv[-1] == "--junitxml=../.aiqa-output/unit/junit.xml"
    command["argv"][-1] = "--junitxml=.aiqa-output/unit/junit.xml"
    fail("INVALID_COMMAND", services.update_configuration, admin, project.pk, 2, policy)


def test_reserved_committed_source_rejected_at_registration(admin, repo):
    (repo / ".aiqa-output").mkdir()
    (repo / ".aiqa-output" / "existing.txt").write_text("cannot hide source as output")
    git(repo, "add", ".aiqa-output")
    git(repo, "commit", "-m", "reserved fixture")
    fail("RESERVED_PATH", services.register_project, admin, "예약 충돌", str(repo), "main")
    assert not Project.objects.exists()


def test_archive_preserves_history_but_prevents_new_execution(admin, project):
    services.set_project_state(admin, project.pk, "ARCHIVED", "종료된 프로젝트")
    assert services.inspect_project(admin, project.pk).blocking_reasons == ["PROJECT_ARCHIVED"]
    fail("PROJECT_ARCHIVED", services.resolve_execution_input, admin, project.pk)
    assert project.config_revisions.count() == 1
    services.set_project_state(admin, project.pk, "ACTIVE", "작업 재개")
    assert services.resolve_execution_input(admin, project.pk).base_commit_sha


def test_running_job_blocks_policy_and_archive_but_queued_does_not(admin, project, policy):
    from apps.runs.models import Job

    job = Job.objects.create(project=project, actor=admin, kind="run.execute", subject_id="fixture",
        idempotency_key="project-busy", payload_hash="a" * 64, instance_id="test-instance", state="RUNNING")
    fail("PROJECT_BUSY", services.update_configuration, admin, project.pk, 1, policy)
    fail("PROJECT_BUSY", services.set_project_state, admin, project.pk, "ARCHIVED", "진행 중 거부")
    job.state = "QUEUED"
    job.save(update_fields=["state"])
    assert services.update_configuration(admin, project.pk, 1, policy).project_config_version == 2


def test_unconfirmed_process_blocks_policy_until_isolated_as_abandoned(admin, project, policy):
    from apps.runs.models import Job, ProcessRecord

    job = Job.objects.create(project=project, actor=admin, kind="conversation.respond", subject_id="fixture",
        idempotency_key="uncertain-process", payload_hash="b" * 64, instance_id="test-instance", state="CANCELLED")
    record = ProcessRecord.objects.create(job=job, stage="conversation", worker_identity="fixture",
        instance_id="test-instance", host_boot_id="fixture-boot", state="UNKNOWN")
    fail("PROJECT_BUSY", services.update_configuration, admin, project.pk, 1, policy)
    fail("PROJECT_BUSY", services.set_project_state, admin, project.pk, "ARCHIVED", "종료 확인 전 거부")
    record.state = "ABANDONED"
    record.save(update_fields=["state"])
    assert record.ended_at is None
    assert services.update_configuration(admin, project.pk, 1, policy).project_config_version == 2


def test_missing_committed_cwd_blocks_modification(admin, project, policy):
    policy["verification_commands"][0]["cwd"] = "missing-directory"
    services.update_configuration(admin, project.pk, 1, policy)
    health = services.inspect_project(admin, project.pk)
    assert "COMMAND_CWD_MISSING" in health.blocking_reasons
    assert health.source_ready and not health.modification_ready


def test_bundle_registration_copies_and_pins_content(admin, project, tmp_path, settings):
    settings.AIQA_DATA_DIR = tmp_path / "app-data"
    source = tmp_path / "wheel-source"
    source.mkdir()
    (source / "fixture.whl").write_bytes(b"offline dependency")
    bundle = services.register_dependency_bundle(admin, project.pk, str(source), "a" * 64, "macos-arm64", "python-3.11")
    stored = Path(bundle.registered_path)
    assert stored == settings.AIQA_DATA_DIR / "dependencies" / str(bundle.pk)
    assert bundle.manifest == [{"path": "fixture.whl", "size": 18, "sha256": hashlib.sha256(b"offline dependency").hexdigest()}]
    assert services.verify_dependency_bundle(bundle).pk == bundle.pk
    (source / "fixture.whl").write_bytes(b"external source changed")
    assert services.verify_dependency_bundle(bundle).pk == bundle.pk
    (stored / "fixture.whl").write_bytes(b"tampered registered bundle")
    fail("DEPENDENCY_CHANGED", services.verify_dependency_bundle, bundle)


def test_bundle_symlinks_rejected_without_copy(admin, project, tmp_path, settings):
    settings.AIQA_DATA_DIR = tmp_path / "app-data"
    source = tmp_path / "wheel-source"
    source.mkdir()
    (source / "escape.whl").symlink_to(Path(project.canonical_repo_path) / "src/main.py")
    fail("PATH_NOT_ALLOWED", services.register_dependency_bundle, admin, project.pk, str(source), "a" * 64, "macos-arm64", "python-3.11")
    assert not DependencyBundle.objects.exists()


def test_invalid_dependency_identifier_is_a_domain_error(admin, project, policy):
    policy["preparation_commands"] = [{"command_id": "prepare", "argv": ["python3", "-m", "venv", ".venv"],
                                        "dependency_bundle_id": "not-a-uuid"}]
    fail("DEPENDENCY_MISSING", services.update_configuration, admin, project.pk, 1, policy)
    assert project.config_revisions.count() == 1


def test_manager_screen_hides_repository_and_commands(client, admin, manager, project):
    ProjectMembership.objects.create(user=manager, project=project, granted_by=admin)
    login(client, manager)
    response = client.get(reverse("projects:detail", args=[project.pk]))
    assert response.status_code == 200
    text = response.content.decode()
    assert project.name in text
    assert project.canonical_repo_path not in text
    assert ".venv/bin/python" not in text
    assert "작업 정책·검증 명령" not in text
    assert client.get(reverse("projects:list")).status_code == 200
    assert client.get(reverse("projects:configure", args=[project.pk])).status_code == 403
    assert client.post(reverse("projects:state", args=[project.pk]), {"state": "ARCHIVED", "reason": "forged"}).status_code == 403
    project.refresh_from_db()
    assert project.state == "ACTIVE"


def test_admin_configuration_form_roundtrip(admin, project, client):
    login(client, admin)
    response = client.get(reverse("projects:configure", args=[project.pk]))
    assert response.status_code == 200
    policy = services.configuration_snapshot(project.current_revision)
    initial = ProjectConfigurationForm.initial_for(project, policy)
    form = ProjectConfigurationForm(initial)
    assert form.is_valid(), form.errors
    response = client.post(reverse("projects:configure", args=[project.pk]), {field.name: field.value() for field in response.context['form']})
    assert response.status_code == 302
    project.refresh_from_db()
    assert project.project_config_version == 2


def test_project_mutations_reject_get_and_missing_csrf(admin, project):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    login(client, admin)
    assert client.get(reverse("projects:state", args=[project.pk])).status_code == 405
    assert client.get(reverse("projects:inspect", args=[project.pk])).status_code == 405
    assert client.post(reverse("projects:state", args=[project.pk]), {"state": "ARCHIVED", "reason": "csrf"}).status_code == 403
    project.refresh_from_db()
    assert project.state == "ACTIVE"


def test_sensitive_command_content_does_not_echo_on_service_error(admin, project, policy):
    policy["verification_commands"][0]["argv"].append("--token=never-display-this-secret")
    with pytest.raises(DomainError) as caught:
        services.update_configuration(admin, project.pk, 1, policy)
    assert "never-display-this-secret" not in str(caught.value)


def test_sensitive_command_is_not_redisplayed_in_admin_error_form(client, admin, project):
    login(client, admin)
    policy = services.configuration_snapshot(project.current_revision)
    initial = ProjectConfigurationForm.initial_for(project, policy)
    policy["verification_commands"][0]["argv"].append("--token=never-echo-this-secret")
    initial["verification_commands"] = json.dumps(policy["verification_commands"])
    response = client.post(reverse("projects:configure", args=[project.pk]), initial)
    assert response.status_code == 400
    assert "never-echo-this-secret" not in response.content.decode()


def test_malformed_command_json_is_not_redisplayed(client, admin, project):
    login(client, admin)
    initial = ProjectConfigurationForm.initial_for(project, services.configuration_snapshot(project.current_revision))
    initial["verification_commands"] = '[{"argv":["pytest", "--token=malformed-secret"'
    response = client.post(reverse("projects:configure", args=[project.pk]), initial)
    assert response.status_code == 400
    assert "malformed-secret" not in response.content.decode()


def test_unreadable_registered_script_blocks_modification(admin, project, policy):
    policy.update(read_paths=["src"], write_paths=["src"])
    policy["verification_commands"][0]["argv"] = ["python3", "tests/test_main.py"]
    services.update_configuration(admin, project.pk, 1, policy)
    assert "COMMAND_SOURCE_NOT_READABLE" in services.inspect_project(admin, project.pk).blocking_reasons


def test_no_network_or_original_mutation_git_operations(monkeypatch, admin, project):
    original = subprocess.run
    observed = []

    def capture(argv, **kwargs):
        observed.append((argv, kwargs))
        return original(argv, **kwargs)

    monkeypatch.setattr(services.subprocess, "run", capture)
    services.inspect_project(admin, project.pk)
    assert observed
    for argv, kwargs in observed:
        assert kwargs["timeout"] == 10
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""
        assert "core.fsmonitor=false" in argv
        assert not kwargs.get("shell", False)
        assert not {"fetch", "pull", "push", "commit", "checkout", "reset", "stash", "clean"}.intersection(argv)


def test_repository_clean_filter_is_never_executed_by_inspection(admin, project, repo, tmp_path):
    marker = tmp_path / "filter-was-executed"
    (repo / ".gitattributes").write_text("*.py filter=fixture\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "filter fixture attributes")
    git(repo, "config", "filter.fixture.clean", "touch " + shlex.quote(str(marker)) + "; cat")
    git(repo, "config", "filter.fixture.required", "true")
    (repo / "src/main.py").write_text("value = 9\n")
    health = services.inspect_project(admin, project.pk)
    assert health.is_dirty
    assert not marker.exists()
    git(repo, "status", "--porcelain=v1")
    assert marker.exists(), "fixture must exercise Git clean-filter execution"


def test_registration_rechecks_admin_authority_after_git_inspection(admin, repo, policy, monkeypatch):
    normalize = services.normalize_configuration

    def revoke_after_inspection(*args, **kwargs):
        result = normalize(*args, **kwargs)
        get_user_model().objects.filter(pk=admin.pk).update(role="MANAGER")
        return result

    monkeypatch.setattr(services, "normalize_configuration", revoke_after_inspection)
    fail("ACCESS_DENIED", services.register_project, admin, "권한 회수 검사", str(repo), "main", policy=policy)
    assert not Project.objects.exists()


def test_invalid_parser_json_returns_bad_request_not_server_error(client, admin, project):
    login(client, admin)
    policy = services.configuration_snapshot(project.current_revision)
    policy["verification_commands"][0]["parser_id"] = ["pytest-summary"]
    initial = ProjectConfigurationForm.initial_for(project, policy)
    assert client.post(reverse("projects:configure", args=[project.pk]), initial).status_code == 400
    assert project.config_revisions.count() == 1
