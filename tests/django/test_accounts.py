from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
import re
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import AccountAudit, ProjectMembership
from apps.conversations.models import Conversation
from apps.errors import DomainError
from apps.projects.models import Project


pytestmark = pytest.mark.django_db

PASSWORD = "Local-Testing-Password-9274"
NEW_PASSWORD = "Changed-Testing-Password-6382"


@pytest.fixture
def user_factory():
    def create(username, role="MANAGER", **kwargs):
        return get_user_model().objects.create_user(
            username=username,
            password=PASSWORD,
            display_name=username,
            role=role,
            **kwargs,
        )

    return create


@pytest.fixture
def admin(user_factory):
    return user_factory("admin", role="ADMIN")


@pytest.fixture
def manager(user_factory):
    return user_factory("manager")


@pytest.fixture
def project(admin, tmp_path):
    return Project.objects.create(
        name="Example", canonical_repo_path=str(tmp_path), created_by=admin
    )


@pytest.fixture
def assigned_project(admin, manager, project):
    ProjectMembership.objects.create(
        user=manager, project=project, granted_by=admin
    )
    return project


def assert_error(code, operation, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def session_login(client, user, issued_at=None):
    client.force_login(user)
    session = client.session
    session["authz_version"] = user.authz_version
    session["issued_at"] = (
        timezone.now().timestamp() if issued_at is None else issued_at
    )
    session.save()


def test_unauthenticated_and_inactive_accounts_are_rejected(manager):
    assert_error("AUTH_REQUIRED", services.require_access, AnonymousUser(), "project.view")
    manager.is_active = False
    manager.save(update_fields=["is_active"])
    assert_error("AUTH_REQUIRED", services.require_access, manager, "project.view")


@pytest.mark.parametrize(
    "action",
    [
        "project.manage",
        "account.manage",
        "model.manage",
        "publication.approve",
        "publication.publish",
        "publication.reject",
    ],
)
def test_manager_cannot_call_administrative_actions(manager, action):
    assert_error("ACCESS_DENIED", services.require_access, manager, action)


@pytest.mark.parametrize(
    "action",
    ["project.manage", "account.manage", "model.manage", "publication.approve"],
)
def test_admin_can_call_administrative_actions(admin, action):
    assert services.require_access(admin, action).pk == admin.pk


def test_access_reloads_current_role_instead_of_trusting_actor_instance(admin, user_factory):
    user_factory("second-admin", role="ADMIN")
    get_user_model().objects.filter(pk=admin.pk).update(role="MANAGER")
    assert admin.role == "ADMIN"
    assert_error("ACCESS_DENIED", services.require_access, admin, "project.manage")


def test_django_staff_or_superuser_flags_do_not_grant_product_role(manager):
    manager.is_staff = True
    manager.is_superuser = True
    manager.save(update_fields=["is_staff", "is_superuser"])
    assert_error("ACCESS_DENIED", services.require_access, manager, "publication.publish")


def test_unknown_action_is_not_implicitly_allowed(admin):
    assert_error("ACCESS_DENIED", services.require_access, admin, "unknown.execute")


@pytest.mark.parametrize("owner_lookup", ["", "owner__is_active", "project__created_by"])
def test_owned_queryset_rejects_unregistered_owner_lookup(admin, owner_lookup):
    error = assert_error(
        "ACCESS_DENIED",
        services.filter_owned_queryset,
        admin,
        Conversation.objects.all(),
        owner_lookup=owner_lookup,
    )
    assert error.status == 403
    assert not AccountAudit.objects.exists()


def test_owned_queryset_uses_current_role_and_default_owner_scope(admin, manager, project, tmp_path, user_factory):
    remaining_admin = user_factory("queryset-admin", role="ADMIN")
    private_project = Project.objects.create(
        name="Unassigned", canonical_repo_path=str(tmp_path / "unassigned"), created_by=remaining_admin
    )
    ProjectMembership.objects.create(user=admin, project=project, granted_by=remaining_admin)
    own_assigned = Conversation.objects.create(
        project=project, owner=admin, creation_key="own-assigned", creation_hash="a" * 64, title="Owned and assigned"
    )
    other_assigned = Conversation.objects.create(
        project=project, owner=manager, creation_key="other-assigned", creation_hash="b" * 64, title="Other owner"
    )
    own_unassigned = Conversation.objects.create(
        project=private_project, owner=admin, creation_key="own-unassigned", creation_hash="c" * 64, title="Unassigned project"
    )
    assert set(services.filter_owned_queryset(admin, Conversation.objects.all()).values_list("pk", flat=True)) == {
        own_assigned.pk,
        other_assigned.pk,
        own_unassigned.pk,
    }
    audit_count = AccountAudit.objects.filter(actor=admin, action="access.list").count()
    assert audit_count == 1

    get_user_model().objects.filter(pk=admin.pk).update(role="MANAGER")
    assert admin.role == "ADMIN"
    assert list(services.filter_owned_queryset(admin, Conversation.objects.all()).values_list("pk", flat=True)) == [
        own_assigned.pk
    ]
    assert AccountAudit.objects.filter(actor=admin, action="access.list").count() == audit_count


def test_project_membership_filters_projects(admin, manager, assigned_project, tmp_path):
    other = Project.objects.create(
        name="Private", canonical_repo_path=str(tmp_path / "other"), created_by=admin
    )
    assert set(services.list_accessible_projects(manager).values_list("pk", flat=True)) == {
        assigned_project.pk
    }
    assert set(services.list_accessible_projects(admin).values_list("pk", flat=True)) == {
        assigned_project.pk,
        other.pk,
    }
    assert_error(
        "NOT_FOUND", services.require_access, manager, "project.view", project_id=other.pk
    )


@pytest.mark.parametrize(
    "action",
    ["conversation.view", "request.view", "run.view", "artifact.view", "event.view"],
)
def test_manager_can_only_read_owned_objects(manager, assigned_project, user_factory, action):
    other = user_factory("other-manager")
    own_object = SimpleNamespace(owner_id=manager.pk, project_id=assigned_project.pk)
    other_object = SimpleNamespace(owner_id=other.pk, project_id=assigned_project.pk)
    assert services.require_access(
        manager, action, project_id=assigned_project.pk, obj=own_object
    ).pk == manager.pk
    assert_error(
        "NOT_FOUND",
        services.require_access,
        manager,
        action,
        project_id=assigned_project.pk,
        obj=other_object,
    )


def test_admin_can_read_other_owners_object(admin, manager, project):
    obj = SimpleNamespace(owner_id=manager.pk, project_id=project.pk)
    assert services.require_access(admin, "request.view", project_id=project.pk, obj=obj).pk == admin.pk
    assert AccountAudit.objects.filter(
        actor=admin, subject=manager, project=project, action="request.view"
    ).exists()


def test_callers_cannot_substitute_project_scope(admin, manager, assigned_project, tmp_path):
    private_project = Project.objects.create(
        name="Not assigned", canonical_repo_path=str(tmp_path / "private"), created_by=admin
    )
    obj = SimpleNamespace(owner_id=manager.pk, project_id=private_project.pk)
    assert_error(
        "NOT_FOUND", services.require_access, manager, "request.view", project_id=assigned_project.pk, obj=obj
    )


def test_object_action_cannot_omit_target(manager, assigned_project):
    assert_error(
        "NOT_FOUND", services.require_access, manager, "request.view", project_id=assigned_project.pk
    )


@pytest.mark.parametrize(
    "action",
    ["conversation.edit", "request.edit", "request.confirm", "request.submit", "request.retry", "run.cancel"],
)
def test_admin_cannot_impersonate_other_owner(admin, manager, project, action):
    obj = SimpleNamespace(owner_id=manager.pk, project_id=project.pk)
    error = assert_error(
        "NOT_FOUND", services.require_access, admin, action, project_id=project.pk, obj=obj
    )
    assert error.status == 404


def test_membership_revocation_hides_previously_owned_objects(admin, manager, assigned_project):
    obj = SimpleNamespace(owner_id=manager.pk, project_id=assigned_project.pk)
    old_version = manager.authz_version
    services.set_membership(admin, manager.pk, assigned_project.pk, False, "assignment ended")
    manager.refresh_from_db()
    assert manager.authz_version == old_version + 1
    assert not ProjectMembership.objects.filter(user=manager, project=assigned_project).exists()
    assert_error(
        "NOT_FOUND", services.require_access, manager, "artifact.view", project_id=assigned_project.pk, obj=obj
    )


def test_create_account_requires_initial_password_change_and_never_audits_password(admin):
    created = services.create_account(admin, "new-manager", "New Manager", "MANAGER", PASSWORD)
    assert created.check_password(PASSWORD)
    assert created.password != PASSWORD
    assert created.must_change_password is True
    assert created.role == "MANAGER"
    assert AccountAudit.objects.exists()
    audit = repr(list(AccountAudit.objects.values()))
    assert PASSWORD not in audit
    assert created.password not in audit


@pytest.mark.parametrize("password", ["123456", "abcdef", "new-manager"])
def test_create_account_accepts_passwords_of_at_least_six_characters(admin, password):
    created = services.create_account(admin, "new-manager", "New Manager", "MANAGER", password)
    assert created.check_password(password)
    assert created.must_change_password is True


def test_create_account_rejects_password_shorter_than_six_characters(admin):
    assert_error("WEAK_PASSWORD", services.create_account, admin, "too-short", "Too Short", "MANAGER", "12345")
    assert not get_user_model().objects.filter(username="too-short").exists()


def test_initial_password_blocks_domain_access_not_only_html_menu(admin):
    created = services.create_account(admin, "initial-blocked", "Initial", "ADMIN", PASSWORD)
    assert_error("PASSWORD_CHANGE_REQUIRED", services.require_access, created, "account.manage")
    assert_error("PASSWORD_CHANGE_REQUIRED", services.list_accessible_projects, created)


def test_duplicate_username_does_not_replace_existing_account(admin, manager):
    assert_error(
        "USERNAME_TAKEN", services.create_account, admin, manager.username, "Replacement", "ADMIN", NEW_PASSWORD
    )
    manager.refresh_from_db()
    assert manager.role == "MANAGER"
    assert manager.check_password(PASSWORD)


def test_create_account_rejects_unknown_role(admin):
    assert_error(
        "INVALID_INPUT", services.create_account, admin, "invalid-role", "Invalid", "OWNER", PASSWORD
    )
    assert not get_user_model().objects.filter(username="invalid-role").exists()


def test_account_change_is_versioned_and_conflicting_change_has_no_effect(admin, manager):
    old_version = manager.authz_version
    changed = services.change_account(
        admin, manager.pk, old_version, {"role": "ADMIN"}, "new administrator"
    )
    assert changed.role == "ADMIN"
    assert changed.authz_version == old_version + 1
    assert_error(
        "VERSION_CONFLICT",
        services.change_account,
        admin,
        manager.pk,
        old_version,
        {"is_active": False},
        "stale form",
    )
    manager.refresh_from_db()
    assert manager.is_active is True
    assert manager.role == "ADMIN"


@pytest.mark.parametrize("changes", [{"role": "MANAGER"}, {"is_active": False}])
def test_last_active_admin_is_protected(admin, changes):
    old_version = admin.authz_version
    assert_error(
        "LAST_ADMIN_REQUIRED", services.change_account, admin, admin.pk, old_version, changes, "remove access"
    )
    admin.refresh_from_db()
    assert admin.role == "ADMIN"
    assert admin.is_active is True
    assert admin.authz_version == old_version


def test_inactive_admin_does_not_satisfy_last_admin_requirement(admin, user_factory):
    user_factory("inactive-admin", role="ADMIN", is_active=False)
    assert_error(
        "LAST_ADMIN_REQUIRED",
        services.change_account,
        admin,
        admin.pk,
        admin.authz_version,
        {"is_active": False},
        "remove access",
    )


def test_non_last_admin_can_demote_self(admin, user_factory):
    remaining_admin = user_factory("remaining-admin", role="ADMIN")
    changed = services.change_account(
        admin, admin.pk, admin.authz_version, {"role": "MANAGER"}, "handover administration"
    )
    assert changed.role == "MANAGER"
    assert_error("ACCESS_DENIED", services.require_access, admin, "account.manage")
    assert services.require_access(remaining_admin, "account.manage").pk == remaining_admin.pk


@pytest.mark.django_db(transaction=True)
def test_concurrent_demotion_cannot_remove_all_active_admins(user_factory):
    first = user_factory("concurrent-first", role="ADMIN")
    second = user_factory("concurrent-second", role="ADMIN")
    barrier = Barrier(2)

    def demote(user_id):
        close_old_connections()
        try:
            actor = get_user_model().objects.get(pk=user_id)
            barrier.wait(timeout=10)
            try:
                services.change_account(
                    actor, actor.pk, actor.authz_version, {"role": "MANAGER"}, "concurrent demotion"
                )
                return "changed"
            except DomainError as error:
                return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(demote, [first.pk, second.pk]))
    assert results.count("changed") <= 1
    assert set(results) <= {"changed", "VERSION_CONFLICT", "LAST_ADMIN_REQUIRED"}
    assert get_user_model().objects.filter(role="ADMIN", is_active=True).exists()


