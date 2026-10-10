"""Reading the list of people.

Three things this tab has to get right once a deployment has more than a
handful of accounts: finding one person, not printing all of them at once,
and not letting either of those hide a registration that is waiting on
somebody.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Entity, Role, User

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-enough-passphrase-42"
USERS = reverse("accounts:settings") + "?tab=users"


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi Dir", code="aji-dir", country="SA")


@pytest.fixture
def admin(client, entity):
    account = User.objects.create_user(
        email="boss@ajigroup.com", password=PASSWORD, role=Role.ADMIN,
        entity=entity, username="المدير")
    client.force_login(account)
    return account


def make(entity, username, email, **fields):
    return User.objects.create_user(
        email=email, password=PASSWORD, role=Role.CONTRACT_REVIEWER,
        entity=entity, username=username, **fields)


def search(client, term):
    response = client.get(USERS + "&q=" + term)
    return list(response.context["users"])


class TestFindingSomebody:
    def test_by_the_name_they_chose(self, client, admin, entity):
        wanted = make(entity, "أحمد العلي", "ahmad@ajigroup.com")
        make(entity, "سارة خالد", "sara@ajigroup.com")

        found = search(client, "أحمد")
        assert [u.pk for u in found] == [wanted.pk]

    def test_by_part_of_the_name(self, client, admin, entity):
        wanted = make(entity, "Ahmad Al-Ali", "ahmad@ajigroup.com")
        make(entity, "Sara Khaled", "sara@ajigroup.com")
        assert [u.pk for u in search(client, "al-al")] == [wanted.pk]

    def test_by_the_address(self, client, admin, entity):
        wanted = make(entity, "أحمد العلي", "ahmad.ali@ajigroup.com")
        make(entity, "سارة خالد", "sara@ajigroup.com")
        assert [u.pk for u in search(client, "ahmad.ali")] == [wanted.pk]

    def test_the_case_does_not_matter(self, client, admin, entity):
        wanted = make(entity, "Ahmad", "AHMAD@ajigroup.com".lower())
        assert wanted in search(client, "AHMAD")

    def test_an_account_with_no_chosen_name_is_still_findable(
            self, client, admin, entity):
        """Accounts predate the field. Searching for what the directory shows
        has to work whether that is a chosen name or an older one."""
        wanted = make(entity, "", "legacy@ajigroup.com",
                      first_name="Legacy", last_name="Person")
        assert wanted in search(client, "Legacy")

    def test_a_term_matching_nobody_returns_nobody(self, client, admin, entity):
        make(entity, "أحمد العلي", "ahmad@ajigroup.com")
        assert search(client, "nobodyhere") == []


class TestNotPrintingEverybody:
    def _many(self, entity, count):
        User.objects.bulk_create([
            User(email="person%02d@ajigroup.com" % i, username="Person %02d" % i,
                 role=Role.CONTRACT_REVIEWER, entity=entity, is_active=True)
            for i in range(count)
        ])

    def test_fifty_fit_on_one_page(self, client, admin, entity):
        self._many(entity, 49)          # plus the administrator = 50
        response = client.get(USERS)
        assert response.context["paginator"].count == 50
        assert response.context["is_paginated"] is False

    def test_past_fifty_it_pages(self, client, admin, entity):
        self._many(entity, 60)
        response = client.get(USERS)
        assert response.context["is_paginated"] is True
        assert len(response.context["users"]) == 50
        assert response.context["paginator"].num_pages == 2

    def test_the_second_page_holds_the_rest(self, client, admin, entity):
        self._many(entity, 60)
        response = client.get(USERS + "&page=2")
        assert len(response.context["users"]) == 11

    def test_a_search_carries_across_pages(self, client, admin, entity):
        """A bare page number would drop the search and page through the whole
        directory instead of through the results."""
        self._many(entity, 60)
        body = client.get(USERS + "&q=Person").content.decode()
        assert "q=Person" in body

    def test_a_search_narrows_before_it_pages(self, client, admin, entity):
        self._many(entity, 60)
        response = client.get(USERS + "&q=Person 0")
        assert response.context["paginator"].count == 10
        assert response.context["is_paginated"] is False


class TestTheWaitingRegistrations:
    def test_they_sit_below_the_directory(self, client, admin, entity):
        make(entity, "أحمد العلي", "ahmad@ajigroup.com", is_active=False)
        body = client.get(USERS).content.decode()

        table = body.index("<table")
        panel = body.index("pending__open")
        assert panel > table, (
            "the waiting registrations should come after the directory table"
        )

    def test_a_search_cannot_hide_them(self, client, admin, entity):
        """Unfiltered on purpose: someone who cannot sign in should not depend
        on the administrator having typed the right thing in the search box."""
        make(entity, "أحمد العلي", "ahmad@ajigroup.com", is_active=False)
        response = client.get(USERS + "&q=nobodyhere")

        assert list(response.context["users"]) == []
        assert len(response.context["pending_users"]) == 1

    def test_paging_cannot_bury_them(self, client, admin, entity):
        """Unpaged too: page three of an alphabetical list is nowhere."""
        User.objects.bulk_create([
            User(email="person%02d@ajigroup.com" % i, username="Person %02d" % i,
                 role=Role.CONTRACT_REVIEWER, entity=entity, is_active=True)
            for i in range(60)
        ])
        make(entity, "زياد الأخير", "zzz.last@ajigroup.com", is_active=False)

        response = client.get(USERS)
        assert response.context["is_paginated"] is True
        assert "زياد الأخير" in response.content.decode()
