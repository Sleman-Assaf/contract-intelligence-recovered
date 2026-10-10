"""Per-call AI telemetry (§45).

Records what was asked of which model and how it went — never the contract
text itself, and never a secret.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel


class AIRequestLog(UUIDPrimaryKeyModel):
    class Status(models.TextChoices):
        SUCCESS = "success", _("Success")
        INVALID_RESPONSE = "invalid_response", _("Invalid response")
        UNAVAILABLE = "unavailable", _("Provider unavailable")
        RATE_LIMITED = "rate_limited", _("Rate limited")
        NOT_CONFIGURED = "not_configured", _("Provider not configured")
        ERROR = "error", _("Error")

    provider = models.CharField(max_length=32, db_index=True)
    model = models.CharField(max_length=128, blank=True)
    request_type = models.CharField(max_length=48, db_index=True)

    analysis_run = models.ForeignKey(
        "analysis.AnalysisRun", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="ai_requests",
    )
    contract_id = models.CharField(max_length=64, blank=True, db_index=True)

    prompt_template_version = models.CharField(max_length=32, blank=True)
    analysis_version = models.CharField(max_length=32, blank=True)

    status = models.CharField(max_length=24, choices=Status.choices, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=1)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    #: Answered from AIResponseCache, without calling the model.
    cached = models.BooleanField(default=False)

    started_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["provider", "-started_at"]),
            models.Index(fields=["status", "-started_at"]),
            models.Index(fields=["contract_id", "-started_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.provider}/{self.request_type} → {self.status}"


class AdvisorConversationQuerySet(models.QuerySet):
    def recallable_for(self, user):
        """Threads this reader has left available to later questions."""
        return self.filter(user=user, remembered=True)


class AdvisorConversation(UUIDPrimaryKeyModel, TimeStampedModel):
    """One continuing conversation with the assistant (§38).

    Kept rather than held in the session because an answer here can inform a
    decision on a contract, and "what did it actually say" has to be
    answerable afterwards. Every turn carries the model that produced it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="advisor_conversations",
    )
    #: The contract in scope. Nullable: a conversation may be general, and it
    #: can be pointed at a contract partway through without starting over.
    contract = models.ForeignKey(
        "contracts.Contract", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="advisor_conversations",
    )
    title = models.CharField(
        max_length=200, blank=True,
        help_text=_("Taken from the opening question, for the history list."),
    )

    #: Whether later questions may draw on this thread. Clearing memory
    #: turns this off rather than deleting anything: the conversation is the
    #: reader's record of what they asked, and forgetting it for the purpose
    #: of answering is not the same as destroying it.
    remembered = models.BooleanField(default=True, db_index=True)

    objects = AdvisorConversationQuerySet.as_manager()

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["user", "-updated_at"])]

    def __str__(self) -> str:
        return self.title or str(self.pk)


