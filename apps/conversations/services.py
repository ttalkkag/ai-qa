import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.accounts.services import filter_owned_queryset, require_access
from apps.errors import DomainError
from apps.projects.models import Project
from apps.projects.services import assert_project_active

from .models import Conversation, Message, ResponseRetry


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise DomainError("INVALID_INPUT", "중복 방지 키를 다시 확인해 주세요.")
    return value


def get_conversation(actor, conversation_id, action="conversation.view"):
    try:
        conversation = Conversation.objects.select_related("project", "owner").get(pk=conversation_id)
    except (Conversation.DoesNotExist, ValueError, ValidationError):
        raise DomainError("NOT_FOUND", status=404)
    require_access(actor, action, project_id=conversation.project_id, obj=conversation)
    return conversation


def list_conversations(actor, project_id):
    require_access(actor, "project.view", project_id=project_id)
    return filter_owned_queryset(actor, Conversation.objects.filter(project_id=project_id))


def _body(body):
    if not isinstance(body, str) or not body.strip() or len(body) > 10000:
        raise DomainError("INVALID_INPUT", "설명은 1~10,000자로 입력해 주세요.")
    return body


def _next_sequence(conversation):
    Conversation.objects.filter(pk=conversation.pk).update(next_sequence=F("next_sequence") + 1, updated_at=timezone.now())
    conversation.refresh_from_db(fields=["next_sequence"])
    return conversation.next_sequence - 1


def _enqueue_response(actor, conversation, message, key):
    from apps.ai.models import AIInvocation
    from apps.ai.services import profile_snapshot
    from apps.runs.services import enqueue_job

    profile = profile_snapshot("conversation")
    invocation = AIInvocation.objects.create(
        actor=actor, conversation=conversation, purpose="conversation",
        profile_snapshot=profile,
        requested_model=profile["model_id"], requested_effort=profile["effort"],
        context_manifest={"target_message_id": str(message.id), "through_sequence": message.sequence},
        prompt="사용자의 최신 요구사항을 분석하고 질문에 답하세요. 허용된 읽기 전용 프로젝트 코드와 대화 자료를 확인하여 관련 도메인, 누락 사항, 영향 범위, 충돌 가능성을 설명하세요. 이 단계에서는 파일을 수정하지 않습니다. 핵심 의도가 모호하면 질문 1~3개로 확인하고 모르는 사실은 만들지 마세요. 자연어 동의는 실행이나 발행 승인이 아닙니다. 실행 상태를 질문한 경우에만 verified_status 조회 결과와 observed_at 기준 시각으로 답하세요. run이 없다는 이유로 일반 질문이나 요구사항 분석을 거절하지 마세요. 대화나 모델 응답으로 실행 상태를 바꾸지 않습니다.",
    )
    enqueue_job(kind="CONVERSATION", subject_id=invocation.id, actor_id=actor.id,
                project_id=conversation.project_id, payload={"invocation_id": str(invocation.id)},
                key=key, slot="CONVERSATION")
    return invocation


@transaction.atomic
def start_conversation(actor, project_id, body, client_message_id, creation_key):
    body = _body(body)
    check_key(client_message_id)
    check_key(creation_key)
    actor = require_access(actor, "conversation.create", project_id=project_id)
    try:
        project = Project.objects.get(pk=project_id)
    except (Project.DoesNotExist, ValueError, ValidationError):
        raise DomainError("NOT_FOUND", status=404)
    assert_project_active(project)
    payload_hash = digest({"body": body, "client_message_id": client_message_id})
    conversation, created = Conversation.objects.get_or_create(
        owner=actor, project=project, creation_key=creation_key,
        defaults={"title": body.strip()[:120], "creation_hash": payload_hash},
    )
    if not created:
        if conversation.creation_hash != payload_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "같은 전송 키에 다른 내용이 있습니다.", status=409)
        return conversation
    send_message(actor, conversation.id, body, [], client_message_id)
    return conversation


