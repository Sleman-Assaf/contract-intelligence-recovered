"""The header search's live list, and search without repeats.

As the reader types, the best matches come back as a list -- contracts,
findings, clauses -- from the same scoped search as the results page. Each
analysis run stores its own copy of the clauses and findings, so only the
current run's are searched: a contract analysed six times listed every
matching clause six times.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.analysis.models import AnalysisRun, Clause, Finding
from apps.contracts.search import SearchFilters, search
from apps.contracts.tests.test_search_and_viewer import (  # noqa: F401  (fixtures)
    contract, entity, evidence, make_user, reviewer,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def analysed_twice(contract, evidence):
    """The evidence run, then a re-analysis storing its own copy of everything."""
    document = evidence["document"]
    old = AnalysisRun.objects.get(contract=contract)
    now = timezone.now()
    AnalysisRun.objects.filter(pk=old.pk).update(document=document, started_at=now - timedelta(hours=2),
                                                 finished_at=now - timedelta(hours=1))
    Clause.objects.filter(contract=contract).update(created_at=now - timedelta(minutes=90))
    new = AnalysisRun.objects.create(contract=contract, version=2, status="succeeded", document=document,
                                     started_at=now - timedelta(minutes=10), finished_at=now + timedelta(minutes=1))
    Clause.objects.create(document=document, contract=contract, clause_number="12.3",
                          text="Damages shall not exceed 10% of the contract value.", page_number=14, ordinal=1)
    Finding.objects.create(analysis=new, contract=contract, risk_level="high",
                           title="Uncapped liquidated damages", clause_number="12.3")
    contract.current_analysis = new
    contract.save(update_fields=["current_analysis"])
    return new


class TestNoRepeats:
    def test_a_reanalysed_contract_lists_each_clause_and_finding_once(self, reviewer, analysed_twice):
        results = search(reviewer, SearchFilters(q="Damages"))
        assert results.clause_count == 1
        assert search(reviewer, SearchFilters(q="Uncapped")).finding_count == 1
        assert {f.analysis_id for f in search(reviewer, SearchFilters(q="Uncapped")).findings} == {analysed_twice.pk}


class TestTheLiveList:
    def _get(self, client, q):
        return client.get(reverse("contracts:search_suggest"), {"q": q}).json()

    def test_matches_come_back_grouped_with_links(self, client, reviewer, evidence):
        client.force_login(reviewer)
        data = self._get(client, "damages")
        kinds = {g["kind"]: g for g in data["groups"]}
        assert {"findings", "clauses"} <= set(kinds)
        finding = kinds["findings"]["items"][0]
        assert finding["title"] == "Uncapped liquidated damages" and finding["badge"] == "high"
        assert finding["url"].startswith("/analysis/") and finding["url"].endswith("?tab=findings")
        clause = kinds["clauses"]["items"][0]
        assert "exceed 10%" in clause["snippet"] and clause["title"].endswith("12.3")
        assert data["all"] == reverse("contracts:search") + "?q=damages"
        assert data["total"] == sum(g["count"] for g in data["groups"])

    def test_a_contract_by_its_reference(self, client, reviewer, contract):
        client.force_login(reviewer)
        data = self._get(client, contract.reference[:6])
        contracts = next(g for g in data["groups"] if g["kind"] == "contracts")
        assert contracts["items"][0]["url"] == reverse("contracts:detail", args=[contract.pk])

    def test_one_letter_is_not_searched(self, client, reviewer, evidence):
        client.force_login(reviewer)
        assert self._get(client, "d")["groups"] == []

    def test_it_never_shows_a_contract_the_reader_cannot_open(self, client, entity, evidence):
        outsider = make_user("outsider@aji.test", Role.USER, entity)     # sees only their own uploads
        client.force_login(outsider)
        assert self._get(client, "damages")["groups"] == []

    def test_signing_in_is_required(self, client):
        assert client.get(reverse("contracts:search_suggest"), {"q": "damages"}).status_code == 401

    def test_the_header_is_wired(self, client, reviewer):
        client.force_login(reviewer)
        page = client.get(reverse("contracts:dashboard")).content.decode()
        assert f'data-search-suggest="{reverse("contracts:search_suggest")}"' in page
        assert "data-search-panel" in page and "js/search.js" in page
