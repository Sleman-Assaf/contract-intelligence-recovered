"""The risk matrix: the whole portfolio by default, narrowed by the filter bar.

Only each contract's current analysis counts -- a contract analysed six
times had its findings counted six times, turning 227 findings into 602. The
filters (contracts, AJi company, country, type, status, upload dates) narrow
every number on the page together, live in the URL, and survive the band
links and the pager.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Entity, Role, User
from apps.analysis.models import AnalysisRun, Finding
from apps.contracts.models import Contract, ContractStatus, ContractType, Country

pytestmark = pytest.mark.django_db


def _contract(name, *, entity, ctype, uploader, country=Country.SAUDI_ARABIA,
              status=ContractStatus.PENDING_CONTRACT_REVIEW, risks=("high",), runs=1, age_days=0):
    """A contract whose `runs` analyses each stored the same findings; the last is current."""
    contract = Contract.objects.create(
        project_name=name, contract_title="T", client_name="C", reference=name[:10],
        country=country, entity=entity, contract_type=ctype, uploaded_by=uploader, status=status)
    Contract.objects.filter(pk=contract.pk).update(created_at=timezone.now() - timedelta(days=age_days))
    for version in range(1, runs + 1):
        run = AnalysisRun.objects.create(contract=contract, version=version, status="succeeded")
        for level in risks:
            Finding.objects.create(analysis=run, contract=contract, risk_level=level, title=f"{name} {level}",
                                   impact=4 if level == "high" else 2, probability=4 if level == "high" else 2,
                                   risk_score=16 if level == "high" else 4)
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    contract.refresh_from_db()
    return contract


@pytest.fixture
def portfolio(entity, contract_type, reviewer):
    jordan = Entity.objects.create(name="AJi - Jordan", code="aji-jo", country=Country.JORDAN)
    other_type = ContractType.objects.create(code="supervision", name="Supervision")
    return {
        # analysed six times: must count once
        "riyadh": _contract("Riyadh Tower", entity=entity, ctype=contract_type, uploader=reviewer,
                            risks=("high", "medium", "medium"), runs=6),
        "amman": _contract("Amman Water", entity=jordan, ctype=other_type, uploader=reviewer,
                           country=Country.JORDAN, risks=("medium", "low"), age_days=40),
        "jeddah": _contract("Jeddah Fit-Out", entity=entity, ctype=contract_type, uploader=reviewer,
                            status=ContractStatus.APPROVED, risks=("low",), age_days=10),
        "jordan": jordan, "other_type": other_type,
    }


def _matrix(client, **params):
    response = client.get(reverse("analysis:risk_matrix"), params)
    assert response.status_code == 200
    return response.context


class TestOnlyTheCurrentAnalysisCounts:
    def test_a_reanalysed_contract_is_counted_once(self, signed_in, portfolio):
        ctx = _matrix(signed_in)
        # 3 (Riyadh, once, not 18) + 2 (Amman) + 1 (Jeddah)
        assert ctx["totals"] == {"all": 6, "high": 1, "medium": 3, "low": 2}
        cells = sum(c["count"] for row in ctx["rows"] for c in row["cells"])
        assert cells == 6 and ctx["paginator"].count == 6
        assert ctx["contract_count"] == 3

    def test_the_dashboard_alerts_use_the_current_analysis_too(self, signed_in, portfolio):
        riyadh = portfolio["riyadh"]
        Finding.objects.filter(contract=riyadh).update(verification_status="needs_human_review")
        conflicts = signed_in.get(reverse("contracts:dashboard")).context["conflicts"]
        assert {f.analysis_id for f in conflicts} == {riyadh.current_analysis_id}


class TestTheFilters:
    def test_one_contract(self, signed_in, portfolio):
        ctx = _matrix(signed_in, contract=portfolio["amman"].pk)
        assert ctx["totals"]["all"] == 2 and ctx["contract_count"] == 1 and ctx["filtered"]

    def test_several_contracts(self, signed_in, portfolio):
        url = reverse("analysis:risk_matrix") + f"?contract={portfolio['amman'].pk}&contract={portfolio['jeddah'].pk}"
        ctx = signed_in.get(url).context
        assert ctx["totals"]["all"] == 3 and ctx["contract_count"] == 2

    @pytest.mark.parametrize("params, expected", [
        (lambda p: {"entity": p["jordan"].pk}, 2),
        (lambda p: {"country": Country.JORDAN}, 2),
        (lambda p: {"type": p["other_type"].code}, 2),
        (lambda p: {"status": ContractStatus.APPROVED}, 1),
        (lambda p: {"from": (timezone.localdate() - timedelta(days=20)).isoformat()}, 4),
        (lambda p: {"to": (timezone.localdate() - timedelta(days=20)).isoformat()}, 2),
    ])
    def test_each_filter(self, signed_in, portfolio, params, expected):
        assert _matrix(signed_in, **params(portfolio))["totals"]["all"] == expected

    def test_filters_combine_with_the_band(self, signed_in, portfolio):
        ctx = _matrix(signed_in, country=Country.SAUDI_ARABIA, risk="medium")
        # the band narrows the log; the band buttons still count the filtered portfolio
        assert ctx["paginator"].count == 2 and ctx["totals"]["all"] == 4

    def test_nonsense_and_hand_edited_values_narrow_nothing_they_should_not(self, signed_in, portfolio):
        assert _matrix(signed_in, contract="not-a-uuid", country="XX", status="nope", entity="99999",
                       **{"from": "yesterday"})["totals"]["all"] == 6

    def test_a_contract_the_reader_cannot_see_cannot_be_chosen(self, client, portfolio, entity):
        user = User.objects.create_user(email="u@aji.test", password="x-long-passphrase-42",
                                        role=Role.USER, entity=entity)          # sees only their own
        client.force_login(user)
        ctx = _matrix(client, contract=portfolio["riyadh"].pk)
        assert ctx["totals"]["all"] == 0 and ctx["filters"]["contract"] == []


class TestTheLinksKeepTheFilters:
    def test_band_links_and_pager_carry_them(self, signed_in, portfolio):
        pk = portfolio["amman"].pk
        ctx = _matrix(signed_in, contract=pk, country=Country.JORDAN, risk="low")
        assert ctx["band_links"]["high"] == f"?contract={pk}&country=JO&risk=high"
        assert ctx["band_links"]["all"] == f"?contract={pk}&country=JO"
        assert ctx["page_prefix"] == f"?contract={pk}&country=JO&risk=low&"

    def test_the_page_shows_the_bar_and_says_what_is_counted(self, signed_in, portfolio):
        body = signed_in.get(reverse("analysis:risk_matrix"), {"country": "JO"}).content.decode()
        assert "data-matrix-filter" in body and "data-cpick" in body and "js/matrixfilter.js" in body
        assert 'name="entity"' in body and 'name="from"' in body and 'type="date"' in body
        assert "يُحتسب آخر تحليل لكل عقد فقط" in body