@transaction.atomic
def send_message(actor, conversation_id, body, attachment_ids, client_message_id):
    body = _body(body)
    check_key(client_message_id)
    conversation = get_conversation(actor, conversation_id, "conversation.edit")
    assert_project_active(conversation.project)
    if not isinstance(attachment_ids, list) or len(attachment_ids) > 3:
        raise DomainError("INVALID_INPUT", "한 메시지에는 첨부를 3개까지 선택할 수 있습니다.")
    attachment_ids = [str(value) for value in attachment_ids]
    payload_hash = digest({"body": body, "attachment_ids": attachment_ids})
    existing = Message.objects.filter(conversation=conversation, client_message_id=client_message_id).first()
    if existing:
        if existing.payload_hash != payload_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
        return existing
    if attachment_ids:
        from apps.artifacts.services import snapshot_attachments
        snapshot_attachments(actor, conversation.id, attachment_ids)
    message = Message.objects.create(
        conversation=conversation, sequence=_next_sequence(conversation),
        kind=Message.Kind.USER, body=body, actor_id=actor.pk,
        client_message_id=client_message_id, payload_hash=payload_hash, attachment_ids=attachment_ids,
    )
    from apps.artifacts.services import touch_conversation_attachments
    touch_conversation_attachments(conversation.id)
    _enqueue_response(actor, conversation, message, f"message:{message.id}")
    return message


@transaction.atomic
def rename_conversation(actor, conversation_id, title):
    conversation = get_conversation(actor, conversation_id, "conversation.edit")
    assert_project_active(conversation.project)
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise DomainError("INVALID_INPUT", "제목은 1~120자로 입력해 주세요.")
    conversation.title = title
    conversation.save(update_fields=["title", "updated_at"])
    return conversation


def build_context(invocation):
    conversation = get_conversation(invocation.actor, invocation.conversation_id, "conversation.edit")
    assert_project_active(conversation.project)
    try:
        target = conversation.messages.get(pk=invocation.context_manifest["target_message_id"], kind=Message.Kind.USER)
    except (KeyError, Message.DoesNotExist, ValidationError):
        raise DomainError("CONTEXT_INVALID")
    messages = list(conversation.messages.filter(
        Q(kind=Message.Kind.USER, sequence__lte=target.sequence)
        | Q(kind=Message.Kind.ASSISTANT, reply_to_message__sequence__lte=target.sequence)
    ).select_related("reply_to_message"))
    messages.sort(key=lambda message: (
        message.reply_to_message.sequence if message.reply_to_message_id else message.sequence,
        1 if message.kind == Message.Kind.ASSISTANT else 0,
        message.sequence,
    ))
    attachments = []
    for message in messages:
        if message.attachment_ids:
            from apps.artifacts.services import snapshot_attachments
            attachments.extend(snapshot_attachments(invocation.actor, conversation.id, message.attachment_ids))
    from apps.runs.models import Run
    latest_run = Run.objects.filter(request_revision__request__conversation=conversation).first()
    verified_status = {"observed_at": timezone.now().isoformat(), "run": None}
    if latest_run:
        verified_status["run"] = {"id": str(latest_run.id), "status": latest_run.status,
                                  "label": latest_run.get_status_display(), "stage": latest_run.current_stage}
    context = json.dumps({"verified_status": verified_status, "untrusted_conversation": [
        {"id": str(message.id), "kind": message.kind, "body": message.body, "attachment_ids": message.attachment_ids}
        for message in messages
    ]}, ensure_ascii=False)
    if len(context) > 50000:
        raise DomainError("CONTEXT_TOO_LARGE", "대화가 길어졌습니다. 새 대화에서 핵심 내용을 정리해 주세요.")
    invocation.context_manifest = {**invocation.context_manifest,
        "message_ids": [str(message.id) for message in messages], "attachments": attachments}
    invocation.save(update_fields=["context_manifest"])
    return context


@transaction.atomic
def complete_response(invocation_id, body):
    from apps.ai.models import AIInvocation

    invocation = AIInvocation.objects.select_related("actor", "conversation").get(pk=invocation_id)
    conversation = get_conversation(invocation.actor, invocation.conversation_id, "conversation.edit")
    assert_project_active(conversation.project)
    existing = Message.objects.filter(ai_invocation=invocation).first()
    if existing:
        return existing
    if invocation.state != "SUCCEEDED" or not isinstance(body, str) or not body.strip():
        raise DomainError("AI_RESPONSE_INCOMPLETE")
    target = conversation.messages.get(pk=invocation.context_manifest["target_message_id"], kind=Message.Kind.USER)
    return Message.objects.create(conversation=conversation, sequence=_next_sequence(conversation),
        kind=Message.Kind.ASSISTANT, body=body, reply_to_message=target, ai_invocation=invocation)


