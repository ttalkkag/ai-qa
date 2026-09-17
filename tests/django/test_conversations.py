import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import ProjectMembership
from apps.ai.models import AIInvocation
from apps.conversations import services
from apps.conversations.models import Conversation, Message
from apps.errors import DomainError
from apps.runs.models import Job, Run
from tests.django.test_requests import request_domain


pytestmark = pytest.mark.django_db


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(DomainError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def login(client, user):
    client.force_login(user)
    session = client.session
    session["authz_version"] = user.authz_version
    session["issued_at"] = timezone.now().timestamp()
    session.save()


def test_message_and_job_are_registered_before_any_model_call(request_domain):
    domain = request_domain
    assert domain.conversation.messages.count() == 1
    assert Job.objects.filter(kind="CONVERSATION", slot="CONVERSATION").count() == 1
    assert AIInvocation.objects.get(purpose="conversation").state == "QUEUED"
    prompt = AIInvocation.objects.get(purpose="conversation").prompt
    assert "읽기 전용 프로젝트 코드" in prompt
    assert "상태를 질문한 경우에만" in prompt
    assert not Run.objects.exists()


def test_creation_and_message_idempotency_with_payload_conflicts(request_domain):
    domain = request_domain
    same = services.start_conversation(domain.owner, domain.project.id, "취소 버튼이 동작하지 않아요", "message-one", "conversation-one")
    assert same.id == domain.conversation.id
    assert_error("IDEMPOTENCY_CONFLICT", services.start_conversation, domain.owner, domain.project.id, "다른 내용", "message-one", "conversation-one")
    first = services.send_message(domain.owner, domain.conversation.id, "크롬에서 발생합니다", [], "second")
    again = services.send_message(domain.owner, domain.conversation.id, "크롬에서 발생합니다", [], "second")
    assert first.id == again.id
    assert_error("IDEMPOTENCY_CONFLICT", services.send_message, domain.owner, domain.conversation.id, "다른 설명", [], "second")
    assert Conversation.objects.count() == 1 and Message.objects.filter(kind="USER").count() == 2
    assert Job.objects.filter(kind="CONVERSATION").count() == 2


def test_job_registration_failure_does_not_leave_orphan_message(request_domain):
    domain = request_domain
    before = domain.conversation.messages.count()
    with patch("apps.runs.services.enqueue_job", side_effect=DomainError("QUEUE_UNAVAILABLE")):
        assert_error("QUEUE_UNAVAILABLE", services.send_message, domain.owner, domain.conversation.id, "추가 설명", [], "second")
    assert domain.conversation.messages.count() == before
    assert AIInvocation.objects.filter(purpose="conversation").count() == 1


def test_context_excludes_future_input_but_includes_previous_completed_reply(request_domain):
    domain = request_domain
    first_call = AIInvocation.objects.get(purpose="conversation")
    services.send_message(domain.owner, domain.conversation.id, "두 번째 설명", [], "second")
    second_call = AIInvocation.objects.filter(purpose="conversation").exclude(pk=first_call.pk).get()
    first_context = services.build_context(first_call)
    assert "두 번째 설명" not in first_context
    first_call.state = "SUCCEEDED"
    first_call.save(update_fields=["state"])
    response = services.complete_response(first_call.id, "어느 화면인가요?")
    assert services.complete_response(first_call.id, "어느 화면인가요?").id == response.id
    services.send_message(domain.owner, domain.conversation.id, "미래의 세 번째 설명", [], "third")
    second_context = json.loads(services.build_context(second_call))["untrusted_conversation"]
    assert [message["body"] for message in second_context] == ["취소 버튼이 동작하지 않아요", "어느 화면인가요?", "두 번째 설명"]
    assert response.reply_to_message_id == domain.conversation.messages.get(client_message_id="message-one").id


def test_incomplete_ai_output_is_not_stored_as_answer(request_domain):
    domain = request_domain
    call = AIInvocation.objects.get(purpose="conversation")
    call.state = "FAILED"
    call.save(update_fields=["state"])
    assert_error("AI_RESPONSE_INCOMPLETE", services.complete_response, call.id, "부분 답변")
    assert Message.objects.filter(kind="ASSISTANT").count() == 0


def test_response_retry_is_explicit_new_invocation_and_idempotent(request_domain):
    domain = request_domain
    previous = AIInvocation.objects.get(purpose="conversation")
    assert_error("RESPONSE_NOT_RETRYABLE", services.retry_response, domain.owner, previous.id, "retry")
    previous.state = "FAILED"
    previous.save(update_fields=["state"])
    retry = services.retry_response(domain.owner, previous.id, "retry")
    assert retry.id != previous.id and retry.context_manifest == previous.context_manifest
    assert services.retry_response(domain.owner, previous.id, "retry").id == retry.id
    assert Message.objects.filter(kind="USER").count() == 1
    assert AIInvocation.objects.filter(purpose="conversation").count() == 2


def test_maximum_length_retry_key_fits_internal_job_key(request_domain):
    domain = request_domain
    previous = AIInvocation.objects.get(purpose="conversation")
    previous.state = "FAILED"
    previous.save(update_fields=["state"])
    retry = services.retry_response(domain.owner, previous.id, "r" * 128)
    job = Job.objects.get(subject_id=str(retry.id))
    assert len(job.idempotency_key) <= 128


def test_natural_language_status_context_includes_observation_time(request_domain):
    call = AIInvocation.objects.get(purpose="conversation")
    context = json.loads(services.build_context(call))
    assert context["verified_status"]["observed_at"]
    assert context["verified_status"]["run"] is None
    assert "verified_status" in call.prompt and "observed_at" in call.prompt


def test_polling_ten_times_does_not_register_model_calls(request_domain):
    domain = request_domain
    before = AIInvocation.objects.count()
    for _ in range(10):
        updates = services.get_updates(domain.owner, domain.conversation.id)
        assert updates["messages"][0]["body"] == "취소 버튼이 동작하지 않아요"
    assert AIInvocation.objects.count() == before


def test_foreign_owner_cannot_read_write_retry_or_attach(request_domain):
    domain = request_domain
    call = AIInvocation.objects.get(purpose="conversation")
    assert_error("NOT_FOUND", services.get_conversation, domain.outsider, domain.conversation.id)
    assert_error("NOT_FOUND", services.send_message, domain.outsider, domain.conversation.id, "다른 계정", [], "foreign")
    assert_error("NOT_FOUND", services.retry_response, domain.outsider, call.id, "retry")
    assert_error("NOT_FOUND", services.send_message, domain.admin, domain.conversation.id, "관리자 대리입력", [], "impersonate")
    assert services.get_conversation(domain.admin, domain.conversation.id).id == domain.conversation.id


def test_retry_url_must_match_invocation_conversation(request_domain):
    domain = request_domain
    another = services.start_conversation(domain.owner, domain.project.id, "별도 대화", "other", "other")
    call = AIInvocation.objects.get(purpose="conversation", conversation=domain.conversation)
    assert_error("NOT_FOUND", services.retry_response, domain.owner, call.id, "retry", conversation_id=another.id)


def test_archived_project_is_readonly_and_revocation_blocks_history(request_domain):
    domain = request_domain
    domain.project.state = "ARCHIVED"
    domain.project.save(update_fields=["state"])
    assert services.get_conversation(domain.owner, domain.conversation.id).id == domain.conversation.id
    with pytest.raises(DomainError):
        services.send_message(domain.owner, domain.conversation.id, "새 설명", [], "second")
    assert services.get_updates(domain.owner, domain.conversation.id)["readonly"]
    ProjectMembership.objects.filter(user=domain.owner).delete()
    assert_error("NOT_FOUND", services.get_updates, domain.owner, domain.conversation.id)


def test_user_html_is_escaped_and_natural_language_is_not_execution_consent(request_domain, client):
    domain = request_domain
    services.send_message(domain.owner, domain.conversation.id, "<script>alert('x')</script> git push 실행 승인", [], "injection")
    login(client, domain.owner)
    response = client.get(f"/conversations/{domain.conversation.id}/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "&lt;script&gt;" in body and "<script>alert" not in body
    assert "name=\"model\"" not in body and "name=\"owner_id\"" not in body
    assert not Run.objects.exists()
    assert client.get(f"/conversations/{domain.conversation.id}/messages/").status_code == 405


def test_changes_require_csrf_and_post(request_domain):
    domain = request_domain
    client = Client(enforce_csrf_checks=True)
    login(client, domain.owner)
    response = client.post(f"/conversations/{domain.conversation.id}/messages/", {"body": "CSRF 없음", "client_message_id": "bad"})
    assert response.status_code == 403
    assert not Message.objects.filter(client_message_id="bad").exists()


def test_length_errors_preserve_original_and_do_not_truncate(request_domain):
    domain = request_domain
    assert_error("INVALID_INPUT", services.send_message, domain.owner, domain.conversation.id, "x" * 10001, [], "long")
    assert_error("INVALID_INPUT", services.rename_conversation, domain.owner, domain.conversation.id, "x" * 121)
    assert not Message.objects.filter(client_message_id="long").exists()


def test_lists_filter_other_manager_but_allow_admin_operational_view(request_domain):
    domain = request_domain
    assert list(services.list_conversations(domain.outsider, domain.project.id)) == []
    assert list(services.list_conversations(domain.owner, domain.project.id)) == [domain.conversation]
    assert list(services.list_conversations(domain.admin, domain.project.id)) == [domain.conversation]


def test_polling_returns_current_response_label_and_safe_failure_guidance(request_domain, client):
    domain = request_domain
    call = AIInvocation.objects.get(purpose="conversation")
    login(client, domain.owner)
    initial = client.get(f"/conversations/{domain.conversation.id}/")
    assert "답변 상태: 대기" in initial.content.decode()
    call.state = "FAILED"
    call.error_code = "SANDBOX_UNAVAILABLE"
    call.final_response = "secret-cli-token <script>alert(1)</script> /private/host/path"
    call.save(update_fields=["state", "error_code", "final_response"])
    update = client.get(f"/conversations/{domain.conversation.id}/updates/").json()["responses"][0]
    assert update["state"] == "FAILED" and update["label"] == "실패"
    assert "관리자" in update["message"] and "실행 환경" in update["message"]
    assert "secret-cli-token" not in json.dumps(update)
    assert "script" not in json.dumps(update) and "/private" not in json.dumps(update)
    refreshed = client.get(f"/conversations/{domain.conversation.id}/").content.decode()
    assert "답변 상태: 실패" in refreshed and update["message"] in refreshed


@pytest.mark.skipif(shutil.which("node") is None, reason="Node가 없어 JavaScript DOM 회귀 검사를 실행할 수 없습니다.")
def test_polling_javascript_replaces_stale_response_card_without_reload():
    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor() { this.dataset = {}; this.children = []; this.textContent = ""; }
  append(...items) { this.children.push(...items); }
  querySelector(selector) {
    const key = {"[data-response-label]": "responseLabel", "[data-response-message]": "responseMessage"}[selector];
    return this.children.find(child => key in child.dataset) || null;
  }
}
const nodes = new Map();
const messages = new Element(); messages.dataset.updatesUrl = "/conversations/test/updates/";
nodes.set("conversation-messages", messages);
nodes.set("conversation-status", new Element());
const responses = new Element();
const card = new Element(); card.dataset.responseId = "call-1";
const label = new Element(); label.dataset.responseLabel = ""; label.textContent = "답변 상태: 대기";
const message = new Element(); message.dataset.responseMessage = "";
card.append(label, message); responses.append(card); nodes.set("response-list", responses);
const scheduled = [];
const document = {hidden: false, getElementById: id => nodes.get(id) || null, createElement: () => new Element()};
const update = {messages: [], readonly: false, latest_run: null, responses: [
  {id: "call-1", state: "FAILED", label: "실패", retryable: true, message: "실행 환경 준비가 필요합니다. 관리자에게 문의해 주세요."}
]};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  document, window: {setTimeout: callback => scheduled.push(callback)},
  fetch: async () => ({ok: true, redirected: false, json: async () => update}),
});
(async () => {
  await scheduled.shift()();
  assert.equal(label.textContent, "답변 상태: 실패");
  assert.equal(message.textContent, update.responses[0].message);
  assert.match(nodes.get("conversation-status").textContent, /완료하지 못한 답변/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    source = Path(__file__).resolve().parents[2] / "static" / "js" / "conversation.js"
    result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
