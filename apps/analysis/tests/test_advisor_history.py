"""Deleting an advisor thread from the history list.

A thread can quote a contract the next reviewer is not cleared to see, so the
interesting cases here are about ownership, not about the happy path.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.ai.models import AdvisorConversation, AdvisorMessage

pytestmark = pytest.mark.django_db


@pytest.fixture
def conversation(reviewer):
    convo = AdvisorConversation.objects.create(user=reviewer, title="سقف الغرامة")
    AdvisorMessage.objects.create(
        conversation=convo, role=AdvisorMessage.Role.USER, body="سؤال"
    )
    return convo


@pytest.fixture
def other_user(entity):
    return User.objects.create_user(
        email="someone-else@aji.test", password="test-password-1234",
        role=Role.CONTRACT_REVIEWER, entity=entity,
    )


class TestDeleteConversation:
    def test_the_owner_can_delete_their_thread(self, signed_in, conversation):
        url = reverse("analysis:advisor_delete", args=[conversation.pk])
        response = signed_in.post(url)

        assert response.status_code == 302
        assert not AdvisorConversation.objects.filter(pk=conversation.pk).exists()

    def test_its_messages_go_with_it(self, signed_in, conversation):
        signed_in.post(reverse("analysis:advisor_delete", args=[conversation.pk]))
        assert not AdvisorMessage.objects.filter(conversation_id=conversation.pk).exists()

    def test_another_reader_cannot_delete_it(self, client, other_user, conversation):
        """Scoped by the queryset, so a foreign id finds nothing. The thread
        survives and the response says nothing about it existing."""
        client.force_login(other_user)
        response = client.post(
            reverse("analysis:advisor_delete", args=[conversation.pk])
        )

        assert response.status_code == 302
        assert AdvisorConversation.objects.filter(pk=conversation.pk).exists()

    def test_get_does_not_delete(self, signed_in, conversation):
        """A link that deletes is one a browser can follow while prefetching."""
        signed_in.get(reverse("analysis:advisor_delete", args=[conversation.pk]))
        assert AdvisorConversation.objects.filter(pk=conversation.pk).exists()

    def test_signing_in_is_required(self, client, conversation):
        response = client.post(
            reverse("analysis:advisor_delete", args=[conversation.pk])
        )
        assert response.status_code == 302
        assert "login" in response["Location"]
        assert AdvisorConversation.objects.filter(pk=conversation.pk).exists()

    def test_deleting_the_open_thread_clears_the_session(self, signed_in, conversation):
        """Otherwise the next page load reopens a thread that is gone."""
        from apps.analysis.views import AdvisorView

        session = signed_in.session
        session[AdvisorView.SESSION_KEY] = str(conversation.pk)
        session.save()

        signed_in.post(reverse("analysis:advisor_delete", args=[conversation.pk]))
        assert AdvisorView.SESSION_KEY not in signed_in.session

    def test_deleting_another_thread_leaves_the_open_one_alone(
        self, signed_in, reviewer, conversation
    ):
        from apps.analysis.views import AdvisorView

        other = AdvisorConversation.objects.create(user=reviewer, title="محادثة أخرى")
        session = signed_in.session
        session[AdvisorView.SESSION_KEY] = str(conversation.pk)
        session.save()

        signed_in.post(reverse("analysis:advisor_delete", args=[other.pk]))
        assert signed_in.session[AdvisorView.SESSION_KEY] == str(conversation.pk)


class TestHistoryMarkup:
    def test_each_thread_offers_a_delete_control(self, signed_in, conversation):
        response = signed_in.get(reverse("analysis:advisor"))
        body = response.content.decode()

        assert reverse("analysis:advisor_delete", args=[conversation.pk]) in body
        assert "navsub__delbtn" in body

    def test_the_control_is_a_post_form_not_a_link(self, signed_in, conversation):
        """A bare link would delete on a prefetch or a crawl."""
        body = signed_in.get(reverse("analysis:advisor")).content.decode()
        url = reverse("analysis:advisor_delete", args=[conversation.pk])

        assert f'action="{url}"' in body
        assert f'href="{url}"' not in body
        assert "csrfmiddlewaretoken" in body


class TestConfirmDialogSafety:
    """The confirmation must not be able to confirm itself.

    A <form method="dialog"> submits with the FIRST submit button when Enter
    is pressed. With "نعم" first, an Enter keypress while the dialog was open
    deleted the thread with no further input -- which is exactly what the
    dialog exists to prevent, and what happened during testing.
    """

    def test_cancel_comes_before_confirm(self, signed_in, conversation):
        body = signed_in.get(reverse("analysis:advisor")).content.decode()

        cancel = body.index('value="cancel"')
        confirm = body.index('value="confirm"')
        assert cancel < confirm, (
            "the destructive button is first in the dialog, so Enter deletes"
        )

    def test_the_dialog_names_what_is_being_deleted(self, signed_in, conversation):
        """"Are you sure?" on its own does not say which of eight threads."""
        body = signed_in.get(reverse("analysis:advisor")).content.decode()

        assert 'data-confirm-name="سقف الغرامة"' in body
        assert "data-confirm-dialog" in body

    def test_the_dialog_is_not_nested_in_a_form(self, signed_in, conversation):
        """Its buttons would submit that form instead of closing it."""
        body = signed_in.get(reverse("analysis:advisor")).content.decode()
        before = body[: body.index("data-confirm-dialog")]

        assert before.count("<form") == before.count("</form>"), (
            "an unclosed <form> precedes the dialog, so the dialog is inside it"
        )


class TestTheOpeningScreen:
    """No free chat: with no file chosen the chat offers its list of analysed
    files, not questions -- "حلّل هذا العقد" with nothing chosen would ask
    the model to analyse a document it was never given."""

    def test_without_a_file_the_list_of_analysed_files_is_shown(self, signed_in, chat_file):
        body = signed_in.get(reverse("analysis:advisor")).content.decode()
        assert not _suggestion_forms(body)
        assert f"?contract={chat_file.pk}" in body and 'class="chatfile"' in body

    def test_with_a_file_its_suggestions_carry_it(self, signed_in, chat_file):
        body = signed_in.get(reverse("analysis:advisor") + f"?contract={chat_file.pk}").content.decode()
        assert len(_suggestion_forms(body)) == 4
        assert body.count(f'name="contract" value="{chat_file.pk}"') == 4


def _suggestion_forms(html):
    """[(needs_contract, is_hidden)] for each suggestion form in `html`."""
    import re

    out = []
    for tag in re.findall(r"<form[^>]*data-suggestion-form[^>]*>", html):
        out.append(('data-needs-contract="1"' in tag, "hidden" in tag))
    return out


class TestErrorTurn:
    """A question that could not be answered is shown as a failure, not as a
    quieter answer. The two must not look alike."""

    @pytest.fixture
    def failed_turn(self, reviewer):
        convo = AdvisorConversation.objects.create(user=reviewer, title="سؤال")
        AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.USER, body="سؤال"
        )
        AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.ASSISTANT,
            error="تعذّر الوصول إلى خدمة الذكاء الاصطناعي.", provider="ajibrain",
        )
        return convo

    def test_a_failure_is_marked_as_one(self, signed_in, failed_turn):
        body = signed_in.get(
            reverse("analysis:advisor") + f"?c={failed_turn.pk}"
        ).content.decode()

        assert 'class="failure"' in body
        assert 'role="alert"' in body
        assert "تعذّر إتمام هذا السؤال" in body

    def test_the_reason_is_shown_to_the_reader(self, signed_in, failed_turn):
        body = signed_in.get(
            reverse("analysis:advisor") + f"?c={failed_turn.pk}"
        ).content.decode()
        assert "تعذّر الوصول إلى خدمة الذكاء الاصطناعي." in body


class TestAnswerProvenance:
    def test_a_grounded_answer_names_its_contract(self, signed_in, reviewer):
        """"Based on the contract" reads as confirmation it was the right one."""
        convo = AdvisorConversation.objects.create(user=reviewer, title="سؤال")
        AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.ASSISTANT,
            body="الإجابة", grounded=True, contract_name="Jeddah Corniche",
            provider="ajibrain", model="qwen3.8:27b",
        )
        body = signed_in.get(
            reverse("analysis:advisor") + f"?c={convo.pk}"
        ).content.decode()

        assert "Jeddah Corniche" in body

    def test_the_footer_names_the_service_not_the_model(self, signed_in, reviewer):
        """The model identifier is provenance; it belongs on the record and in
        the tooltip, not in the middle of a conversation."""
        convo = AdvisorConversation.objects.create(user=reviewer, title="سؤال")
        AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.ASSISTANT,
            body="الإجابة", provider="ajibrain", model="qwen3.8:27b",
        )
        body = signed_in.get(
            reverse("analysis:advisor") + f"?c={convo.pk}"
        ).content.decode()

        assert ">AJi Brain<" in body
        assert ">ajibrain · qwen3.8:27b<" not in body
        # Still recoverable by an operator hovering the line.
        assert 'title="ajibrain · qwen3.8:27b"' in body