@transaction.atomic
def retry_response(actor, call_id, idempotency_key, *, conversation_id=None):
    from apps.ai.models import AIInvocation

    check_key(idempotency_key)
    try:
        previous = AIInvocation.objects.get(pk=call_id, purpose="conversation")
    except (AIInvocation.DoesNotExist, ValueError, ValidationError):
        raise DomainError("NOT_FOUND", status=404)
    if conversation_id is not None and str(previous.conversation_id) != str(conversation_id):
        raise DomainError("NOT_FOUND", status=404)
    conversation = get_conversation(actor, previous.conversation_id, "conversation.edit")
    assert_project_active(conversation.project)
    existing = ResponseRetry.objects.filter(actor=actor, key=idempotency_key).first()
    if existing:
        if existing.previous_invocation_id != previous.id:
            raise DomainError("IDEMPOTENCY_CONFLICT", status=409)
        return existing.invocation
    if previous.state not in {"FAILED", "CANCELLED", "INTERRUPTED"}:
        raise DomainError("RESPONSE_NOT_RETRYABLE", status=409)
    target = conversation.messages.get(pk=previous.context_manifest["target_message_id"])
    invocation = _enqueue_response(actor, conversation, target, f"response-retry:{digest([str(actor.id), idempotency_key])}")
    ResponseRetry.objects.create(actor=actor, key=idempotency_key, previous_invocation=previous, invocation=invocation)
    return invocation


def response_status(call):
    labels = {"QUEUED": "대기", "RUNNING": "답변 작성 중", "SUCCEEDED": "완료",
              "FAILED": "실패", "CANCELLED": "취소", "INTERRUPTED": "중단"}
    message = ""
    if call.state == "FAILED":
        if call.error_code in {"SANDBOX_UNAVAILABLE", "CONFIG_UNSUPPORTED", "EXECUTION_BLOCKED", "DOCKER_UNAVAILABLE", "DOCKER_IMAGE_MISSING"}:
            message = "실행 환경 준비가 필요합니다. 관리자에게 문의해 주세요."
        elif call.error_code in {"CODEX_NOT_FOUND", "LOGIN_REQUIRED"}:
            message = "AI 연결 준비가 필요합니다. 관리자에게 문의해 주세요."
        elif call.error_code in {"MODEL_UNAVAILABLE", "MODEL_MISMATCH"}:
            message = "지정된 AI 모델을 사용할 수 없습니다. 관리자 확인이 필요합니다."
        elif call.error_code == "RATE_LIMITED":
            message = "AI 이용 한도로 답변을 중단했습니다. 잠시 후 다시 요청해 주세요."
        elif call.error_code == "CLI_TIMEOUT":
            message = "답변 시간이 초과되었습니다. 다시 요청할 수 있습니다."
        elif call.error_code == "CONTEXT_TOO_LARGE":
            message = "대화가 길어졌습니다. 새 대화에서 핵심 내용을 정리해 주세요."
        else:
            message = "답변을 완료하지 못했습니다. 다시 요청하거나 관리자에게 문의해 주세요."
    elif call.state in {"CANCELLED", "INTERRUPTED"}:
        message = "답변이 중단되었습니다. 다시 요청할 수 있습니다."
    return {"id": str(call.id), "state": call.state, "label": labels.get(call.state, "상태 확인 필요"),
            "message": message, "retryable": call.state in {"FAILED", "CANCELLED", "INTERRUPTED"}}


def get_updates(actor, conversation_id, after=0):
    from apps.ai.models import AIInvocation

    conversation = get_conversation(actor, conversation_id)
    try:
        after = int(after)
    except (ValueError, TypeError):
        raise DomainError("INVALID_INPUT")
    if after < 0:
        raise DomainError("INVALID_INPUT")
    messages = [{"id": str(message.id), "sequence": message.sequence, "kind": message.kind,
                 "body": message.body, "created_at": message.created_at.isoformat()}
                for message in conversation.messages.filter(sequence__gt=after)]
    calls = [response_status(call) for call in AIInvocation.objects.filter(conversation=conversation, purpose="conversation")]
    from apps.runs.models import Run
    latest_run = Run.objects.filter(request_revision__request__conversation=conversation).first()
    run_state = None if latest_run is None else {"id": str(latest_run.id), "status": latest_run.status, "label": latest_run.get_status_display()}
    return {"messages": messages, "responses": calls, "readonly": conversation.project.state != "ACTIVE", "latest_run": run_state}
