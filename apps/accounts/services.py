import hashlib
from datetime import timedelta

from django.apps import apps
from django.contrib.auth import authenticate as django_authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Exists, F, Q
from django.utils import timezone

from apps.errors import DomainError

from .models import AccountAudit, LoginAttempt, ProjectMembership, User


ADMIN_ACTIONS = frozenset({
    "account.manage", "project.manage", "model.manage", "publication.approve",
    "publication.publish", "publication.reject", "run.cancel_operational", "result.approve",
})
PROJECT_ACTIONS = frozenset({"project.view", "project.inspect", "conversation.create", "request.create"})
READ_ACTIONS = frozenset({"conversation.view", "request.view", "run.view", "artifact.view", "event.view"})
OWNER_ACTIONS = frozenset({
    "conversation.edit", "request.edit", "request.confirm", "request.submit", "request.retry", "run.cancel",
})


def _current_user(actor):
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise DomainError("AUTH_REQUIRED", status=401)
    try:
        return User.objects.get(pk=actor.pk, is_active=True)
    except (User.DoesNotExist, ValidationError, ValueError, TypeError):
        raise DomainError("AUTH_REQUIRED", status=401) from None
    except OperationalError as error:
        if "locked" in str(error).lower():
            raise DomainError("VERSION_CONFLICT", status=409) from None
        raise


def _object_scope(obj):
    current = obj
    for _ in range(6):
        if current is None:
            break
        owner_id = getattr(current, "owner_id", None)
        project_id = getattr(current, "project_id", None)
        if owner_id is not None and project_id is not None:
            return owner_id, project_id
        current = next((getattr(current, name, None) for name in ("request", "conversation", "run", "revision") if getattr(current, name, None) is not None), None)
    raise DomainError("NOT_FOUND", status=404)


def require_access(actor, action, project_id=None, obj=None):
    actor = _current_user(actor)
    if action == "account.password":
        return actor
    if actor.must_change_password:
        raise DomainError("PASSWORD_CHANGE_REQUIRED", status=403)
    if action == "account.profile":
        return actor
    if action in ADMIN_ACTIONS:
        if actor.role != User.Role.ADMIN:
            raise DomainError("ACCESS_DENIED", status=403)
        return actor
    if action not in PROJECT_ACTIONS | READ_ACTIONS | OWNER_ACTIONS:
        raise DomainError("ACCESS_DENIED", status=403)
    owner_id = None
    if action in READ_ACTIONS | OWNER_ACTIONS:
        owner_id, object_project_id = _object_scope(obj)
        if project_id is not None and str(project_id) != str(object_project_id):
            raise DomainError("NOT_FOUND", status=404)
        project_id = object_project_id
    Project = apps.get_model("projects", "Project")
    try:
        project_exists = Project.objects.filter(pk=project_id).exists()
    except (ValidationError, ValueError, TypeError):
        project_exists = False
    if not project_exists:
        raise DomainError("NOT_FOUND", status=404)
    if actor.role != User.Role.ADMIN and not ProjectMembership.objects.filter(user=actor, project_id=project_id).exists():
        raise DomainError("NOT_FOUND", status=404)
    if owner_id is not None and str(owner_id) != str(actor.pk):
        if action in OWNER_ACTIONS or actor.role != User.Role.ADMIN:
            raise DomainError("NOT_FOUND", status=404)
        AccountAudit.objects.create(actor=actor, subject_id=owner_id, project_id=project_id, action=action, reason="운영 목적의 타인 자료 조회")
    return actor


def list_accessible_projects(actor):
    actor = _current_user(actor)
    if actor.must_change_password:
        raise DomainError("PASSWORD_CHANGE_REQUIRED", status=403)
    projects = apps.get_model("projects", "Project").objects.all()
    if actor.role != User.Role.ADMIN:
        projects = projects.filter(memberships__user=actor)
    return projects.order_by("name")


def filter_owned_queryset(actor, queryset, owner_lookup="owner"):
    if owner_lookup not in {"owner", "run__requested_by", "requested_by"}:
        raise DomainError("ACCESS_DENIED", status=403)
    actor = _current_user(actor)
    if actor.must_change_password:
        raise DomainError("PASSWORD_CHANGE_REQUIRED", status=403)
    if actor.role == User.Role.ADMIN:
        AccountAudit.objects.create(actor=actor, action="access.list", after={"model": queryset.model._meta.label}, reason="운영 목적의 전체 자료 목록 조회")
        return queryset
    return queryset.filter(**{owner_lookup: actor}, project__in=list_accessible_projects(actor))


def _validate_password(password, user):
    try:
        validate_password(password, user=user)
    except ValidationError:
        raise DomainError("WEAK_PASSWORD", "비밀번호가 안전성 기준을 충족하지 않습니다.") from None


