"""Comparing negotiation rounds.

A contract is rarely settled in one pass. AJi comments, the counterparty
returns a revised draft, and the question a reviewer actually has is narrow:
*what moved since last time?* Which of our points did they take, which did they
ignore, and what did they introduce while they were in there.

The comparison is a set difference over clause categories, computed from stored
findings. No model is asked "what changed" — that is a question they answer
fluently and unreliably, and the answer here is already in the database.

One caveat governs the whole module: **a comparison is only as sound as the two
analyses behind it.** If either round was `degraded`, a category can vanish
from the findings simply because it was never examined, which is
indistinguishable from the counterparty fixing it. `RoundComparison.is_reliable`
is False in that case and the UI must say so, because "resolved" is exactly the
word a reviewer will act on.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import AnalysisRun, AnalysisStatus, Finding

#: Risk levels ordered worst-first, for deciding whether a point improved.
_SEVERITY = {"high": 3, "medium": 2, "low": 1, "unknown": 0, "": 0}


@dataclass
class CategoryChange:
    """One clause category, and what happened to it between two rounds."""

    code: str
    label: str
    before: Finding | None
    after: Finding | None

    @property
    def state(self) -> str:
        if self.before is None and self.after is not None:
            return "introduced"
        if self.before is not None and self.after is None:
            return "resolved"
        return "carried"

    @property
    def direction(self) -> str:
        """For a carried point: did it get better, worse, or stay put?"""
        if self.before is None or self.after is None:
            return ""
        before = _SEVERITY.get(self.before.risk_level, 0)
        after = _SEVERITY.get(self.after.risk_level, 0)
        if after < before:
            return "improved"
        if after > before:
            return "worsened"
        return "unchanged"

    @property
    def wording_changed(self) -> bool:
        """Whether the clause text itself moved.

        Compared on normalised whitespace: a PDF re-export rewraps lines
        without anyone having negotiated anything.
        """
        if self.before is None or self.after is None:
            return False
        return (
            " ".join((self.before.original_wording or "").split())
            != " ".join((self.after.original_wording or "").split())
        )


@dataclass
class RoundComparison:
    previous: AnalysisRun
    current: AnalysisRun
    changes: list[CategoryChange] = field(default_factory=list)

    @property
    def resolved(self) -> list[CategoryChange]:
        return [c for c in self.changes if c.state == "resolved"]

    @property
    def introduced(self) -> list[CategoryChange]:
        return [c for c in self.changes if c.state == "introduced"]

    @property
    def carried(self) -> list[CategoryChange]:
        return [c for c in self.changes if c.state == "carried"]

    @property
    def improved(self) -> list[CategoryChange]:
        return [c for c in self.carried if c.direction == "improved"]

    @property
    def worsened(self) -> list[CategoryChange]:
        return [c for c in self.carried if c.direction == "worsened"]

    @property
    def is_reliable(self) -> bool:
        """False when either analysis was incomplete.

        A point missing from a degraded run looks exactly like a point the
        counterparty conceded. Calling that "resolved" would hand a reviewer a
        concession that never happened.
        """
        return AnalysisStatus.DEGRADED not in {
            self.previous.status, self.current.status
        }

    @property
    def unreliable_because(self) -> list[str]:
        reasons = []
        if self.previous.status == AnalysisStatus.DEGRADED:
            reasons.append("previous")
        if self.current.status == AnalysisStatus.DEGRADED:
            reasons.append("current")
        return reasons


def _by_category(run: AnalysisRun) -> dict[str, Finding]:
    """Worst finding per category. A category with several findings is
    represented by the one a reviewer would look at first."""
    out: dict[str, Finding] = {}
    for finding in run.findings.select_related("category").order_by("-risk_score"):
        if finding.category_id is None:
            continue
        out.setdefault(finding.category.code, finding)
    return out


def compare_runs(previous: AnalysisRun, current: AnalysisRun) -> RoundComparison:
    """Diff two analysis runs by clause category."""
    before = _by_category(previous)
    after = _by_category(current)

    changes = []
    for code in sorted(set(before) | set(after)):
        sample = before.get(code) or after.get(code)
        changes.append(CategoryChange(
            code=code,
            label=sample.category.display_name if sample.category else code,
            before=before.get(code),
            after=after.get(code),
        ))

    # Worst first, so the points that still matter lead.
    changes.sort(
        key=lambda c: -(
            (c.after.risk_score if c.after else 0)
            or (c.before.risk_score if c.before else 0)
            or 0
        )
    )
    return RoundComparison(previous=previous, current=current, changes=changes)


def rounds_for(contract) -> list:
    """Every uploaded round, oldest first, with its latest analysis attached."""
    files = contract.source_files.order_by("round_number", "created_at")
    rounds = []
    for source_file in files:
        # SourceFile -> Document is one-to-one; a missing one raises rather
        # than returning None, so it is asked for defensively.
        document = getattr(source_file, "document", None)
        run = (
            AnalysisRun.objects.filter(contract=contract, document=document)
            .order_by("-version").first()
            if document else None
        )
        rounds.append({"source_file": source_file, "document": document, "run": run})
    return rounds


def latest_comparison(contract) -> RoundComparison | None:
    """Compare the two most recent analysed rounds, if there are two."""
    analysed = [r for r in rounds_for(contract) if r["run"] is not None]
    if len(analysed) < 2:
        return None
    return compare_runs(analysed[-2]["run"], analysed[-1]["run"])