def test_membership_grant_is_unique_and_records_actor(admin, manager, project):
    services.set_membership(admin, manager.pk, project.pk, True, "new assignment")
    services.set_membership(admin, manager.pk, project.pk, True, "repeat assignment")
    membership = ProjectMembership.objects.get(user=manager, project=project)
    assert membership.granted_by_id == admin.pk
    assert ProjectMembership.objects.filter(user=manager, project=project).count() == 1
    assert AccountAudit.objects.filter(actor=admin).exists()


def test_password_change_requires_current_password(manager):
    assert_error(
        "INVALID_CREDENTIALS", services.change_password, manager, "wrong-password", NEW_PASSWORD
    )
    manager.refresh_from_db()
    assert manager.check_password(PASSWORD)


@pytest.mark.parametrize("password", ["", "12345"])
def test_password_change_rejects_weak_password(manager, password):
    assert_error("WEAK_PASSWORD", services.change_password, manager, PASSWORD, password)
    manager.refresh_from_db()
    assert manager.check_password(PASSWORD)


@pytest.mark.parametrize("new_password", [NEW_PASSWORD, "123456"])
def test_password_change_clears_initial_password_requirement(admin, new_password):
    manager = services.create_account(admin, "initial-user", "Initial", "MANAGER", PASSWORD)
    services.change_password(manager, PASSWORD, new_password)
    manager.refresh_from_db()
    assert manager.check_password(new_password)
    assert not manager.check_password(PASSWORD)
    assert manager.must_change_password is False


