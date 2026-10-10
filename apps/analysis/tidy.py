"""Fewer, truer findings: one point once, and no risk without evidence.

On six real contracts a run produced 50-80 findings, most "medium", and a
reviewer could not see the ten that matter:

* The model often could not judge a clause at all -- "clause text not found",
  "the clause's subject does not match the standard" (it was filed under the
  wrong topic) -- and no rule applied either. With nothing found, that is not
  a risk: its level becomes UNKNOWN ("not determined"). It stays listed, so
  nothing is lost, but it no longer counts as a medium risk.
* The same point repeated clause after clause: "the Client's duty to provide
  data is absent" in 20.1, 20.2, 20.2.2, 20.2.3, 21.3, 21.4, 21.5. Findings of
  one topic, against one standard, saying the same thing are merged into the
  most serious of them, which lists the other clauses (`also_in`).
"""
from __future__ import annotations

import difflib
import re

from apps.contracts.models import RiskLevel

from .models import ComplianceResult

_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟ]")
_NON_WORD = re.compile(r"[^\w\s]+")


def _norm(text: str) -> str:
    text = _TASHKEEL.sub("", (text or "").lower())
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"}))
    return re.sub(r"\s+", " ", _NON_WORD.sub(" ", text)).strip()


def _same_point(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = set(na.split()), set(nb.split())
    jaccard = len(wa & wb) / max(1, len(wa | wb))
    return jaccard >= 0.6 or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


def _point(finding) -> str:
    return finding.deviation or finding.title or ""


def tidy_findings(run) -> dict:
    """Apply both rules to a run's findings; returns what changed."""
    findings = list(run.findings.select_related("category").order_by("-risk_score", "clause_number"))
    undetermined = 0
    for finding in findings:
        if (finding.ai_result == ComplianceResult.UNKNOWN
                and finding.system_result == ComplianceResult.NOT_APPLICABLE
                and finding.ai_confidence is not None          # the model answered, without a verdict
                and finding.risk_level != RiskLevel.UNKNOWN):
            finding.risk_level = RiskLevel.UNKNOWN
            finding.risk_score = 0
            finding.save(update_fields=["risk_level", "risk_score", "updated_at"])
            undetermined += 1

    restored = _restore_uncovered_topics(run, findings)

    merged = 0
    kept: list = []
    for finding in findings:                                 # most serious first
        twin = next((k for k in kept
                     if k.category_id == finding.category_id
                     and k.standard_version_id == finding.standard_version_id
                     and (k.risk_level == RiskLevel.UNKNOWN) == (finding.risk_level == RiskLevel.UNKNOWN)
                     and _same_point(_point(k), _point(finding))), None)
        if twin is None:
            kept.append(finding)
            continue
        detail = dict(twin.system_rule_detail or {})
        also = [n for n in detail.get("also_in", []) if n]
        if finding.clause_number and finding.clause_number not in also and finding.clause_number != twin.clause_number:
            also.append(finding.clause_number)
        for number in (finding.system_rule_detail or {}).get("also_in", []):
            if number and number not in also and number != twin.clause_number:
                also.append(number)
        detail["also_in"] = also
        twin.system_rule_detail = detail
        twin.save(update_fields=["system_rule_detail", "updated_at"])
        finding.delete()
        merged += 1
    return {"undetermined": undetermined, "merged": merged, "left": len(kept), "restored": restored}


def _restore_uncovered_topics(run, findings) -> int:
    """A topic no clause actually covers is a real finding.

    Each "this clause does not cover X" is noise on its own -- the clause is
    about something else. But when *every* clause filed under a topic says so,
    nothing in the contract covers it: one association agreement's only
    "termination" clause was about returning equipment, and "there is no
    termination clause" -- the manual review's point -- was demoted with the
    rest. The most confident of them gets its risk level back.
    """
    from collections import Counter

    from . import scoring
    from .models import Clause

    clauses = Counter(Clause.objects.filter(contract=run.contract, document=run.document)
                      .exclude(primary_category=None).values_list("primary_category_id", flat=True))
    by_topic: dict = {}
    for finding in findings:
        by_topic.setdefault(finding.category_id, []).append(finding)
    restored = 0
    for topic, items in by_topic.items():
        covered = {f.clause_id for f in items}
        if (not items or any(f.risk_level != RiskLevel.UNKNOWN for f in items)
                or not clauses.get(topic) or len(covered) < clauses[topic]):
            continue
        best = max(items, key=lambda f: (f.ai_confidence or 0, -len(f.also_in)))
        risk = scoring.score_finding(
            standard_impact=best.standard_version.impact if best.standard_version_id else None,
            system_result=best.system_result, ai_result=best.ai_result,
            verification=best.verification_status, ai_confidence=best.ai_confidence)
        if risk.level == RiskLevel.UNKNOWN:
            continue
        best.risk_level, best.risk_score = risk.level, risk.score
        detail = dict(best.system_rule_detail or {})
        detail["covers_none"] = True
        best.system_rule_detail = detail
        best.save(update_fields=["risk_level", "risk_score", "system_rule_detail", "updated_at"])
        restored += 1
    return restored


def recount(run) -> None:
    """The run's counts and overall level after tidying (as _finalise does)."""
    findings = list(run.findings.all())
    run.high_count = sum(1 for f in findings if f.risk_level == RiskLevel.HIGH)
    run.medium_count = sum(1 for f in findings if f.risk_level == RiskLevel.MEDIUM)
    run.low_count = sum(1 for f in findings if f.risk_level == RiskLevel.LOW)
    run.unresolved_count = sum(1 for f in findings if f.needs_human)
    if run.overall_risk != RiskLevel.UNKNOWN:
        run.overall_risk = (RiskLevel.HIGH if run.high_count else RiskLevel.MEDIUM if run.medium_count
                            else RiskLevel.LOW if run.low_count else run.overall_risk)
    run.save(update_fields=["high_count", "medium_count", "low_count", "unresolved_count", "overall_risk",
                            "updated_at"])
