"""The assistant's memory, as a page a reader can act on.

The control sits in Settings beside three administrator tabs, and it is the
one tab that is nobody's to administer: every reader has conversations, every
reader clears their own, and no one sees anyone else's count.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Entity, Role, User
from apps.ai.models import AdvisorConversation, AdvisorMessage

pytestmark = pytest.mark.django_db

SETTINGS = reverse("accounts:settings")
ASSISTANT = SETTINGS + "?tab=assistant"


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi Memory", code="aji-mem", country="SA")


@pytest.fixture
def reader(entity):
    return User.objects.create_user(
        email="reader@aji.test", password="a-long-enough-passphrase-42",
        role=Role.CONTRACT_REVIEWER, entity=entity,
    )


@pytest.fixture
def admin(entity):
    return User.objects.create_user(
        email="boss@aji.test", password="a-long-enough-passphrase-42",
        role=Role.ADMIN, entity=entity,
    )


def thread(user, *, remembered=True, answered=True):
    convo = AdvisorConversation.objects.create(
        user=user, title="غرامة التأخير", remembered=remembered)
    AdvisorMessage.objects.create(
        conversation=convo, role=AdvisorMessage.Role.USER,
        body="ما سقف غرامة التأخير؟")
    if answered:
        AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.ASSISTANT,
            body="حتى 10% من قيمة العقد.")
    return convo


class TestGettingThere:
    def test_a_reader_without_admin_rights_lands_on_it(self, client, reader):
        """"users" is the right landing tab for an administrator and a locked
        door for everyone else."""
        client.force_login(reader)
        response = client.get(SETTINGS)
        assert response.context["tab"] == "assistant"

    def test_an_administrator_still_lands_on_users(self, client, admin):
        client.force_login(admin)
        assert client.get(SETTINGS).context["tab"] == "users"

    def test_an_administrator_can_still_open_it(self, client, admin):
        client.force_login(admin)
        assert client.get(ASSISTANT).context["tab"] == "assistant"

    def test_it_needs_no_permission_of_its_own(self, client, reader):
        client.force_login(reader)
        body = client.get(ASSISTANT).content.decode()
        assert "ذاكرة المساعد" in body
        assert "users_denied" not in body

    def test_signing_in_is_still_required(self, client):
        assert client.get(ASSISTANT).status_code == 302

    def test_the_sidebar_links_to_it(self, client, reader):
        client.force_login(reader)
        assert "?tab=assistant" in client.get(ASSISTANT).content.decode()


class TestWhatItReports:
    def test_it_counts_only_what_answers_would_draw_on(self, client, reader):
        """The number has to be the number the assistant would use. A count
        that includes cleared threads makes clearing look like it failed."""
        thread(reader)
        thread(reader)
        thread(reader, remembered=False)
        client.force_login(reader)

        context = client.get(ASSISTANT).context
        assert context["remembered_count"] == 2
        assert context["conversation_count"] == 3

    def test_it_counts_nobody_else(self, client, reader, admin):
        thread(admin)
        thread(admin)
        thread(reader)
        client.force_login(reader)

        context = client.get(ASSISTANT).context
        assert context["remembered_count"] == 1
        assert context["conversation_count"] == 1

    def test_with_nothing_to_clear_it_says_so(self, client, reader):
        client.force_login(reader)
        body = client.get(ASSISTANT).content.decode()
        assert "لا توجد محادثات بعد" in body
        assert "advisor/forget" not in body

    def test_once_cleared_it_says_the_threads_are_still_there(self, client, reader):
        thread(reader, remembered=False)
        client.force_login(reader)
        body = client.get(ASSISTANT).content.decode()
        assert "الذاكرة ممسوحة" in body
        # No button: there is nothing left to clear.
        assert "advisor/forget" not in body

    def test_the_button_appears_when_there_is_something_to_clear(self, client, reader):
        thread(reader)
        client.force_login(reader)
        body = client.get(ASSISTANT).content.decode()
        assert "advisor/forget" in body

    def test_the_confirmation_does_not_threaten_deletion(self, client, reader):
        """The control reuses the delete dialog, which would otherwise tell a
        reader their conversations are about to be destroyed irreversibly."""
        thread(reader)
        client.force_login(reader)
        body = client.get(ASSISTANT).content.decode()
        assert "data-confirm-safe=\"1\"" in body
        assert "لن يُحذف شيء" in body


class TestClearingFromHere:
    def test_it_returns_to_the_tab_it_was_pressed_on(self, client, reader):
        thread(reader)
        client.force_login(reader)
        response = client.post(reverse("analysis:advisor_forget"))
        assert response.status_code == 302
        assert response.url == ASSISTANT

    def test_the_page_then_reports_nothing_remembered(self, client, reader):
        thread(reader)
        client.force_login(reader)
        client.post(reverse("analysis:advisor_forget"))

        context = client.get(ASSISTANT).context
        assert context["remembered_count"] == 0
        assert context["conversation_count"] == 1

    def test_a_new_conversation_is_remembered_again(self, client, reader):
        """Clearing is not a switch that stays off, and the page says so --
        a reader who thinks it is would be wrong about what happens next."""
        thread(reader)
        client.force_login(reader)
        client.post(reverse("analysis:advisor_forget"))

        thread(reader)
        assert client.get(ASSISTANT).context["remembered_count"] == 1