def test_initial_password_cannot_be_reused_to_bypass_forced_change(admin):
    manager = services.create_account(admin, "initial-reuse", "Initial", "MANAGER", PASSWORD)
    assert_error("WEAK_PASSWORD", services.change_password, manager, PASSWORD, PASSWORD)
    manager.refresh_from_db()
    assert manager.must_change_password is True


@pytest.mark.parametrize("new_password", [NEW_PASSWORD, "123456"])
def test_password_reset_requires_change_and_does_not_audit_secret(admin, manager, new_password):
    services.reset_password(admin, manager.pk, new_password, "user requested reset")
    manager.refresh_from_db()
    assert manager.check_password(new_password)
    assert manager.must_change_password is True
    audit = repr(list(AccountAudit.objects.values()))
    assert new_password not in audit
    assert manager.password not in audit


def test_password_reset_rejects_password_shorter_than_six_characters(admin, manager):
    assert_error("WEAK_PASSWORD", services.reset_password, admin, manager.pk, "12345", "invalid reset")
    manager.refresh_from_db()
    assert manager.check_password(PASSWORD)
    assert manager.must_change_password is False


def test_login_failure_does_not_disclose_whether_account_exists(manager):
    for username in [manager.username, "missing-user"]:
        assert_error(
            "INVALID_CREDENTIALS", services.authenticate, username, "wrong-password", remote_addr="127.0.0.1"
        )


