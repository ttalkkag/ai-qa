from html import unescape
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.template.loader import render_to_string

from apps.accounts.models import ProjectMembership
from apps.approvals.models import Publication, PublicationPolicy
from apps.conversations.models import Conversation
from apps.projects.models import ProjectConfigRevision
from apps.runs.models import Job
from apps.work_requests.models import WorkRequest
from tests.django.test_artifacts_local import create_bundle, setup as result_domain
from tests.django.test_projects import admin, login, manager, policy, project, repo


pytestmark = pytest.mark.django_db


def test_page_templates_do_not_repeat_top_level_titles(settings):
    templates = Path(settings.BASE_DIR) / 'templates'
    with_titles = [str(path.relative_to(templates)) for path in templates.rglob('*.html')
                   if re.search(r'<h1\b', path.read_text(), re.I)]
    assert with_titles == []


@pytest.fixture
def thread(admin, manager, project):
    ProjectMembership.objects.create(user=manager, project=project, granted_by=admin)
    conversation = Conversation.objects.create(project=project, owner=manager, title="게시판 검색", creation_key="navigation", creation_hash="a" * 64)
    work_request = WorkRequest.objects.create(project=project, owner=manager, conversation=conversation, draft={"title": "검색 기능 추가"})
    return SimpleNamespace(project=project, conversation=conversation, request=work_request, owner=manager)