class AdvisorMessage(UUIDPrimaryKeyModel, TimeStampedModel):
    """One turn. A failed turn is recorded too, as an assistant turn carrying
    the error: a conversation that silently skips a question it could not
    answer misrepresents what happened."""

    class Role(models.TextChoices):
        USER = "user", _("Reviewer")
        ASSISTANT = "assistant", _("Assistant")

    conversation = models.ForeignKey(
        AdvisorConversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    body = models.TextField(blank=True)

    # ── provenance, on assistant turns ───────────────────────────────────
    provider = models.CharField(max_length=32, blank=True)
    model = models.CharField(max_length=128, blank=True)
    #: The excerpts the answer was built from, resolved to their text so the
    #: turn stays readable after the contract or the standards change.
    source_quotes = models.JSONField(default=list, blank=True)
    standard_quotes = models.JSONField(default=list, blank=True)
    grounded = models.BooleanField(
        default=False, help_text=_("Whether a contract was in scope for this turn.")
    )
    #: What was brought in from the reader's earlier conversations, resolved
    #: to text at the time so the turn still reads correctly after those
    #: conversations are edited or deleted -- the same reason the quotes above
    #: are text. Shown under the answer, so a reader can always see what the
    #: assistant was going on besides their question.
    recalled = models.JSONField(default=list, blank=True)
    #: Which contract the answer was read from, as its name at the time. Text
    #: rather than a foreign key, for the same reason the quotes above are
    #: text: the label has to keep saying what was true when the answer was
    #: given, even after the contract is renamed or removed -- and a thread
    #: can be pointed at a different contract partway through, so the
    #: conversation's current one is the wrong thing to show on an old turn.
    contract_name = models.CharField(max_length=200, blank=True)
    error = models.TextField(blank=True)
    #: The reader pressed Stop while the answer was being written. `body` then
    #: holds what they had seen, cleaned of excerpt labels but not validated or
    #: cited -- the page says so beside it.
    stopped = models.BooleanField(default=False)
    #: A photo the reader attached to their question, as a path under
    #: PRIVATE_MEDIA_ROOT (never a public URL). Served back only to its owner.
    image_path = models.CharField(max_length=512, blank=True)
    #: A PDF the reader attached, likewise under PRIVATE_MEDIA_ROOT, and the
    #: name they gave it -- shown on the turn, since a uuid says nothing.
    document_path = models.CharField(max_length=512, blank=True)
    document_name = models.CharField(max_length=200, blank=True)
    #: The pages of the thread's attached PDF this turn was given. A thread
    #: goes on being about its file after the turn that attached it, and a
    #: "continue" picks up after the last page already sent.
    document_pages = models.JSONField(default=list, blank=True)
    #: The reader took this turn's attachment off the thread (the x in the
    #: list). The file and the turn stay; later questions stop carrying it.
    detached = models.BooleanField(default=False)
    #: This question was answered with a file in hand -- attached now or
    #: carried by the thread. Such an exchange is about that file, and the
    #: file belongs to its own chat: it is never recalled into another one.
    used_attachment = models.BooleanField(default=False)
    #: The stored reading of the file this turn was answered from. The file
    #: is read once, when attached; a follow-up is answered from this, never
    #: by reading the file again.
    extracted_file = models.ForeignKey(
        "documents.ExtractedFile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="advisor_messages",
    )
    #: The chunks of that file this turn was given, by ordinal -- so a
    #: "continue" picks up after the last one already sent.
    chunks_sent = models.JSONField(default=list, blank=True)

    #: How each provider is named to a reader. The model identifier is real
    #: provenance and stays on the record and in the tooltip, but it is noise
    #: in the middle of a conversation.
    PROVIDER_LABELS = {"ajibrain": "AJi Brain"}

    @property
    def provider_label(self) -> str:
        return self.PROVIDER_LABELS.get(self.provider, self.provider)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self) -> str:
        return f"{self.role}: {self.body[:40]}"


# ── attachments go with the turn ─────────────────────────────────────────
from django.db.models.signals import post_delete
from django.dispatch import receiver


@receiver(post_delete, sender=AdvisorMessage)
def _remove_attached_photo(sender, instance, **kwargs):
    """The file leaves with the last message that uses it -- deleting a thread
    from the sidebar must not leave the reader's files on disk.

    After the delete, not before: a whole thread goes in one cascade, and
    only once every turn is gone can "is anything still using this file" be
    answered. A file the same reader attached in another chat is still in
    use there and stays.
    """
    from pathlib import Path

    from django.conf import settings

    from apps.documents.chat_files import delete_if_unused
    from apps.documents.models import ExtractedFile

    if instance.extracted_file_id:
        delete_if_unused(instance.extracted_file_id)

    for relative in (instance.image_path, instance.document_path):
        if not relative:
            continue
        still_used = (
            AdvisorMessage.objects.filter(image_path=relative).exists()
            or AdvisorMessage.objects.filter(document_path=relative).exists()
            or ExtractedFile.objects.filter(stored_path=relative).exists()
        )
        if still_used:
            continue
        try:
            root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
            target = (root / relative).resolve()
            if target.is_relative_to(root) and target.is_file():
                target.unlink()
        except OSError:
            pass


class AIResponseCache(models.Model):
    """A validated model answer, reused when exactly the same prompt is asked
    again with the same model.

    Keyed by a hash of the prompt -- the prompt itself is never stored. The
    answer holds what a finding already holds (the model's reading of a
    clause). Temperature is 0, so asking again would give the same answer;
    re-analysing a contract, or a contract built from the same AJi template,
    then costs nothing for the clauses that did not change. A changed clause,
    standard version, related clause, prompt or model is a different prompt.
    """

    key = models.CharField(max_length=64, unique=True)
    request_type = models.CharField(max_length=48, db_index=True)
    model = models.CharField(max_length=128, blank=True)
    raw_text = models.TextField()
    hits = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.request_type} {self.key[:12]} ({self.hits} hits)"