def test_fifth_login_failure_throttles_username_and_address_pair(manager):
    for _ in range(4):
        assert_error(
            "INVALID_CREDENTIALS", services.authenticate, manager.username, "wrong-password", remote_addr="127.0.0.1"
        )
    assert_error(
        "LOGIN_THROTTLED", services.authenticate, manager.username, "wrong-password", remote_addr="127.0.0.1"
    )
    assert_error(
        "LOGIN_THROTTLED", services.authenticate, manager.username, PASSWORD, remote_addr="127.0.0.1"
    )
    assert services.authenticate(manager.username, PASSWORD, remote_addr="127.0.0.2").pk == manager.pk


def test_login_throttle_expires_after_five_minutes(manager, monkeypatch):
    now = timezone.now()
    for _ in range(5):
        with pytest.raises(DomainError):
            services.authenticate(manager.username, "wrong-password", remote_addr="127.0.0.1")
    monkeypatch.setattr(services.timezone, "now", lambda: now + timedelta(minutes=5, seconds=5))
    assert services.authenticate(manager.username, PASSWORD, remote_addr="127.0.0.1").pk == manager.pk


def test_logout_is_post_only_and_requires_csrf(admin):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    session_login(client, admin)
    assert client.get(reverse("accounts:logout")).status_code == 405
    assert client.post(reverse("accounts:logout")).status_code == 403
    assert client.session.get("_auth_user_id") == str(admin.pk)


@pytest.mark.parametrize("role,view_name", [
    ("ADMIN", "accounts:manage"),
    ("ADMIN", "accounts:me"),
    ("MANAGER", "accounts:me"),
])
def test_logout_is_at_bottom_of_account_page_not_global_header(user_factory, role, view_name):
    from django.test import Client

    actor = user_factory("logout-user", role=role)
    client = Client(enforce_csrf_checks=True)
    session_login(client, actor)
    project_page = client.get(reverse("projects:list"))
    assert project_page.status_code == 200
    assert reverse("accounts:logout") not in project_page.content.decode()
    assert f'href="{reverse("accounts:me")}"' in project_page.content.decode()

    response = client.get(reverse(view_name))
    assert response.status_code == 200
    content = response.content.decode()
    header = re.search(r'<header class="site-header">(.*?)</header>', content, re.S).group(1)
    assert "로그아웃" not in header
    if view_name == "accounts:manage":
        assert reverse("accounts:logout") not in content
        return
    assert content.count(f'action="{reverse("accounts:logout")}"') == 1
    form = re.search(r'<form class="account-logout" method="post" action="/accounts/logout/">(.*?)</form>\s*</div>\s*</main>', content, re.S)
    assert form is not None
    assert '<button class="quiet" type="submit">로그아웃</button>' in form.group(1)
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form.group(1)).group(1)
    assert client.post(reverse("accounts:logout"), {"csrfmiddlewaretoken": token}).status_code == 302
    assert "_auth_user_id" not in client.session


