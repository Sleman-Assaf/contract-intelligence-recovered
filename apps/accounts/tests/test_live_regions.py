"""Filtering and paging without reloading the page, across the site.

One script does this for every page, and a page opts in by marking one element
`data-live`. That is the whole contract, which makes it easy to lose: delete
the wrapper while reorganising a template and filtering quietly goes back to
full page loads. It still *works*, so nothing fails and nobody notices.

These tests are the thing that notices. They check the markup the script needs
rather than the script itself -- the behaviour was verified in a browser, but
what rots over time is the template.
"""
from __future__ import annotations

import re

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

#: Pages whose lists are filtered or paged, and what has to be inside the
#: swapped region for the result to be coherent.
LIVE_PAGES = {
    "contracts:list": ("filterbar", "count-pill", "<table"),
    "contracts:search": ("<form", "<select"),
    "accounts:settings": ("<table",),
    "analysis:risk_matrix": ("matrix", "meterlist", "bandfilter", "<table"),
    "analysis:comparison": ("<select",),
    "reports:index": ("<select",),
    "workflow:index": ("<select",),
}


@pytest.fixture
def admin(client, db):
    from apps.accounts.models import Entity, Role, User

    entity = Entity.objects.create(name="AJi Live", code="aji-live", country="SA")
    account = User.objects.create_user(
        email="boss@ajigroup.com", password="a-long-enough-passphrase-42",
        role=Role.ADMIN, entity=entity, username="المدير")
    client.force_login(account)
    return account


@pytest.mark.parametrize("name", sorted(LIVE_PAGES))
def test_the_page_offers_exactly_one_region(client, admin, name):
    """One region, because the script swaps the first it finds. Two would mean
    half the page updating and half of it not."""
    body = client.get(reverse(name)).content.decode()
    assert body.count("data-live") == 1, (
        f"{name} has {body.count('data-live')} live regions; the script swaps "
        f"one, so a second would never update"
    )


@pytest.mark.parametrize("name,markers", sorted(LIVE_PAGES.items()))
def test_the_region_holds_what_the_controls_change(client, admin, name, markers):
    """Controls and results together. A filter outside the region would stop
    responding; a result outside it would keep showing the old answer."""
    body = client.get(reverse(name)).content.decode()
    region = body[body.index("data-live"):]
    for marker in markers:
        # Only what actually rendered. Some of these cards appear once there
        # is data to put in them, and an empty database is not a misplaced
        # region -- the question is whether a thing that *is* on the page sits
        # inside the part that updates.
        if marker not in body:
            continue
        assert marker in region, f"{name}: {marker} sits outside the swapped region"


class TestTheControlsStillWorkWithoutIt:
    """Progressive enhancement: the script is an improvement on a page that
    already worked, not the thing that makes it work."""

    def test_the_contracts_filter_is_a_real_get_form(self, client, admin):
        body = client.get(reverse("contracts:list")).content.decode()
        assert 'method="get"' in body
        # A submit button, for a reader whose browser never runs the script.
        assert 'type="submit"' in body

    def test_the_risk_bands_are_real_links(self, client, admin):
        body = client.get(reverse("analysis:risk_matrix")).content.decode()
        assert 'href="?risk=high"' in body

    @pytest.mark.parametrize("name", ["reports:index", "workflow:index",
                                      "analysis:comparison"])
    def test_a_picker_asks_the_form_to_submit_rather_than_submitting_it(
            self, client, admin, name):
        """`form.submit()` fires no submit event, so the script never sees it
        and the page always reloads. `requestSubmit()` fires a real one, and
        falls back to a plain submit where it is missing."""
        body = client.get(reverse(name)).content.decode()
        assert "requestSubmit" in body
        assert re.search(r'onchange="this\.form\.submit\(\)"', body) is None


class TestPagesThatShouldNotHaveOne:
    """A region on a page with nothing to filter is dead weight, and invites
    someone to wire a link into it that should have been a navigation."""

    @pytest.mark.parametrize("name", ["contracts:dashboard", "analysis:advisor"])
    def test_no_region(self, client, admin, name):
        assert "data-live" not in client.get(reverse(name)).content.decode()


def test_the_script_is_loaded_for_every_page(client, admin):
    """Loaded once in the base template rather than per page: a page opts in
    with markup alone, and cannot half-opt-in by forgetting the script."""
    body = client.get(reverse("contracts:dashboard")).content.decode()
    assert "livefilter.js" in body
