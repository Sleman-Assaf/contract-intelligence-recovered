"""Deterministic standards resolution (§15).

The backend selects which approved standard applies. A language model is never
asked what AJi's policy is — it only ever receives the standard the application
has already chosen.

Precedence, most specific first:

    Contract-Type Override  →  Country Override  →  AJi Global Standard

Resolution is also *time-aware*: a contract analysed today must resolve against
the version that was in force, and re-running an old analysis must be able to
resolve the version that applied then (§33).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import ClauseCategory, Standard, StandardScope, StandardStatus, StandardVersion


@dataclass(frozen=True)
class ResolvedStandard:
    """One applicable standard, with the evidence of why it was chosen."""

    category_code: str
    standard: Standard
    version: StandardVersion
    scope: str
    resolution_path: list[str]

    @property
    def is_demo_fixture(self) -> bool:
        """True when this came from test data rather than approved AJi policy.

        Must be surfaced in the UI — a finding derived from a fixture is not a
        finding derived from company policy (§49).
        """
        return self.version.is_demo_fixture


@dataclass(frozen=True)
class ResolutionResult:
    resolved: dict[str, ResolvedStandard]
    unresolved_categories: list[str]
    as_of: date
    reference_template: str

    @property
    def snapshot(self) -> dict[str, dict]:
        """Frozen record of exactly what applied, stored on the AnalysisRun.

        Without this a finding cannot be explained months later.
        """
        return {
            code: {
                "standard_id": r.standard.pk,
                "version_id": str(r.version.pk),
                "version": r.version.version,
                "scope": r.scope,
                "resolution_path": r.resolution_path,
                "is_demo_fixture": r.version.is_demo_fixture,
                "profile": r.standard.reference_template,
            }
            for code, r in self.resolved.items()
        }

    @property
    def uses_demo_fixtures(self) -> bool:
        return any(r.is_demo_fixture for r in self.resolved.values())


def _active_version(standard: Standard, as_of: date) -> StandardVersion | None:
    """The approved version in force on a date.

    Only APPROVED counts. A draft has not been signed off by Legal, so it must
    never silently drive a finding.
    """
    return (
        standard.versions.filter(
            status=StandardStatus.APPROVED,
            effective_date__isnull=False,
            effective_date__lte=as_of,
        )
        .filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=as_of))
        .order_by("-effective_date", "-version")
        .first()
    )


def resolve_for_category(
    category: ClauseCategory,
    *,
    country: str,
    contract_type_id: int | None,
    reference_template: str = "aji_standard",
    as_of: date | None = None,
    fallback: bool = True,
    perspective: str = "",
) -> ResolvedStandard | None:
    """Resolve one category, most specific scope first.

    When the profile has no approved standard in force for the category, the
    default profile (settings.STANDARDS_DEFAULT_PROFILE, the FIDIC baseline)
    is tried the same way, and the resolution path says so: a finding built
    on the baseline is labelled as such, never passed off as AJi policy.
    """
    as_of = as_of or timezone.localdate()

    candidates = (
        Standard.objects.filter(
            category=category,
            reference_template=reference_template,
            is_active=True,
        )
        .filter(
            # Global standards carry no country; country overrides must match.
            Q(scope=StandardScope.GLOBAL, country="", contract_type__isnull=True)
            | Q(scope=StandardScope.COUNTRY, country=country, contract_type__isnull=True)
            | Q(scope=StandardScope.CONTRACT_TYPE, contract_type_id=contract_type_id)
        )
        # A standard for AJi's side, or one for every side.
        .filter(Q(perspective="") | Q(perspective=perspective or ""))
        .select_related("contract_type")
    )

    # Highest specificity that actually has an approved version in force wins.
    # A contract-type override with only a draft must fall back rather than
    # leave the category unresolved.
    attempted: list[str] = []
    # A standard written for AJi's side beats one for every side; within
    # each, the most specific scope wins.
    ordered = sorted(candidates, key=lambda s: (bool(perspective) and s.perspective == perspective,
                                                s.specificity), reverse=True)
    for standard in ordered:
        # A contract-type override still has to belong to the right country
        # (or be country-neutral).
        if standard.scope == StandardScope.CONTRACT_TYPE and standard.country not in ("", country):
            continue

        version = _active_version(standard, as_of)
        label = f"{standard.scope}:{standard.country or '*'}" + (
            f":{standard.perspective}" if standard.perspective else "")
        if version is None:
            attempted.append(f"{label}(no approved version in force)")
            continue

        attempted.append(f"{label}(v{version.version})")
        return ResolvedStandard(
            category_code=category.code,
            standard=standard,
            version=version,
            scope=standard.scope,
            resolution_path=attempted,
        )

    default = getattr(settings, "STANDARDS_DEFAULT_PROFILE", "")
    if fallback and default and default != reference_template:
        base = resolve_for_category(
            category, country=country, contract_type_id=contract_type_id,
            reference_template=default, as_of=as_of, fallback=False, perspective=perspective,
        )
        if base is not None:
            return ResolvedStandard(
                category_code=base.category_code, standard=base.standard,
                version=base.version, scope=base.scope,
                resolution_path=[*attempted, f"fallback:{default}", *base.resolution_path],
            )
    return None


def resolve_for_contract(
    contract,
    *,
    categories=None,
    as_of: date | None = None,
) -> ResolutionResult:
    """Resolve every applicable standard for one contract.

    Called once per analysis run; the result is frozen into the run's snapshot
    so the analysis remains explainable after Legal changes a standard.
    """
    as_of = as_of or contract.effective_date or timezone.localdate()
    template = contract.reference_template or "aji_standard"

    if categories is None:
        categories = ClauseCategory.objects.filter(is_active=True)

    resolved: dict[str, ResolvedStandard] = {}
    unresolved: list[str] = []

    for category in categories:
        match = resolve_for_category(
            category,
            country=contract.country,
            contract_type_id=contract.contract_type_id,
            reference_template=template,
            as_of=as_of,
            perspective=getattr(contract, "analysis_perspective", "") or "",
        )
        if match is None:
            # NO_STANDARD_FOUND is a real outcome, not a reason to invent one.
            unresolved.append(category.code)
        else:
            resolved[category.code] = match

    return ResolutionResult(
        resolved=resolved,
        unresolved_categories=unresolved,
        as_of=as_of,
        reference_template=template,
    )


def expected_categories(result: ResolutionResult) -> set[str]:
    """Categories a compliant contract is expected to address.

    Used for missing-clause detection: the set difference between what the
    standards expect and what the contract actually contains. Deterministic —
    never a question put to a model (§23).
    """
    return set(result.resolved)