def test_initial_password_change_still_allows_logout(client, manager):
    manager.must_change_password = True
    manager.save(update_fields=["must_change_password"])
    session_login(client, manager)
    response = client.get(reverse("accounts:me"), follow=True)
    assert response.resolver_match.view_name == "accounts:password"
    assert '<form class="account-logout" method="post" action="/accounts/logout/">' in response.content.decode()
    assert client.post(reverse("accounts:logout")).status_code == 302
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("role", ["ADMIN", "MANAGER"])
def test_personal_account_page_only_offers_id_nickname_password_and_logout(client, user_factory, role):
    actor = user_factory("profile-owner", role=role, email="owner@example.com")
    session_login(client, actor)
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 200
    content = response.content.decode()
    main = re.search(r'<main\b[^>]*>(.*?)</main>', content, re.S).group(1)
    assert re.search(r'<input\b[^>]*name="display_name"[^>]*value="profile-owner"', main)
    assert re.search(r'<input\b[^>]*id="account-username"[^>]*value="profile-owner"[^>]*readonly', main)
    assert 'name="username"' not in main
    assert '닉네임:' in main
    assert 'name="email"' not in main
    assert '이메일' not in main
    assert 'owner@example.com' not in main
    assert "내 정보 저장" in main
    assert f'href="{reverse("accounts:password")}"' in main
    assert f'action="{reverse("accounts:logout")}"' in main
    for absent in ["사용 가능한 프로젝트", "할당된 프로젝트", "관리자에게 문의", "관리자", "매니저"]:
        assert absent not in main
    assert reverse("accounts:manage") not in main


@pytest.mark.parametrize("role", ["ADMIN", "MANAGER"])
def test_personal_profile_save_changes_only_own_name_and_keeps_email_and_session(client, user_factory, role):
    actor = user_factory("profile-owner", role=role, email="original@example.com")
    other = user_factory("another-account", email="other@example.com")
    session_login(client, actor)
    before_actor = get_user_model().objects.values().get(pk=actor.pk)
    before_other = get_user_model().objects.values().get(pk=other.pk)
    session_before = dict(client.session)
    response = client.post(reverse("accounts:me"), {
        "display_name": "새 이름", "email": "owner@example.com",
        "target_id": str(other.pk), "user_id": str(other.pk), "pk": str(other.pk),
        "username": "replaced-login", "role": "MANAGER" if role == "ADMIN" else "ADMIN",
        "is_active": "false", "is_staff": "true", "is_superuser": "true",
        "authz_version": "999", "password": "forged-password",
        "must_change_password": "true", "action": "change",
    })
    assert response.status_code == 302
    assert response.url == reverse("accounts:me")
    after_actor = get_user_model().objects.values().get(pk=actor.pk)
    assert after_actor == {**before_actor, "display_name": "새 이름"}
    assert get_user_model().objects.values().get(pk=other.pk) == before_other
    for key in ["_auth_user_id", "_auth_user_hash", "authz_version", "issued_at"]:
        assert client.session[key] == session_before[key]
    page = client.get(reverse("accounts:me"))
    assert page.status_code == 200
    assert page.context["form"]["display_name"].value() == "새 이름"
    assert page.context["account_username"] == "profile-owner"
    assert "original@example.com" not in page.content.decode()
    if role == "MANAGER":
        assert client.get(reverse("accounts:manage")).status_code == 403


@pytest.mark.parametrize("data,invalid_field", [
    ({"display_name": "", "email": "owner@example.com"}, "display_name"),
    ({"display_name": "   ", "email": "owner@example.com"}, "display_name"),
    ({"display_name": "x" * 101, "email": "owner@example.com"}, "display_name"),
    ({}, "display_name"),
])
def test_personal_profile_http_rejects_invalid_values_without_partial_save(client, manager, data, invalid_field):
    session_login(client, manager)
    before = get_user_model().objects.values().get(pk=manager.pk)
    response = client.post(reverse("accounts:me"), data)
    assert response.status_code == 400
    assert invalid_field in response.context["form"].errors
    assert get_user_model().objects.values().get(pk=manager.pk) == before
    assert client.session["_auth_user_id"] == str(manager.pk)


