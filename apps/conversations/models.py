import uuid

from django.conf import settings
from django.db import models


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    creation_key = models.CharField(max_length=128)
    creation_hash = models.CharField(max_length=64)
    title = models.CharField(max_length=120)
    next_sequence = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "id"]
        constraints = [models.UniqueConstraint(
            fields=["owner", "project", "creation_key"], name="conversation_creation_key",
        )]


class Message(models.Model):
    class Kind(models.TextChoices):
        USER = "USER", "내 설명"
        ASSISTANT = "ASSISTANT", "AI 답변"
        EVENT = "EVENT", "진행 안내"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.PROTECT, related_name="messages")
    sequence = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=Kind.choices)
    body = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    client_message_id = models.CharField(max_length=128, null=True, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    reply_to_message = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    ai_invocation = models.OneToOneField("ai.AIInvocation", null=True, blank=True, on_delete=models.PROTECT)
    attachment_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["conversation", "sequence"], name="message_sequence"),
            models.UniqueConstraint(fields=["conversation", "client_message_id"], name="message_client_key"),
        ]


class ResponseRetry(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.CharField(max_length=128)
    previous_invocation = models.ForeignKey("ai.AIInvocation", on_delete=models.PROTECT, related_name="response_retries")
    invocation = models.OneToOneField("ai.AIInvocation", on_delete=models.PROTECT, related_name="response_retry_receipt")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "key"], name="response_retry_key")]