def location_links(content):
    match = re.search(r'<nav class="site-navigation" aria-label="현재 위치">(.*?)</nav>', content, re.S)
    assert match is not None
    return [(unescape(label), unescape(url)) for url, label in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', match.group(1), re.S)]


@pytest.mark.parametrize("actor_fixture,body_text", [
    ("admin", "프로젝트를 연결하고 시작하세요."),
    ("manager", "할당된 프로젝트가 없습니다. 관리자에게 문의하세요."),
    (None, 'aria-label="로그인"'),
])
def test_page_regions_are_separate_ordered_body_children(client, request, actor_fixture, body_text):
    if actor_fixture:
        login(client, request.getfixturevalue(actor_fixture))
    response = client.get(reverse("projects:list"), follow=True)
    assert response.status_code == 200
    content = response.content.decode()
    regions = re.search(
        r'<body>\s*<header class="site-header">(?P<header>.*?)</header>\s*'
        r'<nav class="site-navigation" aria-label="현재 위치">(?P<navigation>.*?)</nav>\s*'
        r'<div class="site-notifications"[^>]* hidden>.*?</div>\s*</div>\s*'
        r'<main class="site-main container(?: login-main| project-empty-main)?">(?P<main>.*?)</main>\s*'
        r'<footer class="site-footer">(?P<footer>.*?)</footer>',
        content, re.S,
    )
    assert regions is not None
    assert '<a class="brand" href="/projects/">' in regions["header"]
    if actor_fixture:
        assert '<nav aria-label="주 메뉴">' in regions["header"]
        assert 'href="/projects/"' in regions["header"]
        assert 'href="/accounts/me/"' in regions["header"]
    else:
        assert 'aria-label="주 메뉴"' not in regions["header"]
    assert '<div class="container navigation-content">' in regions["navigation"]
    assert '<ol class="breadcrumbs">' in regions["navigation"]
    assert 'aria-current="page"' in regions["navigation"]
    assert 'aria-label="현재 위치"' not in regions["main"]
    assert 'class="page-header"' not in content
    assert re.search(r'<div class="site-notifications"[^>]* hidden>', content)
    assert body_text in regions["main"]
    assert "© kimchanhyung98" in regions["footer"]
    assert 'href="https://git.chanhyung.kim"' in regions["footer"]
    assert 'href="https://linkedin.chanhyung.kim"' in regions["footer"]


def test_registration_notice_is_between_navigation_and_main(client, admin, repo):
    login(client, admin)
    response = client.post(reverse('projects:create'), {'name': '알림 테스트', 'local_path': str(repo)}, follow=True)
    assert response.status_code == 200
    content = response.content.decode()
    notice = '프로젝트를 등록했습니다. 수정 실행 전에 필수 검증 명령을 등록하세요.'
    assert content.count(notice) == 1
    assert re.search(r'</nav>\s*<div class="site-notifications"[^>]*>.*?</div>\s*</div>\s*<main', content, re.S)
    assert 'aria-label="알림 닫기"' in content
    assert 'js/notifications.js' in content
    main = re.search(r'<main\b[^>]*>(.*?)</main>', content, re.S).group(1)
    assert notice not in main
    assert content.index('class="site-header"') < content.index('class="site-navigation"') < content.index('class="site-notifications"') < content.index('class="site-main')
    refreshed = client.get(response.request['PATH_INFO']).content.decode()
    assert re.search(r'<div class="site-notifications"[^>]* hidden>', refreshed)


def test_notification_region_escapes_and_displays_each_message():
    content = render_to_string('includes/notifications.html', {'messages': ['<script>alert(1)</script>', '두 번째 알림']})
    assert '<script>' not in content
    assert '&lt;script&gt;' in content
    assert content.count('<p>') == 2
    assert '두 번째 알림' in content
    assert re.search(r'<div class="site-notifications"[^>]* hidden>', render_to_string('includes/notifications.html', {'messages': []}))


@pytest.mark.parametrize("view_name,label", [
    ("work_requests:list", "Request"),
    ("accounts:me", "Account"),
    ("ai:settings", "AI"),
    ("portal:admin", "Admin"),
    ("portal:docs", "Docs"),
])
def test_top_level_location_matches_its_page(client, admin, view_name, label):
    login(client, admin)
    response = client.get(reverse(view_name))
    assert response.status_code == 200
    assert location_links(response.content.decode()) == [(label, reverse(view_name))]


@pytest.mark.parametrize("view_name,label", [
    ("accounts:manage", "Users"),
    ("approvals:list", "Approvals"),
    ("operations:dashboard", "Operations"),
])
def test_admin_subpage_location_links_to_admin_hub(client, admin, view_name, label):
    login(client, admin)
    response = client.get(reverse(view_name))
    assert response.status_code == 200
    links = location_links(response.content.decode())
    assert links == [("Admin", reverse("portal:admin")), (label, reverse(view_name))]
    for _, url in links:
        assert client.get(url).status_code == 200


def test_conversation_location_has_clickable_project_and_list(client, thread):
    login(client, thread.owner)
    response = client.get(reverse("conversations:detail", args=[thread.conversation.pk]))
    assert response.status_code == 200
    links = location_links(response.content.decode())
    assert links == [
        ("Project", reverse("projects:list")),
        (thread.project.name, reverse("projects:detail", args=[thread.project.pk])),
        ("Conversations", reverse("conversations:list", args=[thread.project.pk])),
        (thread.conversation.title, reverse("conversations:detail", args=[thread.conversation.pk])),
    ]
    for _, url in links:
        assert client.get(url).status_code == 200


def test_result_location_links_to_parent_request_and_run(client, result_domain):
    ProjectConfigRevision.objects.create(project=result_domain["project"], project_config_version=1,
        canonical_repo_path=result_domain["project"].canonical_repo_path, source_ref="main", created_by=result_domain["admin"])
    bundle = create_bundle(result_domain)
    login(client, result_domain["manager"])
    response = client.get(reverse("artifacts:detail", args=[bundle.pk]))
    assert response.status_code == 200
    work_request = bundle.request_revision.request
    links = location_links(response.content.decode())
    assert links == [
        ("Request", reverse("work_requests:list")),
        (work_request.title, reverse("work_requests:detail", args=[work_request.pk])),
        ("Run", reverse("runs:detail", args=[bundle.run_id])),
        ("Result", reverse("artifacts:detail", args=[bundle.pk])),
    ]
    for _, url in links:
        assert client.get(url).status_code == 200
    run_response = client.get(reverse("runs:detail", args=[bundle.run_id]))
    assert location_links(run_response.content.decode()) == links[:-1]


def test_password_location_links_to_own_account(client, manager):
    login(client, manager)
    response = client.get(reverse("accounts:password"))
    assert location_links(response.content.decode()) == [
        ("Account", reverse("accounts:me")), ("Password", reverse("accounts:password")),
    ]


def test_manager_publication_list_location_stays_under_my_requests(client, manager):
    login(client, manager)
    response = client.get(reverse("approvals:list"))
    assert location_links(response.content.decode()) == [
        ("Request", reverse("work_requests:list")), ("Publication", reverse("approvals:list")),
    ]


def test_publication_preparation_location_uses_safe_get_url(client, admin, project):
    job = Job.objects.create(actor=admin, project=project, kind="publication.prepare", subject_id="00000000-0000-0000-0000-000000000001", state="FAILED",
        idempotency_key="navigation-prepare", payload_hash="a" * 64, instance_id="test")
    login(client, admin)
    response = client.get(reverse("approvals:preparation-status", args=[job.pk]))
    assert response.status_code == 200
    assert location_links(response.content.decode()) == [
        ("Admin", reverse("portal:admin")),
        ("Approvals", reverse("approvals:list")),
        ("Prepare Publication", reverse("approvals:preparation-status", args=[job.pk])),
    ]


@pytest.mark.parametrize("actor_key", ["admin", "manager"])
def test_publication_detail_location_follows_role_specific_parent(client, result_domain, actor_key):
    ProjectConfigRevision.objects.create(project=result_domain["project"], project_config_version=1,
        canonical_repo_path=result_domain["project"].canonical_repo_path, source_ref="main", created_by=result_domain["admin"])
    bundle = create_bundle(result_domain)
    policy = PublicationPolicy.objects.create(project=result_domain["project"], updated_by=result_domain["admin"])
    publication = Publication.objects.create(project=result_domain["project"], run=bundle.run, bundle=bundle,
        policy=policy, snapshot={}, snapshot_hash="a" * 64, created_by=result_domain["admin"])
    login(client, result_domain[actor_key])
    response = client.get(reverse("approvals:detail", args=[publication.pk]))
    assert response.status_code == 200
    work_request = bundle.request_revision.request
    if actor_key == "admin":
        parent_links = [("Admin", reverse("portal:admin")), ("Approvals", reverse("approvals:list"))]
    else:
        parent_links = [("Request", reverse("work_requests:list")), (work_request.title, reverse("work_requests:detail", args=[work_request.pk]))]
    links = location_links(response.content.decode())
    assert links == parent_links + [("Review Publication", reverse("approvals:detail", args=[publication.pk]))]
    for _, url in links:
        assert client.get(url).status_code == 200


@pytest.mark.parametrize("view_name", ["projects:create", "accounts:manage", "ai:settings", "operations:dashboard", "portal:admin"])
def test_forbidden_admin_page_location_only_links_to_accessible_root(client, manager, view_name):
    login(client, manager)
    response = client.get(reverse(view_name))
    assert response.status_code == 403
    assert location_links(response.content.decode()) == [("Project", reverse("projects:list"))]
    assert '<span aria-current="page">Notice</span>' in response.content.decode()


@pytest.mark.parametrize("target", ["project", "conversation", "request"])
def test_revoked_membership_location_does_not_expose_object_names(client, thread, target):
    ProjectMembership.objects.filter(user=thread.owner, project=thread.project).delete()
    route = {"project": "projects:detail", "conversation": "conversations:detail", "request": "work_requests:detail"}[target]
    login(client, thread.owner)
    response = client.get(reverse(route, args=[getattr(thread, target).pk]))
    assert response.status_code in {403, 404}
    root = ("Request", "work_requests:list") if target == "request" else ("Project", "projects:list")
    assert location_links(response.content.decode()) == [(root[0], reverse(root[1]))]
    assert '<span aria-current="page">Notice</span>' in response.content.decode()
    assert thread.project.name not in response.content.decode()
    assert thread.conversation.title not in response.content.decode()
    assert thread.request.title not in response.content.decode()


@pytest.mark.parametrize("target,action,detail", [
    ("conversation", "conversations:message", "conversations:detail"),
    ("request", "work_requests:draft", "work_requests:detail"),
])
def test_invalid_post_location_links_to_canonical_get_page(client, thread, target, action, detail):
    login(client, thread.owner)
    obj = getattr(thread, target)
    response = client.post(reverse(action, args=[obj.pk]), {})
    assert response.status_code == 400
    links = location_links(response.content.decode())
    assert links[-1] == (obj.title, reverse(detail, args=[obj.pk]))
    for _, url in links:
        assert client.get(url).status_code == 200


def test_location_escapes_user_entered_names(client, thread):
    thread.conversation.title = '<img src=x onerror="alert(1)"> & 게시판'
    thread.conversation.save(update_fields=["title"])
    login(client, thread.owner)
    response = client.get(reverse("conversations:detail", args=[thread.conversation.pk]))
    breadcrumb = re.search(r'<nav class="site-navigation" aria-label="현재 위치">(.*?)</nav>', response.content.decode(), re.S).group(1)
    assert '<img' not in breadcrumb
    assert '&lt;img' in breadcrumb
    assert location_links(response.content.decode())[-1][0] == thread.conversation.title