@pytest.mark.parametrize("stored_email", ["", "original@example.com"])
def test_personal_profile_email_cannot_be_changed_by_forged_post(client, manager, stored_email):
    manager.email = stored_email
    manager.save(update_fields=["email"])
    session_login(client, manager)
    for email in ["forged@example.com", "", "invalid-email", "x" * 300]:
        response = client.post(reverse("accounts:me"), {"display_name": "Contact Owner", "email": email})
        assert response.status_code == 302
        page = client.get(reverse("accounts:me"))
        assert 'account_email' not in page.context
        assert '이메일' not in page.content.decode()
        manager.refresh_from_db()
        assert manager.email == stored_email
        assert manager.username == "manager"
        assert manager.check_password(PASSWORD)


def test_personal_profile_save_requires_csrf_and_accepts_rendered_token(manager):
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    session_login(client, manager)
    data = {"display_name": "CSRF Owner", "email": "owner@example.com"}
    assert client.post(reverse("accounts:me"), data).status_code == 403
    manager.refresh_from_db()
    assert manager.display_name == "manager"
    assert manager.email == ""
    page = client.get(reverse("accounts:me"))
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
    assert client.post(reverse("accounts:me"), {**data, "csrfmiddlewaretoken": token}).status_code == 302
    manager.refresh_from_db()
    assert manager.display_name == "CSRF Owner"
    assert manager.email == ""


def test_personal_profile_post_cannot_bypass_initial_password_change(client, manager):
    manager.must_change_password = True
    manager.save(update_fields=["must_change_password"])
    session_login(client, manager)
    response = client.post(reverse("accounts:me"), {"display_name": "Blocked", "email": "blocked@example.com"})
    assert response.status_code == 302
    assert response.url == reverse("accounts:password")
    manager.refresh_from_db()
    assert manager.display_name == "manager"
    assert manager.email == ""
    assert manager.must_change_password


def test_personal_profile_post_requires_login(client, manager):
    response = client.post(reverse("accounts:me"), {"display_name": "Blocked", "email": "blocked@example.com"})
    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts:login"))
    manager.refresh_from_db()
    assert manager.display_name == "manager"
    assert manager.email == ""


def test_personal_profile_service_trims_fields_and_audits_only_profile_changes(manager):
    before_version = manager.authz_version
    updated = services.update_profile(manager, "  이름 변경  ")
    assert updated.pk == manager.pk
    assert updated.display_name == "이름 변경"
    assert updated.email == ""
    assert updated.authz_version == before_version
    assert updated.check_password(PASSWORD)
    audit = AccountAudit.objects.get(actor=manager, subject=manager, action="account.profile")
    assert audit.before == {"display_name": "manager"}
    assert audit.after == {"display_name": "이름 변경"}
    services.update_profile(updated, updated.display_name)
    assert AccountAudit.objects.filter(action="account.profile").count() == 1


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 101, None])
def test_personal_profile_service_rejects_invalid_values_without_writes(manager, display_name):
    before = get_user_model().objects.values().get(pk=manager.pk)
    assert_error("INVALID_INPUT", services.update_profile, manager, display_name)
    assert get_user_model().objects.values().get(pk=manager.pk) == before
    assert not AccountAudit.objects.filter(action="account.profile").exists()


@pytest.mark.parametrize("state,error", [
    ("anonymous", "AUTH_REQUIRED"), ("inactive", "AUTH_REQUIRED"),
    ("initial-password", "PASSWORD_CHANGE_REQUIRED"),
])
def test_personal_profile_service_requires_current_active_password_ready_account(manager, state, error):
    actor = manager
    if state == "anonymous":
        actor = AnonymousUser()
    elif state == "inactive":
        get_user_model().objects.filter(pk=manager.pk).update(is_active=False)
    else:
        get_user_model().objects.filter(pk=manager.pk).update(must_change_password=True)
    before = get_user_model().objects.values().get(pk=manager.pk)
    assert_error(error, services.update_profile, actor, "Blocked")
    assert get_user_model().objects.values().get(pk=manager.pk) == before
    assert not AccountAudit.objects.filter(action="account.profile").exists()


def test_personal_profile_service_does_not_restore_stale_role_or_session_version(admin, user_factory):
    user_factory("remaining-admin", role="ADMIN")
    changed_version = admin.authz_version + 1
    get_user_model().objects.filter(pk=admin.pk).update(role="MANAGER", authz_version=changed_version)
    updated = services.update_profile(admin, "Current Name")
    assert updated.role == "MANAGER"
    assert updated.authz_version == changed_version
    assert updated.username == "admin"
    assert updated.check_password(PASSWORD)


