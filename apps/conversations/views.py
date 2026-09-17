import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.accounts.services import require_access
from apps.errors import DomainError
from apps.projects.models import Project

from . import services
from .forms import MessageForm, RenameForm, StartConversationForm


def _can_edit(actor, conversation):
    try:
        require_access(actor, "conversation.edit", project_id=conversation.project_id, obj=conversation)
        return conversation.project.state == "ACTIVE"
    except DomainError:
        return False


def _detail_context(actor, conversation, form=None, error=None):
    from apps.ai.models import AIInvocation

    calls = list(AIInvocation.objects.filter(conversation=conversation, purpose="conversation"))
    for call in calls:
        display = services.response_status(call)
        call.retry_key = str(uuid.uuid4())
        call.retryable = display["retryable"]
        call.response_label = display["label"]
        call.response_message = display["message"]
    return {"conversation": conversation, "chat_messages": conversation.messages.all(), "calls": calls,
            "form": form or MessageForm(initial={"client_message_id": str(uuid.uuid4())}),
            "rename_form": RenameForm(initial={"title": conversation.title}),
            "can_edit": _can_edit(actor, conversation), "domain_error": error}


@login_required
@require_http_methods(["GET", "POST"])
def project_conversations(request, project_id):
    conversations = services.list_conversations(request.user, project_id)
    project = Project.objects.get(pk=project_id)
    form = StartConversationForm(request.POST or None, initial={"client_message_id": str(uuid.uuid4()), "creation_key": str(uuid.uuid4())})
    if request.method == "POST" and form.is_valid():
        try:
            conversation = services.start_conversation(request.user, project_id, **form.cleaned_data)
            return redirect("conversations:detail", conversation_id=conversation.id)
        except DomainError as error:
            form.add_error(None, error.message)
            return render(request, "conversations/list.html", {"form": form, "project": project, "conversations": conversations}, status=error.status)
    return render(request, "conversations/list.html", {"form": form, "project": project, "conversations": conversations}, status=400 if request.method == "POST" else 200)


@login_required
@require_GET
def detail(request, conversation_id):
    conversation = services.get_conversation(request.user, conversation_id)
    return render(request, "conversations/detail.html", _detail_context(request.user, conversation))


@login_required
@require_POST
def message(request, conversation_id):
    conversation = services.get_conversation(request.user, conversation_id, "conversation.edit")
    form = MessageForm(request.POST)
    error = None
    if form.is_valid():
        try:
            services.send_message(request.user, conversation_id, **form.cleaned_data)
            return redirect("conversations:detail", conversation_id=conversation_id)
        except DomainError as caught:
            error = caught
            form.add_error(None, caught.message)
    return render(request, "conversations/detail.html", _detail_context(request.user, conversation, form, error), status=error.status if error else 400)


@login_required
@require_POST
def rename(request, conversation_id):
    form = RenameForm(request.POST)
    if not form.is_valid():
        raise DomainError("INVALID_INPUT", "제목은 1~120자로 입력해 주세요.")
    services.rename_conversation(request.user, conversation_id, form.cleaned_data["title"])
    return redirect("conversations:detail", conversation_id=conversation_id)


@login_required
@require_GET
def updates(request, conversation_id):
    response = JsonResponse(services.get_updates(request.user, conversation_id, request.GET.get("after", 0)))
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def retry_response(request, conversation_id, call_id):
    services.retry_response(request.user, call_id, request.POST.get("idempotency_key"), conversation_id=conversation_id)
    return redirect("conversations:detail", conversation_id=conversation_id)


@login_required
@require_POST
def create_request(request, conversation_id):
    from apps.work_requests.services import create_or_get_draft

    work_request = create_or_get_draft(request.user, conversation_id)
    return redirect("work_requests:detail", request_id=work_request.id)