def _reason(reason):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise DomainError("INVALID_INPUT", "변경 사유를 입력하세요.")
    return reason.strip()


def _admin_exists(admin_id):
    return Exists(User.objects.filter(pk=admin_id, is_active=True, role=User.Role.ADMIN, must_change_password=False))


def _admin_write_guard(admin):
    updated = User.objects.filter(pk=admin.pk, is_active=True, role=User.Role.ADMIN, must_change_password=False).update(authz_version=F("authz_version"))
    if not updated:
        raise DomainError("ACCESS_DENIED", status=403)


def _target(target_id):
    try:
        return User.objects.get(pk=target_id)
    except (User.DoesNotExist, ValidationError, ValueError, TypeError):
        raise DomainError("NOT_FOUND", status=404) from None
    except OperationalError as error:
        if "locked" in str(error).lower():
            raise DomainError("VERSION_CONFLICT", status=409) from None
        raise


def _authz_change(admin, target, updates, expected_version):
    queryset = User.objects.filter(pk=target.pk, authz_version=expected_version).filter(_admin_exists(admin.pk))
    if updates.get("role", target.role) != User.Role.ADMIN or not updates.get("is_active", target.is_active):
        another_admin = Exists(User.objects.filter(is_active=True, role=User.Role.ADMIN).exclude(pk=target.pk))
        queryset = queryset.filter(~Q(role=User.Role.ADMIN) | Q(is_active=False) | another_admin)
    if queryset.update(**updates, authz_version=F("authz_version") + 1) != 1:
        require_access(admin, "account.manage")
        target.refresh_from_db()
        if target.authz_version != expected_version:
            raise DomainError("VERSION_CONFLICT", status=409)
        raise DomainError("LAST_ADMIN_REQUIRED", status=409)
    target.refresh_from_db()
    return target


def _request_revoked_run_cancellation(user_id, project_id=None):
    from apps.runs.services import revoke_actor_work

    revoke_actor_work(user_id, project_id=project_id)


def create_account(admin, username, display_name, role, initial_password):
    admin = require_access(admin, "account.manage")
    user = User(username=username, display_name=display_name, role=role, must_change_password=True)
    if role not in User.Role.values or not isinstance(display_name, str) or not display_name.strip():
        raise DomainError("INVALID_INPUT")
    _validate_password(initial_password, user)
    user.set_password(initial_password)
    try:
        user.full_clean(exclude=["password"])
    except ValidationError:
        if User.objects.filter(username=username).exists():
            raise DomainError("USERNAME_TAKEN", status=409) from None
        raise DomainError("INVALID_INPUT") from None
    try:
        with transaction.atomic():
            _admin_write_guard(admin)
            user.save()
            AccountAudit.objects.create(actor=admin, subject=user, action="account.create", after={"username": user.username, "display_name": user.display_name, "role": user.role, "is_active": True})
    except IntegrityError:
        raise DomainError("USERNAME_TAKEN", status=409) from None
    return user


def change_account(admin, target_id, expected_authz_version, changes=None, reason="", **change_values):
    admin = require_access(admin, "account.manage")
    target = _target(target_id)
    reason = _reason(reason)
    updates = dict(changes or {})
    updates.update(change_values)
    if not updates or set(updates) - {"role", "is_active", "display_name"}:
        raise DomainError("INVALID_INPUT")
    if "role" in updates and updates["role"] not in User.Role.values:
        raise DomainError("INVALID_INPUT")
    if "is_active" in updates and not isinstance(updates["is_active"], bool):
        raise DomainError("INVALID_INPUT")
    if "display_name" in updates and (not isinstance(updates["display_name"], str) or not updates["display_name"].strip() or len(updates["display_name"]) > 100):
        raise DomainError("INVALID_INPUT")
    before = {key: getattr(target, key) for key in updates}
    try:
        with transaction.atomic():
            target = _authz_change(admin, target, updates, expected_authz_version)
            AccountAudit.objects.create(actor=admin, subject=target, action="account.change", before=before, after=updates, reason=reason)
            if updates.get("is_active") is False:
                _request_revoked_run_cancellation(target.pk)
            elif before.get("role") == User.Role.ADMIN and target.role == User.Role.MANAGER:
                from apps.approvals.services import revoke_actor_publications

                revoke_actor_publications(target.pk)
                Project = apps.get_model("projects", "Project")
                for project_id in Project.objects.exclude(memberships__user_id=target.pk).values_list("pk", flat=True):
                    _request_revoked_run_cancellation(target.pk, project_id=project_id)
    except OperationalError as error:
        if "locked" in str(error).lower():
            raise DomainError("VERSION_CONFLICT", status=409) from None
        raise
    return target