def test_account_management_labels_target_their_own_form_without_changing_post_names(client, admin, manager, project):
    session_login(client, admin)
    response = client.get(reverse("accounts:manage"))
    assert response.status_code == 200
    content = response.content.decode()
    ids = re.findall(r'\sid="([^"]+)"', content)
    assert len(ids) == len(set(ids))
    expected_names = {
        "change": {"target_id", "expected_authz_version", "display_name", "role", "is_active", "reason"},
        "reset": {"target_id", "new_temporary_password", "reason"},
        "membership": {"user_id", "project_id", "granted", "reason"},
    }
    rendered_actions = []
    for form in re.findall(r'<form\b[^>]*>(.*?)</form>', content, re.S):
        form_ids = set(re.findall(r'\sid="([^"]+)"', form))
        for label_target in re.findall(r'<label\b[^>]*\sfor="([^"]+)"', form):
            assert label_target in form_ids
        action = re.search(r'name="action" value="([^"]+)"', form)
        if not action:
            continue
        action = action.group(1)
        rendered_actions.append(action)
        if action == "create":
            assert "id_username" in form_ids
        else:
            names = set(re.findall(r'\sname="([^"]+)"', form))
            assert names == expected_names[action] | {"action", "csrfmiddlewaretoken"}
    assert sorted(rendered_actions) == sorted(["create", "change", "reset", "membership", "change", "reset", "membership"])


def test_login_renders_minimal_accessible_form(client):
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    content = response.content.decode()
    main = re.search(r'<main class="site-main container login-main">(.*?)</main>', content, re.S).group(1)
    assert '<form class="form-stack login-form" method="post" aria-label="로그인">' in main
    assert '<h1' not in main
    assert '<p>' not in main
    assert 'name="csrfmiddlewaretoken"' in main
    assert 'for="id_username">아이디:</label>' in main
    assert 'autocomplete="username"' in main
    assert 'autocomplete="current-password"' in main
    assert 'type="password"' in main


def test_minimal_login_keeps_authentication_errors(client, manager):
    response = client.post(reverse("accounts:login"), {"username": manager.username, "password": "incorrect-password"})
    assert response.status_code == 401
    content = response.content.decode()
    assert 'class="errorlist nonfield"' in content
    assert "사용자 이름 또는 비밀번호를 확인하세요." in content
    assert 'aria-label="로그인"' in content
    assert 'value="manager"' in content
    assert "incorrect-password" not in content
    assert "_auth_user_id" not in client.session


def test_successful_login_records_version_and_absolute_issue_time(client, manager):
    before = timezone.now().timestamp()
    response = client.post(reverse("accounts:login"), {"username": manager.username, "password": PASSWORD})
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(manager.pk)
    assert client.session["authz_version"] == manager.authz_version
    assert before <= client.session["issued_at"] <= timezone.now().timestamp()
    assert client.session.get_expire_at_browser_close() is True
    assert PASSWORD.encode() not in response.content


def test_login_preserves_leading_and_trailing_password_whitespace(client, manager):
    password = f"  {PASSWORD}  "
    manager.set_password(password)
    manager.save(update_fields=["password"])
    response = client.post(reverse("accounts:login"), {"username": manager.username, "password": password})
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(manager.pk)


def test_password_change_preserves_entered_whitespace(client, manager):
    session_login(client, manager)
    new_password = f"  {NEW_PASSWORD}  "
    response = client.post(
        reverse("accounts:password"),
        {"current_password": PASSWORD, "new_password": new_password, "new_password_confirm": new_password},
    )
    assert response.status_code == 302
    manager.refresh_from_db()
    assert manager.check_password(new_password)
    assert not manager.check_password(NEW_PASSWORD)


def test_logout_destroys_current_session_and_can_be_repeated(client, manager):
    session_login(client, manager)
    assert client.post(reverse("accounts:logout")).status_code == 302
    assert "_auth_user_id" not in client.session
    assert client.post(reverse("accounts:logout")).status_code == 302
    assert "_auth_user_id" not in client.session


def test_unauthenticated_json_request_returns_401_not_login_html(client):
    response = client.get(reverse("accounts:me"), HTTP_ACCEPT="application/json")
    assert response.status_code == 401


def test_unauthenticated_other_domain_json_request_returns_401(client):
    response = client.get(reverse("projects:list"), HTTP_ACCEPT="application/json")
    assert response.status_code == 401


def test_unauthenticated_polling_returns_401_without_accept_header(client):
    response = client.get("/conversations/0c212bd1-6aa3-46d0-9226-b9731013d8ae/updates/")
    assert response.status_code == 401


def test_manager_cannot_create_accounts_through_direct_http_request(client, manager):
    session_login(client, manager)
    before = get_user_model().objects.count()
    response = client.post(
        reverse("accounts:manage"),
        {"username": "forged-admin", "display_name": "Forged", "role": "ADMIN", "initial_password": PASSWORD},
    )
    assert response.status_code == 403
    assert get_user_model().objects.count() == before


