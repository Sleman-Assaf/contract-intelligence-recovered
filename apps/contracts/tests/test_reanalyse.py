"""Queueing a fresh analysis from the contract page.

The button was shown to every role and the endpoint refused most of them; and
the message it flashed on success was the one English sentence left in the
Arabic UI. Neither had a test, which is how both lasted.
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from apps.accounts import permissions as perms
from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractType

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi - Jordan", code="aji-jo", country=Country.JORDAN)


@pytest.fixture
def contract(entity):
    kind = ContractType.objects.create(code="arch", name="Architectural Design")
    uploader = User.objects.create_user(
        email="uploader@aji.test", password="test-password-1234",
        role=Role.CONTRACT_REVIEWER, entity=entity)
    return Contract.objects.create(
        project_name="Amman Gateway", contract_title="Design Consultancy",
        client_name="GAM", country=Country.JORDAN, entity=entity,
        contract_type=kind, uploaded_by=uploader,
    )


def signed_in(client, role, entity, lang="ar"):
    user = User.objects.create_user(
        email=f"{role}@aji.test", password="test-password-1234", role=role,
        entity=entity, preferred_language=lang)
    client.force_login(user)
    return user


@pytest.mark.parametrize("role", [r.value for r in Role])
def test_the_button_follows_the_capability(client, entity, contract, role):
    user = signed_in(client, role, entity)
    response = client.get(reverse("contracts:detail", args=[contract.pk]))
    if response.status_code != 200:
        pytest.skip(f"{role} cannot open this contract at all")
    allowed = perms.has_capability(user, perms.RUN_ANALYSIS)
    assert ("/reanalyse/" in response.content.decode()) is allowed


@pytest.fixture
def analysed(contract):
    """A contract with an analysis: its own page redirects to the analysis."""
    from apps.analysis.models import AnalysisRun

    run = AnalysisRun.objects.create(contract=contract, version=1, status="succeeded")
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    return run


class TestTheAnalysisPageCarriesTheRecord:
    """Once a contract has an analysis, its own page redirects there -- so
    the analysis page has to offer what the contract page did: the source
    file, the versions, the rounds and a fresh analysis."""

    def test_the_contract_page_does_redirect(self, client, entity, contract, analysed):
        signed_in(client, Role.CONTRACT_REVIEWER, entity)
        response = client.get(reverse("contracts:detail", args=[contract.pk]))
        assert response.status_code == 302
        assert response["Location"] == reverse("analysis:detail", args=[analysed.pk])

    def test_rounds_and_versions_are_reachable(self, client, entity, contract, analysed):
        signed_in(client, Role.CONTRACT_REVIEWER, entity)
        body = client.get(reverse("analysis:detail", args=[analysed.pk]) + "?tab=provenance").content.decode()
        assert reverse("contracts:rounds", args=[contract.pk]) in body
        assert reverse("analysis:detail", args=[analysed.pk]) in body   # the versions table

    @pytest.mark.parametrize("role", [r.value for r in Role])
    def test_reanalyse_follows_the_capability_there_too(self, client, entity, contract, analysed, role):
        user = signed_in(client, role, entity)
        response = client.get(reverse("analysis:detail", args=[analysed.pk]) + "?tab=provenance")
        if response.status_code != 200:
            pytest.skip(f"{role} cannot open this contract at all")
        allowed = perms.has_capability(user, perms.RUN_ANALYSIS)
        assert ("/reanalyse/" in response.content.decode()) is allowed


def test_a_role_without_the_capability_is_refused(client, entity, contract):
    signed_in(client, Role.LEGAL_REVIEWER, entity)
    with mock.patch("apps.analysis.tasks.analyse_contract.apply_async") as queued:
        response = client.post(reverse("contracts:reanalyse", args=[contract.pk]))
    assert response.status_code == 403
    queued.assert_not_called()


def test_get_does_nothing_but_go_back(client, entity, contract):
    signed_in(client, Role.CONTRACT_REVIEWER, entity)
    response = client.get(reverse("contracts:reanalyse", args=[contract.pk]))
    assert response.status_code == 302
    assert response["Location"] == reverse("contracts:detail", args=[contract.pk])


@pytest.mark.parametrize("lang,expected", [
    ("ar", "أُدرجت إعادة التحليل في قائمة الانتظار"),
    ("en", "Re-analysis queued"),
])
def test_queues_and_says_so_in_the_readers_language(client, entity, contract, lang, expected):
    signed_in(client, Role.CONTRACT_REVIEWER, entity, lang)
    with mock.patch("apps.analysis.tasks.analyse_contract.apply_async") as queued:
        response = client.post(reverse("contracts:reanalyse", args=[contract.pk]))
    assert response.status_code == 302
    queued.assert_called_once()
    assert str(contract.pk) in queued.call_args.kwargs["args"]
    flashed = [m.message for m in get_messages(response.wsgi_request)]
    assert any(expected in m for m in flashed), flashed
