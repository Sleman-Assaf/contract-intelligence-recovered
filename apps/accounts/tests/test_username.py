"""A name to be called by, which is not a way in.

The site used to show whatever it could find -- a first and last name if an
account had one, otherwise the email address -- so a page of colleagues read
as a page of addresses. Registration now asks for a name, and everywhere a
person is named goes through `display_name`, so it appears throughout without
a template having to change.

Most of what follows guards the other half of that sentence. The field is
called `username` on a Django user model, which is the most natural place in
the world to assume a credential; it is not one, and these tests say so in a
way a future reader cannot miss.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Entity, Role, User

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-enough-passphrase-42"


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi Names", code="aji-names", country="SA")


@pytest.fixture
def signup(client):
    def _signup(**overrides):
        data = {
            "username": "أحمد العلي",
            "email": "ahmad@ajigroup.com",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
        data.update(overrides)
        return client.post(reverse("accounts:signup"), data)
    return _signup


class TestRegistering:
    def test_the_name_is_asked_for_and_kept(self, signup):
        signup()
        assert User.objects.get(email="ahmad@ajigroup.com").username == "أحمد العلي"

    def test_it_is_required(self, signup):
        response = signup(username="")
        assert not User.objects.filter(email="ahmad@ajigroup.com").exists()
        assert response.status_code == 200      # the form came back

    def test_a_single_character_is_not_a_name(self, signup):
        signup(username="أ")
        assert not User.objects.filter(email="ahmad@ajigroup.com").exists()

    def test_whitespace_is_tidied_rather_than_rejected(self, signup):
        """Someone pasting a name should not be told off for the spaces that
        came with it."""
        signup(username="  أحمد   العلي  ")
        assert User.objects.get(email="ahmad@ajigroup.com").username == "أحمد العلي"

    def test_the_email_is_still_required(self, signup):
        signup(email="")
        assert not User.objects.filter(username="أحمد العلي").exists()

    def test_two_people_may_share_a_name(self, client, signup):
        """It is a label, not a key. Insisting on uniqueness would tell the
        second Ahmad that somebody else already owns his name."""
        signup()
        signup(email="ahmad.two@ajigroup.com")
        assert User.objects.filter(username="أحمد العلي").count() == 2

    def test_registering_still_leaves_the_account_inactive(self, signup):
        signup()
        account = User.objects.get(email="ahmad@ajigroup.com")
        assert account.is_active is False
        assert account.role == Role.USER


class TestItIsNotACredential:
    """`USERNAME_FIELD` is the email and stays that way."""

    def test_signing_in_with_the_name_fails(self, client, entity):
        account = User.objects.create_user(
            email="ahmad@ajigroup.com", password=PASSWORD,
            role=Role.CONTRACT_REVIEWER, entity=entity, username="أحمد العلي",
        )
        assert account.is_active

        response = client.post(reverse("accounts:login"), {
            "username": "أحمد العلي", "password": PASSWORD})
        assert not response.wsgi_request.user.is_authenticated

    def test_signing_in_with_the_email_works(self, client, entity):
        User.objects.create_user(
            email="ahmad@ajigroup.com", password=PASSWORD,
            role=Role.CONTRACT_REVIEWER, entity=entity, username="أحمد العلي",
        )
        response = client.post(reverse("accounts:login"), {
            "username": "ahmad@ajigroup.com", "password": PASSWORD})
        assert response.wsgi_request.user.is_authenticated

    def test_the_login_field_is_still_the_email(self):
        assert User.USERNAME_FIELD == "email"


class TestWhatTheSiteCallsThem:
    """`display_name` is the one place that decides, so every screen agrees."""

    def _user(self, entity, **fields):
        return User.objects.create_user(
            email="ahmad@ajigroup.com", password=PASSWORD,
            role=Role.CONTRACT_REVIEWER, entity=entity, **fields)

    def test_the_chosen_name_wins(self, entity):
        account = self._user(entity, username="أحمد العلي",
                             first_name="Ahmad", last_name="Ali")
        assert account.display_name == "أحمد العلي"

    def test_an_older_account_keeps_the_name_it_had(self, entity):
        """Accounts predate the field, and a migration cannot invent names."""
        account = self._user(entity, first_name="Ahmad", last_name="Ali")
        assert account.display_name == "Ahmad Ali"

    def test_with_nothing_at_all_the_address_is_used(self, entity):
        """A blank where a person's name goes is worse than an address."""
        assert self._user(entity).display_name == "ahmad@ajigroup.com"

    def test_a_name_of_spaces_does_not_count_as_a_name(self, entity):
        account = self._user(entity, username="   ", first_name="Ahmad")
        assert account.display_name == "Ahmad"

    def test_the_name_reaches_a_page(self, client, entity):
        """Not asserted through the property but through a rendered page:
        the property being right is no use if a template asks for the email."""
        admin = User.objects.create_user(
            email="boss@ajigroup.com", password=PASSWORD, role=Role.ADMIN,
            entity=entity, username="المدير")
        self._user(entity, username="أحمد العلي")

        client.force_login(admin)
        body = client.get(
            reverse("accounts:settings") + "?tab=users").content.decode()
        assert "أحمد العلي" in body


class TestThePendingPanel:
    """Waiting registrations, folded behind their own count."""

    def test_a_waiting_registration_is_named_and_offers_activation(
            self, client, entity):
        admin = User.objects.create_user(
            email="boss@ajigroup.com", password=PASSWORD, role=Role.ADMIN,
            entity=entity, username="المدير")
        waiting = User.objects.create_user(
            email="ahmad@ajigroup.com", password=PASSWORD, role=Role.USER,
            entity=entity, username="أحمد العلي", is_active=False)

        client.force_login(admin)
        body = client.get(
            reverse("accounts:settings") + "?tab=users").content.decode()

        assert "أحمد العلي" in body
        # Closed by default, and still saying how many are waiting.
        assert "pending__open" in body
        assert reverse("accounts:set_user_active", args=[waiting.pk]) in body

    def test_nothing_waiting_means_no_panel(self, client, entity):
        admin = User.objects.create_user(
            email="boss@ajigroup.com", password=PASSWORD, role=Role.ADMIN,
            entity=entity, username="المدير")
        client.force_login(admin)
        body = client.get(
            reverse("accounts:settings") + "?tab=users").content.decode()
        assert "pending__open" not in body