def test_initial_password_redirects_to_password_change(client, admin):
    created = services.create_account(admin, "initial-http", "Initial HTTP", "MANAGER", PASSWORD)
    session_login(client, created)
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 302
    assert response.url == reverse("accounts:password")
    assert client.get(reverse("accounts:password")).status_code == 200


def test_password_change_http_forces_new_login(client, manager):
    session_login(client, manager)
    response = client.post(
        reverse("accounts:password"),
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD},
    )
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session
    manager.refresh_from_db()
    assert manager.check_password(NEW_PASSWORD)


def test_permission_change_invalidates_existing_http_session(client, admin, manager):
    session_login(client, manager)
    services.change_account(admin, manager.pk, manager.authz_version, {"role": "ADMIN"}, "promoted")
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 302
    assert reverse("accounts:login") in response.url
    assert "_auth_user_id" not in client.session


def test_membership_revocation_invalidates_existing_http_session(client, admin, manager, assigned_project):
    session_login(client, manager)
    services.set_membership(admin, manager.pk, assigned_project.pk, False, "remove assignment")
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session


def test_password_reset_invalidates_existing_http_session(client, admin, manager):
    session_login(client, manager)
    services.reset_password(admin, manager.pk, NEW_PASSWORD, "recover account")
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session


def test_deactivation_invalidates_existing_http_session(client, admin, manager):
    session_login(client, manager)
    services.change_account(admin, manager.pk, manager.authz_version, {"is_active": False}, "remove access")
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session


def test_twelve_hour_session_limit_is_absolute(client, manager):
    session_login(client, manager, issued_at=timezone.now().timestamp() - 12 * 60 * 60 - 1)
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 302
    assert reverse("accounts:login") in response.url
    assert "_auth_user_id" not in client.session


def test_http_polling_does_not_extend_session_issue_time(client, manager):
    issued_at = timezone.now().timestamp() - 60
    session_login(client, manager, issued_at=issued_at)
    assert client.get(reverse("accounts:me")).status_code == 200
    assert client.session["issued_at"] == issued_at


def test_session_without_issue_metadata_is_rejected(client, manager):
    client.force_login(manager)
    response = client.get(reverse("accounts:me"))
    assert response.status_code == 302
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("password", [PASSWORD, "123456"])
def test_bootstrap_admin_uses_hidden_input_and_does_not_print_password(monkeypatch, password):
    prompts = []

    def hidden_password(prompt):
        prompts.append(prompt)
        return password

    monkeypatch.setattr("apps.accounts.management.commands.bootstrap_admin.getpass", hidden_password)
    output = StringIO()
    call_command("bootstrap_admin", username="bootstrap-user", stdout=output)
    created = get_user_model().objects.get(username="bootstrap-user")
    assert created.role == "ADMIN"
    assert created.is_active is True
    assert created.check_password(password)
    assert created.must_change_password is False
    assert len(prompts) == 2
    assert password not in output.getvalue()
    assert created.password not in output.getvalue()
    assert AccountAudit.objects.filter(subject=created, action="account.bootstrap").exists()


def test_bootstrap_does_not_promote_or_reset_existing_account(monkeypatch, manager):
    def unexpected_password_prompt(prompt):
        pytest.fail("Existing account should be rejected before requesting a password")

    monkeypatch.setattr("apps.accounts.management.commands.bootstrap_admin.getpass", unexpected_password_prompt)
    with pytest.raises(CommandError):
        call_command("bootstrap_admin", username=manager.username, stdout=StringIO())
    manager.refresh_from_db()
    assert manager.role == "MANAGER"
    assert manager.check_password(PASSWORD)


def test_bootstrap_cannot_create_another_admin_after_initial_setup(monkeypatch, admin):
    def unexpected_password_prompt(prompt):
        pytest.fail("Completed setup should be rejected before requesting a password")

    monkeypatch.setattr("apps.accounts.management.commands.bootstrap_admin.getpass", unexpected_password_prompt)
    with pytest.raises(CommandError):
        call_command("bootstrap_admin", username="extra-bootstrap-admin", stdout=StringIO())
    assert get_user_model().objects.count() == 1
    assert not get_user_model().objects.filter(username="extra-bootstrap-admin").exists()


def test_bootstrap_password_mismatch_creates_no_account(monkeypatch):
    passwords = iter([PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr("apps.accounts.management.commands.bootstrap_admin.getpass", lambda prompt: next(passwords))
    with pytest.raises(CommandError):
        call_command("bootstrap_admin", username="mismatched", stdout=StringIO())
    assert not get_user_model().objects.filter(username="mismatched").exists()
