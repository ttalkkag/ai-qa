from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import ProjectMembership, User
from apps.conversations.models import Conversation
from apps.conversations.services import start_conversation
from apps.errors import DomainError
from apps.projects.models import Project
from apps.runs.models import Job, Run
from apps.work_requests import services
from apps.work_requests.models import RequestRevision, RequestSubmission, WorkRequest


pytestmark = pytest.mark.django_db


@pytest.fixture
def request_domain(tmp_path):
    admin = User.objects.create_user(username="req-admin", password="test-password", role="ADMIN")
    owner = User.objects.create_user(username="req-owner", password="test-password")
    outsider = User.objects.create_user(username="req-other", password="test-password")
    project = Project.objects.create(name="Request project", canonical_repo_path=str(tmp_path), created_by=admin)
    for actor in (owner, outsider):
        ProjectMembership.objects.create(user=actor, project=project, granted_by=admin)
    conversation = start_conversation(owner, project.id, "취소 버튼이 동작하지 않아요", "message-one", "conversation-one")
    work_request = services.create_or_get_draft(owner, conversation.id)
    basis = SimpleNamespace(project=project, config_revision=SimpleNamespace(project_config_version=1), base_commit_sha="a" * 40, policy={})
    return SimpleNamespace(admin=admin, owner=owner, outsider=outsider, project=project, conversation=conversation, request=work_request, basis=basis)


@pytest.fixture
def execution_basis(request_domain, monkeypatch):
    def resolve(actor, project_id, expected_version=None, expected_sha=None, **kwargs):
        basis = request_domain.basis
        if expected_version is not None and expected_version != basis.config_revision.project_config_version:
            raise DomainError("PROJECT_CONFIG_CHANGED", status=409)
        if expected_sha is not None and expected_sha != basis.base_commit_sha:
            raise DomainError("BASE_COMMIT_CHANGED", status=409)
        return basis
    monkeypatch.setattr(services, "resolve_execution_input", resolve)
    monkeypatch.setattr("apps.projects.services.resolve_execution_input", resolve)
    return request_domain


def specification(**overrides):
    result = {"title": "취소 버튼 수정", "goal": "취소 가능한 주문을 취소", "current_behavior": "반응 없음",
              "expected_behavior": "주문 취소 상태 표시", "reproduction_steps": "주문 상세에서 취소 클릭",
              "acceptance_criteria": ["취소 후 상태 안내"], "scope": "주문 상세", "out_of_scope": "환불 정책",
              "unknowns": "브라우저 차이 미확인", "attachment_ids": [], "blocking_questions": []}
    result.update(overrides)
    return result


