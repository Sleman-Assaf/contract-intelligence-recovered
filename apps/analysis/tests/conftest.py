"""Fixtures for the analysis screens.

Deliberately self-contained rather than imported from another app's test
module: a test that reaches into a sibling app's fixtures breaks the moment
that app reorganises its own tests.
"""
from __future__ import annotations

import pytest

from apps.accounts.models import Entity, Role, User
from apps.analysis.models import AnalysisRun, Finding
from apps.contracts.models import Contract, ContractStatus, ContractType, Country
from apps.standards.models import ClauseCategory


@pytest.fixture
def entity():
    return Entity.objects.create(
        name="AJi - Saudi Arabia", code="aji-sa", country=Country.SAUDI_ARABIA
    )


@pytest.fixture
def contract_type():
    return ContractType.objects.create(code="design", name="Design Consultancy")


@pytest.fixture
def reviewer(entity):
    return User.objects.create_user(
        email="analysis-reviewer@aji.test",
        password="test-password-1234",
        role=Role.CONTRACT_REVIEWER,
        entity=entity,
    )


@pytest.fixture
def contract(entity, contract_type, reviewer):
    return Contract.objects.create(
        project_name="Riyadh Business District",
        contract_title="Design Consultancy Agreement",
        client_name="Ministry of Culture",
        reference="4500002530",
        country=Country.SAUDI_ARABIA,
        entity=entity,
        contract_type=contract_type,
        uploaded_by=reviewer,
        status=ContractStatus.PENDING_CONTRACT_REVIEW,
    )


@pytest.fixture
def run_with_finding(contract):
    run = AnalysisRun.objects.create(
        contract=contract, version=1, status="succeeded",
        overall_risk="high", high_count=1,
        executive_summary="Uncapped delay damages.",
    )
    category = ClauseCategory.objects.create(code="delay", name="Delay damages")
    Finding.objects.create(
        analysis=run, contract=contract, category=category,
        risk_level="high", impact=5, probability=4, risk_score=20,
        title="Uncapped liquidated damages",
        clause_number="12.3", page_number=14,
        original_wording="Damages shall not exceed ...",
        system_result="non_compliant", ai_result="non_compliant",
        verification_status="verified",
    )
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    return run


@pytest.fixture
def signed_in(client, reviewer):
    client.force_login(reviewer)
    return client


@pytest.fixture
def chat_file(run_with_finding):
    """An analysed contract the chat can answer from: its run has a RAG
    index (apps.analysis.rag) with one passage, clause 12.3."""
    from apps.analysis import rag
    from apps.analysis.models import RagIndex, RagPassage

    run = run_with_finding
    text = "12.3 Liquidated damages for delay shall not exceed 10% of the fee."
    counted = {}
    for term in rag.terms(text):
        counted[term] = counted.get(term, 0) + 1
    index = RagIndex.objects.create(analysis=run, contract=run.contract, passages_count=1,
                                    avg_terms=sum(counted.values()), doc_freq={t: 1 for t in counted})
    RagPassage.objects.create(index=index, ordinal=0, clause_number="12.3", page_number=14,
                              text=text, terms=counted, length=sum(counted.values()))
    return run.contract
