"""Contract search (§39).

Deliberately a straightforward relational search rather than a vector index.
The corpus is small, the dimensions users actually ask about are structured
(client, country, risk, status, reviewer, date), and a full-text engine would
add an operational dependency for no present benefit. Semantic search can be
layered on later — `search_clauses` is where it would go.

Everything is scoped through `perms.scope_contracts` first, so search can never
become a way to discover a contract you may not open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.db.models import Q, QuerySet

from apps.accounts import permissions as perms


@dataclass
class SearchFilters:
    q: str = ""
    country: str = ""
    risk: str = ""
    status: str = ""
    contract_type: str = ""
    reviewer: str = ""
    entity: str = ""
    date_from: date | None = None
    date_to: date | None = None
    scope: str = "all"          # all | contracts | clauses | findings

    @classmethod
    def from_request(cls, params) -> "SearchFilters":
        def parse_date(value: str):
            from django.utils.dateparse import parse_date as _parse
            return _parse(value) if value else None

        return cls(
            q=(params.get("q") or "").strip(),
            country=params.get("country", ""),
            risk=params.get("risk", ""),
            status=params.get("status", ""),
            contract_type=params.get("type", ""),
            reviewer=params.get("reviewer", ""),
            entity=params.get("entity", ""),
            date_from=parse_date(params.get("from", "")),
            date_to=parse_date(params.get("to", "")),
            scope=params.get("scope", "all"),
        )

    @property
    def is_empty(self) -> bool:
        return not any([
            self.q, self.country, self.risk, self.status,
            self.contract_type, self.reviewer, self.entity,
            self.date_from, self.date_to,
        ])


@dataclass
class SearchResults:
    contracts: QuerySet | list = field(default_factory=list)
    clauses: QuerySet | list = field(default_factory=list)
    findings: QuerySet | list = field(default_factory=list)
    contract_count: int = 0
    clause_count: int = 0
    finding_count: int = 0

    @property
    def total(self) -> int:
        return self.contract_count + self.clause_count + self.finding_count


def _apply_structured(qs: QuerySet, f: SearchFilters, *, prefix: str = "") -> QuerySet:
    """Apply the structured filters, optionally across a relation."""
    p = f"{prefix}__" if prefix else ""
    if f.country:
        qs = qs.filter(**{f"{p}country": f.country})
    if f.risk:
        qs = qs.filter(**{f"{p}risk_level": f.risk})
    if f.status:
        qs = qs.filter(**{f"{p}status": f.status})
    if f.contract_type:
        qs = qs.filter(**{f"{p}contract_type__code": f.contract_type})
    if f.reviewer:
        qs = qs.filter(**{f"{p}reviewer_id": f.reviewer})
    if f.entity:
        qs = qs.filter(**{f"{p}entity_id": f.entity})
    if f.date_from:
        qs = qs.filter(**{f"{p}created_at__date__gte": f.date_from})
    if f.date_to:
        qs = qs.filter(**{f"{p}created_at__date__lte": f.date_to})
    return qs


def search_contracts(user, f: SearchFilters, *, limit: int = 50):
    from .models import Contract

    qs = perms.scope_contracts(user, Contract.objects.all())
    qs = _apply_structured(qs, f)

    if f.q:
        qs = qs.filter(
            Q(project_name__icontains=f.q)
            | Q(client_name__icontains=f.q)
            | Q(contract_title__icontains=f.q)
            | Q(reference__icontains=f.q)
            | Q(department__icontains=f.q)
        )
    qs = qs.select_related("contract_type", "entity", "reviewer").order_by("-created_at")
    return qs.count(), qs[:limit]


def _current_run_clauses(qs: QuerySet) -> QuerySet:
    """Only the clauses of each contract's current analysis.

    Every analysis run stores its own copy of the clauses, so without this a
    contract analysed six times listed every matching clause six times.
    """
    from django.db.models import F

    # A bound the run does not have (no current analysis yet, or one still
    # running) is simply not applied, rather than hiding every clause.
    run = "contract__current_analysis__"
    return qs.filter(
        Q(**{f"{run}document_id__isnull": True}) | Q(document_id=F(f"{run}document_id")),
        Q(**{f"{run}started_at__isnull": True}) | Q(created_at__gte=F(f"{run}started_at")),
        Q(**{f"{run}finished_at__isnull": True}) | Q(created_at__lte=F(f"{run}finished_at")),
    )


def search_clauses(user, f: SearchFilters, *, limit: int = 50):
    """Free-text over extracted clause text.

    Matches the *stored source text*, so a hit is always something that really
    appears in the document rather than a model's paraphrase of it.
    """
    from apps.analysis.models import Clause

    from .models import Contract

    if not f.q:
        return 0, Clause.objects.none()

    allowed = perms.scope_contracts(user, Contract.objects.all())
    allowed = _apply_structured(allowed, f)

    qs = (
        _current_run_clauses(Clause.objects.filter(contract__in=allowed))
        .filter(Q(text__icontains=f.q) | Q(heading__icontains=f.q)
                | Q(clause_number__iexact=f.q))
        .select_related("contract", "primary_category")
        .order_by("contract_id", "ordinal")
    )
    return qs.count(), qs[:limit]


def search_findings(user, f: SearchFilters, *, limit: int = 50):
    from apps.analysis.models import Finding

    from .models import Contract

    allowed = perms.scope_contracts(user, Contract.objects.all())
    allowed = _apply_structured(allowed, f)

    # The current analysis's findings: an older run's are superseded, and
    # listing them beside the current ones repeated every finding.
    qs = Finding.objects.current().filter(contract__in=allowed)
    if f.risk:
        qs = qs.filter(risk_level=f.risk)
    if f.q:
        qs = qs.filter(
            Q(title__icontains=f.q)
            | Q(deviation__icontains=f.q)
            | Q(original_wording__icontains=f.q)
            | Q(clause_number__iexact=f.q)
            | Q(category__name__icontains=f.q)
        )
    qs = qs.select_related("contract", "category").order_by("-risk_score")
    return qs.count(), qs[:limit]


def search(user, f: SearchFilters, *, limit: int = 50) -> SearchResults:
    results = SearchResults()
    if f.is_empty:
        return results

    if f.scope in ("all", "contracts"):
        results.contract_count, results.contracts = search_contracts(user, f, limit=limit)
    if f.scope in ("all", "clauses"):
        results.clause_count, results.clauses = search_clauses(user, f, limit=limit)
    if f.scope in ("all", "findings"):
        results.finding_count, results.findings = search_findings(user, f, limit=limit)
    return results