def ready(domain):
    updated = services.save_draft(domain.owner, domain.request.id, domain.request.draft_version, specification())
    domain.request = updated
    return services.confirm_revision(domain.owner, updated.id, updated.draft_version,
        domain.basis.config_revision.project_config_version, domain.basis.base_commit_sha)


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(DomainError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def test_one_conversation_one_draft_and_no_run(request_domain):
    domain = request_domain
    assert services.create_or_get_draft(domain.owner, domain.conversation.id).id == domain.request.id
    assert WorkRequest.objects.count() == 1
    assert Run.objects.count() == 0
    assert Job.objects.filter(kind="CLARIFICATION").count() == 1
    assert domain.request.draft["goal"] == ""


def test_suggestion_progress_is_private_and_uncached(client, request_domain):
    domain = request_domain
    url = f"/requests/{domain.request.id}/progress/"
    assert client.get(url).status_code == 302
    for actor in (domain.outsider, domain.owner):
        client.force_login(actor)
        session = client.session
        session["authz_version"] = actor.authz_version
        session["issued_at"] = timezone.now().timestamp()
        session.save()
        if actor == domain.outsider:
            assert client.get(url).status_code == 404
    response = client.get(url)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert response.json() == {"draft_version": 1, "suggestion_state": "QUEUED"}
    invocation = domain.request.suggestions.get().invocation
    invocation.state = "SUCCEEDED"
    invocation.save(update_fields=["state"])
    assert client.get(url).json()["suggestion_state"] == "SUCCEEDED"
    ProjectMembership.objects.filter(user=domain.owner).delete()
    assert client.get(url).status_code == 404


def test_analysis_survives_confirmation(execution_basis):
    domain = execution_basis
    fields = specification(related_domains="주문", impact_analysis="cancel 함수와 주문 상태", conflict_analysis="완료 주문은 취소하지 않음")
    draft = services.save_draft(domain.owner, domain.request.id, 1, fields)
    revision = services.confirm_revision(domain.owner, draft.id, draft.draft_version, 1, "a" * 40)
    for name in ("related_domains", "impact_analysis", "conflict_analysis"):
        assert revision.snapshot[name] == fields[name]
    prompt = domain.request.suggestions.first().invocation.prompt
    assert "엔드포인트" in prompt and "blocking_questions" in prompt


def test_required_fields_and_blocking_questions_cannot_be_confirmed(execution_basis):
    domain = execution_basis
    assert_error("SPEC_INCOMPLETE", services.confirm_revision, domain.owner, domain.request.id, 1, 1, "a" * 40)
    draft = services.save_draft(domain.owner, domain.request.id, 1, specification(blocking_questions=["무엇을 수정하나요?"]))
    assert_error("SPEC_INCOMPLETE", services.confirm_revision, domain.owner, draft.id, draft.draft_version, 1, "a" * 40)
    assert RequestRevision.objects.count() == 0


def test_confirm_is_idempotent_and_snapshot_is_immutable(execution_basis):
    domain = execution_basis
    revision = ready(domain)
    again = services.confirm_revision(domain.owner, domain.request.id, domain.request.draft_version, 1, "a" * 40)
    assert again.id == revision.id
    original_snapshot, original_hash = revision.snapshot.copy(), revision.content_hash
    services.save_draft(domain.owner, domain.request.id, domain.request.draft_version, specification(goal="새 요구"))
    revision.refresh_from_db()
    assert revision.snapshot == original_snapshot and revision.content_hash == original_hash
    with pytest.raises(ValueError):
        revision.save()
    with pytest.raises(ValueError):
        RequestRevision.objects.filter(pk=revision.pk).update(snapshot={})


def test_old_tab_cannot_overwrite_new_draft(execution_basis):
    domain = execution_basis
    services.save_draft(domain.owner, domain.request.id, 1, specification())
    assert_error("VERSION_CONFLICT", services.save_draft, domain.owner, domain.request.id, 1, specification(goal="오래된 내용"))
    domain.request.refresh_from_db()
    assert domain.request.draft["goal"] != "오래된 내용"


def test_changed_confirmation_basis_is_rejected(execution_basis):
    domain = execution_basis
    draft = services.save_draft(domain.owner, domain.request.id, 1, specification())
    domain.basis.base_commit_sha = "b" * 40
    assert_error("BASE_COMMIT_CHANGED", services.confirm_revision, domain.owner, draft.id, draft.draft_version, 1, "a" * 40)
    assert RequestRevision.objects.count() == 0


def test_refresh_basis_preserves_body_and_is_idempotent(execution_basis):
    domain = execution_basis
    old_revision = ready(domain)
    original = domain.request.draft
    domain.basis.base_commit_sha = "b" * 40
    assert_error("BASIS_CONFLICT", services.confirm_revision, domain.owner, domain.request.id, domain.request.draft_version, 1, "b" * 40)
    refreshed = services.refresh_basis(domain.owner, domain.request.id, domain.request.draft_version, "refresh-key")
    same = services.refresh_basis(domain.owner, domain.request.id, domain.request.draft_version, "refresh-key")
    assert refreshed.draft_version == same.draft_version == domain.request.draft_version + 1
    assert refreshed.draft == original and refreshed.state == "DRAFT"
    new = services.confirm_revision(domain.owner, refreshed.id, refreshed.draft_version, 1, "b" * 40)
    assert new.number == old_revision.number + 1 and new.base_commit_sha == "b" * 40


def test_submit_same_and_different_keys_return_one_primary_run(execution_basis):
    domain = execution_basis
    revision = ready(domain)
    first = services.submit(domain.owner, domain.request.id, revision.id, "submit-key")
    assert services.submit(domain.owner, domain.request.id, revision.id, "submit-key").pk == first.pk
    assert services.submit(domain.owner, domain.request.id, revision.id, "alias-key").pk == first.pk
    assert Run.objects.count() == RequestSubmission.objects.count() == Job.objects.filter(kind="RUN").count() == 1
    domain.request.refresh_from_db()
    assert domain.request.state == "SUBMITTED"


def test_submission_alias_key_cannot_later_target_other_revision(execution_basis):
    domain = execution_basis
    revision = ready(domain)
    first = services.submit(domain.owner, domain.request.id, revision.id, "first-key")
    services.submit(domain.owner, domain.request.id, revision.id, "alias-key")
    Run.objects.filter(pk=first.primary_run_id).update(status="FAILED")
    domain.request.refresh_from_db()
    next_revision = ready(domain)
    assert_error("IDEMPOTENCY_CONFLICT", services.submit, domain.owner, domain.request.id, next_revision.id, "alias-key")
    assert Run.objects.count() == 1


def test_active_run_blocks_new_revision_and_old_input_stays_same(execution_basis):
    domain = execution_basis
    first_revision = ready(domain)
    submission = services.submit(domain.owner, domain.request.id, first_revision.id, "submit")
    domain.request.refresh_from_db()
    new_revision = ready(domain)
    assert_error("REQUEST_BUSY", services.submit, domain.owner, domain.request.id, new_revision.id, "next-submit")
    submission.primary_run.refresh_from_db()
    assert submission.primary_run.request_revision_id == first_revision.id
    assert submission.primary_run.baseline_commit_sha == first_revision.base_commit_sha


def test_submission_rechecks_project_basis(execution_basis):
    domain = execution_basis
    revision = ready(domain)
    domain.basis.base_commit_sha = "c" * 40
    assert_error("BASE_COMMIT_CHANGED", services.submit, domain.owner, domain.request.id, revision.id, "submit")
    assert not Run.objects.exists() and not RequestSubmission.objects.exists()


def test_run_registration_failure_rolls_back_submission(execution_basis):
    domain = execution_basis
    revision = ready(domain)
    with patch("apps.runs.services.enqueue_job", side_effect=DomainError("QUEUE_UNAVAILABLE")):
        assert_error("QUEUE_UNAVAILABLE", services.submit, domain.owner, domain.request.id, revision.id, "submit")
    assert not Run.objects.exists() and not RequestSubmission.objects.exists()
    domain.request.refresh_from_db()
    assert domain.request.state == "READY"


@pytest.mark.parametrize("actor_field", ["outsider", "admin"])
def test_no_one_can_confirm_or_submit_for_another_owner(execution_basis, actor_field):
    domain = execution_basis
    revision = ready(domain)
    actor = getattr(domain, actor_field)
    assert_error("NOT_FOUND", services.confirm_revision, actor, domain.request.id, domain.request.draft_version, 1, "a" * 40)
    assert_error("NOT_FOUND", services.submit, actor, domain.request.id, revision.id, "impersonate")
    assert not Run.objects.exists()


def test_foreign_revision_and_revoked_membership_are_rejected(execution_basis):
    domain = execution_basis
    revision = ready(domain)
    other_conversation = start_conversation(domain.owner, domain.project.id, "다른 문제", "other-message", "other-chat")
    other_request = services.create_or_get_draft(domain.owner, other_conversation.id)
    assert_error("NOT_FOUND", services.submit, domain.owner, other_request.id, revision.id, "cross-request")
    ProjectMembership.objects.filter(user=domain.owner).delete()
    assert_error("NOT_FOUND", services.get_request, domain.owner, domain.request.id)


def test_late_suggestion_does_not_overwrite_new_draft(execution_basis):
    domain = execution_basis
    suggestion = domain.request.suggestions.get()
    suggestion.invocation.state = "SUCCEEDED"
    suggestion.invocation.save(update_fields=["state"])
    draft = services.save_draft(domain.owner, domain.request.id, 1, specification(goal="최신 요구"))
    services.complete_suggestion(suggestion.invocation_id, specification(goal="오래된 제안"))
    assert_error("SUGGESTION_STALE", services.apply_suggestion, domain.owner, draft.id, suggestion.id, draft.draft_version)
    draft.refresh_from_db()
    assert draft.draft["goal"] == "최신 요구"


def test_suggestion_is_explicit_and_idempotent(execution_basis):
    domain = execution_basis
    first = services.suggest_revision(domain.owner, domain.request.id, 1, "suggestion-key")
    assert services.suggest_revision(domain.owner, domain.request.id, 1, "suggestion-key").id == first.id
    first.invocation.state = "SUCCEEDED"
    first.invocation.save(update_fields=["state"])
    services.complete_suggestion(first.invocation_id, specification())
    domain.request.refresh_from_db()
    assert domain.request.draft["goal"] == ""
    updated = services.apply_suggestion(domain.owner, domain.request.id, first.id, 1)
    assert updated.state == "DRAFT" and updated.draft["goal"] == specification()["goal"]
    assert not Run.objects.exists()


def test_revision_pins_selected_attachments_and_rejects_other_conversation(execution_basis):
    from apps.artifacts.models import ArtifactPin
    from apps.artifacts.services import store_attachment

    domain = execution_basis
    attachment = store_attachment(domain.owner, domain.conversation.id, SimpleUploadedFile("steps.txt", "재현 과정".encode(), content_type="text/plain"))
    updated = services.save_draft(domain.owner, domain.request.id, 1, specification(attachment_ids=[str(attachment.id)]))
    revision = services.confirm_revision(domain.owner, updated.id, updated.draft_version, 1, "a" * 40)
    assert revision.attachment_snapshot[0]["sha256"] == attachment.sha256
    assert ArtifactPin.objects.filter(artifact=attachment, reference=f"revision:{revision.id}").exists()
    foreign = start_conversation(domain.outsider, domain.project.id, "다른 사람", "foreign", "foreign")
    foreign_attachment = store_attachment(domain.outsider, foreign.id, SimpleUploadedFile("private.txt", b"private", content_type="text/plain"))
    assert_error("NOT_FOUND", services.save_draft, domain.owner, updated.id, updated.draft_version, specification(attachment_ids=[str(foreign_attachment.id)]))


def test_suggestion_worker_context_uses_frozen_prompt_not_future_draft(execution_basis):
    domain = execution_basis
    suggestion = domain.request.suggestions.get()
    prompt = suggestion.invocation.prompt
    services.save_draft(domain.owner, domain.request.id, 1, specification(goal="미래의 바뀐 목표"))
    context = services.build_suggestion_context(suggestion.invocation)
    assert context == prompt and "미래의 바뀐 목표" not in context
    ProjectMembership.objects.filter(user=domain.owner).delete()
    assert_error("NOT_FOUND", services.build_suggestion_context, suggestion.invocation)


def test_request_form_has_separate_save_confirm_submit_and_escapes_html(execution_basis, client):
    domain = execution_basis
    services.save_draft(domain.owner, domain.request.id, 1, specification(title="<script>alert(1)</script>"))
    client.force_login(domain.owner)
    session = client.session
    session["authz_version"] = domain.owner.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()
    response = client.get(f"/requests/{domain.request.id}/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "&lt;script&gt;" in body and "<script>alert" not in body
    assert "초안 저장" in body and "이 명세로 준비" in body and "name=\"expected_base_commit_sha\"" in body
    assert client.get(f"/requests/{domain.request.id}/ready/").status_code == 405