def set_membership(admin, user_id, project_id, granted, reason):
    admin = require_access(admin, "account.manage")
    target = _target(user_id)
    reason = _reason(reason)
    if not isinstance(granted, bool):
        raise DomainError("INVALID_INPUT")
    Project = apps.get_model("projects", "Project")
    try:
        project = Project.objects.get(pk=project_id)
    except (Project.DoesNotExist, ValidationError, ValueError, TypeError):
        raise DomainError("NOT_FOUND", status=404) from None
    with transaction.atomic():
        _authz_change(admin, target, {}, target.authz_version)
        if granted:
            membership, changed = ProjectMembership.objects.get_or_create(user=target, project=project, defaults={"granted_by": admin})
        else:
            changed, _ = ProjectMembership.objects.filter(user=target, project=project).delete()
            membership = None
        AccountAudit.objects.create(actor=admin, subject=target, project=project, action="membership.grant" if granted else "membership.revoke", after={"granted": granted, "changed": bool(changed)}, reason=reason)
        if not granted and changed and target.role != User.Role.ADMIN:
            _request_revoked_run_cancellation(target.pk, project_id=project_id)
    return membership


def update_profile(actor, display_name):
    actor = require_access(actor, "account.profile")
    if not isinstance(display_name, str):
        raise DomainError("INVALID_INPUT")
    try:
        updates = {
            "display_name": User._meta.get_field("display_name").clean(display_name.strip(), actor),
        }
    except ValidationError:
        raise DomainError("INVALID_INPUT", "닉네임을 확인하세요.") from None
    try:
        with transaction.atomic():
            actor = require_access(actor, "account.profile")
            before = {key: getattr(actor, key) for key in updates}
            if before == updates:
                return actor
            updated = User.objects.filter(
                pk=actor.pk, authz_version=actor.authz_version, is_active=True, must_change_password=False,
            ).update(**updates)
            if not updated:
                raise DomainError("VERSION_CONFLICT", status=409)
            AccountAudit.objects.create(actor=actor, subject=actor, action="account.profile", before=before, after=updates)
    except OperationalError as error:
        if "locked" in str(error).lower():
            raise DomainError("VERSION_CONFLICT", status=409) from None
        raise
    actor.refresh_from_db()
    return actor


def change_password(actor, current_password, new_password):
    actor = require_access(actor, "account.password")
    if not actor.check_password(current_password):
        raise DomainError("INVALID_CREDENTIALS", status=400)
    if current_password == new_password:
        raise DomainError("WEAK_PASSWORD", "현재 비밀번호와 다른 새 비밀번호를 사용하세요.")
    _validate_password(new_password, actor)
    actor.set_password(new_password)
    with transaction.atomic():
        updated = User.objects.filter(pk=actor.pk, authz_version=actor.authz_version, is_active=True).update(password=actor.password, must_change_password=False, authz_version=F("authz_version") + 1)
        if not updated:
            raise DomainError("VERSION_CONFLICT", status=409)
        AccountAudit.objects.create(actor=actor, subject=actor, action="password.change")
    actor.refresh_from_db()
    return actor


def reset_password(admin, target_id, new_temporary_password, reason):
    admin = require_access(admin, "account.manage")
    target = _target(target_id)
    reason = _reason(reason)
    _validate_password(new_temporary_password, target)
    target.set_password(new_temporary_password)
    with transaction.atomic():
        target = _authz_change(admin, target, {"password": target.password, "must_change_password": True}, target.authz_version)
        AccountAudit.objects.create(actor=admin, subject=target, action="password.reset", after={"must_change_password": True}, reason=reason)
        _request_revoked_run_cancellation(target.pk)
    return target


def authenticate(username, password, remote_addr=""):
    key = hashlib.sha256(f"{username}\0{remote_addr}".encode()).hexdigest()
    now = timezone.now()
    attempt, _ = LoginAttempt.objects.get_or_create(key=key)
    if attempt.locked_until and attempt.locked_until > now:
        raise DomainError("LOGIN_THROTTLED", "로그인을 잠시 후 다시 시도하세요.", status=429)
    if attempt.locked_until:
        LoginAttempt.objects.filter(pk=key, locked_until=attempt.locked_until).update(failures=0, locked_until=None)
    user = django_authenticate(username=username, password=password)
    if user is None:
        LoginAttempt.objects.filter(pk=key).update(failures=F("failures") + 1, updated_at=now)
        attempt.refresh_from_db()
        if attempt.failures >= 5:
            LoginAttempt.objects.filter(pk=key).update(locked_until=now + timedelta(minutes=5))
            raise DomainError("LOGIN_THROTTLED", "로그인을 잠시 후 다시 시도하세요.", status=429)
        raise DomainError("INVALID_CREDENTIALS", "사용자 이름 또는 비밀번호를 확인하세요.", status=401)
    LoginAttempt.objects.filter(pk=key).update(failures=0, locked_until=None)
    return user
