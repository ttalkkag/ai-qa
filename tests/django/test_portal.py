from html import unescape
import re

import pytest
from django.urls import reverse

from tests.django.test_projects import admin, assert_current_menu, login, manager


pytestmark = pytest.mark.django_db


def main_content(response):
    return re.search(r'<main class="site-main container">(.*?)</main>', response.content.decode(), re.S).group(1)


@pytest.mark.parametrize("actor_fixture,labels", [
    ("admin", ["Project", "Request", "AI", "Admin", "Docs", "Account"]),
    ("manager", ["Project", "Request", "Docs", "Account"]),
])
def test_header_menu_order_and_icon_accessibility(client, request, actor_fixture, labels):
    login(client, request.getfixturevalue(actor_fixture))
    response = client.get(reverse("projects:list"))
    header = re.search(r'<header class="site-header">(.*?)</header>', response.content.decode(), re.S).group(1)
    menu = re.search(r'<nav aria-label="주 메뉴">(.*?)</nav>', header, re.S).group(1)
    links = re.findall(r'<a\b([^>]*)>(.*?)</a>', menu, re.S)
    actual_labels = []
    for attributes, body in links:
        label = re.search(r'aria-label="([^"]+)"', attributes)
        actual_labels.append(unescape(label.group(1)) if label else re.sub(r'<[^>]+>', '', body).strip())
    assert actual_labels == labels
    for label, route in [("Docs", "portal:docs"), ("Account", "accounts:me")]:
        attributes, body = next((attrs, body) for attrs, body in links if f'aria-label="{label}"' in attrs)
        assert f'title="{label}"' in attributes
        assert f'href="{reverse(route)}"' in attributes
        assert re.search(r'<svg\b[^>]*aria-hidden="true"', body)
        assert re.sub(r'<[^>]+>', '', body).strip() == ""
    assert reverse("accounts:logout") not in header


@pytest.mark.parametrize("view_name", ["portal:admin", "portal:docs"])
def test_portal_pages_require_login(client, view_name):
    url = reverse(view_name)
    response = client.get(url)
    assert response.status_code == 302
    assert response.url == f'{reverse("accounts:login")}?next={url}'


def test_admin_hub_connects_management_pages(client, admin):
    login(client, admin)
    response = client.get(reverse("portal:admin"))
    assert response.status_code == 200
    assert_current_menu(response.content.decode(), reverse("portal:admin"))
    content = main_content(response)
    links = re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', content, re.S)
    assert links == [
        (reverse("approvals:list"), "요청 승인"),
        (reverse("accounts:manage"), "계정 관리"),
        (reverse("operations:dashboard"), "운영 상태"),
    ]
    assert "사용자가 확인한 로컬 결과 승인 · 원격 발행은 별도" in content
    assert 'aria-label="관리 기능"' in content
    for url, _ in links:
        assert client.get(url).status_code == 200


def test_manager_cannot_open_admin_hub_even_with_django_staff_flags(client, manager):
    manager.is_staff = True
    manager.is_superuser = True
    manager.save(update_fields=["is_staff", "is_superuser"])
    login(client, manager)
    response = client.get(reverse("portal:admin"))
    assert response.status_code == 403
    for route in ["accounts:manage", "operations:dashboard", "ai:settings"]:
        assert f'href="{reverse(route)}"' not in response.content.decode()


def test_invalid_account_creation_keeps_form_open_and_input_visible(client, admin):
    login(client, admin)
    response = client.post(reverse("accounts:manage"), {"action": "create", "username": "unfinished"})
    assert response.status_code == 400
    content = main_content(response)
    assert '<details class="account-create" open>' in content
    assert 'value="unfinished"' in content
    assert 'class="errorlist"' in content


@pytest.mark.parametrize("actor_fixture", ["admin", "manager"])
def test_docs_explains_actual_manager_workflow_and_links_to_accessible_pages(client, request, actor_fixture):
    login(client, request.getfixturevalue(actor_fixture))
    response = client.get(reverse("portal:docs"))
    assert response.status_code == 200
    assert_current_menu(response.content.decode(), reverse("portal:docs"))
    content = main_content(response)
    anchors = re.findall(r'href="#([^"]+)"', content)
    assert anchors == ["start", "conversation", "request", "progress", "result", "account"] + (["project-setup"] if actor_fixture == "admin" else [])
    for anchor in anchors:
        assert content.count(f'id="{anchor}"') == 1
    for text in ["대화 시작", "요청으로 정리", "초안 저장", "이 명세로 준비", "수정 작업 시작", "취소 요청", "결과 확인", "비밀번호 변경", "로그아웃"]:
        assert text in content
    assert "비밀번호는 6자 이상" in content
    assert "매니저에게 발행 권한은 없습니다" in content
    assert "로컬 승인은 push·PR을 실행하지 않습니다" in content
    urls = set(re.findall(r'href="(/[^"#]+)"', content))
    expected_urls = {reverse("projects:list"), reverse("work_requests:list"), reverse("accounts:me")}
    if actor_fixture == "admin":
        expected_urls |= {reverse("projects:create"), reverse("accounts:manage"), reverse("ai:settings")}
        assert 'GitHub URL이 아닌' in content
        assert content.index('GitHub URL이 아닌') < content.index('macOS:') < content.index('Windows:')
        assert '<li>macOS: <code>/Users/your-name/Projects/sample-board</code></li>' in content
        assert r'<li>Windows: <code>C:\Users\your-name\Projects\sample-board</code></li>' in content
        assert '경로 이름으로 복사' not in content
        assert '주소 표시줄' not in content
        assert 'Windows 서버 실행은 미검증' not in content
        assert '등록 후 할 일' in content
    else:
        assert 'project-setup' not in content
    assert urls == expected_urls
    for url in urls:
        assert client.get(url).status_code == 200


@pytest.mark.parametrize("view_name", ["portal:admin", "portal:docs"])
def test_portal_pages_do_not_accept_post(client, admin, view_name):
    login(client, admin)
    response = client.post(reverse(view_name), {})
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
