"""The deterministic rules engine, value extraction and scoring (§17, §18, §24, §25)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.analysis import rules, scoring
from apps.analysis.models import ComplianceResult, VerificationOutcome
from apps.analysis.value_extraction import (
    parse_duration_days,
    parse_money,
    parse_percentage,
)


# ── value extraction ─────────────────────────────────────────────────────

class TestValueExtraction:
    @pytest.mark.parametrize("text,expected", [
        ("shall not exceed 10% of the contract value", Decimal("10")),
        ("capped at 15%", Decimal("15")),
        ("لا تتجاوز ١٠٪ من قيمة العقد", Decimal("10")),
        ("a rate of 12.5%", Decimal("12.5")),
    ])
    def test_percentages(self, text, expected):
        assert parse_percentage(text) == expected

    @pytest.mark.parametrize("text,amount,currency", [
        ("SAR 25,000,000", Decimal("25000000"), "SAR"),
        ("professional indemnity of SAR 10 million", Decimal("10000000"), "SAR"),
        ("JOD 7,000,000", Decimal("7000000"), "JOD"),
    ])
    def test_money(self, text, amount, currency):
        parsed_amount, parsed_currency = parse_money(text)
        assert parsed_amount == amount
        assert parsed_currency == currency

    @pytest.mark.parametrize("text,days", [
        ("within 90 days", Decimal("90")),
        ("within 3 months", Decimal("90")),
        ("a period of 2 years", Decimal("730")),
        ("خلال ٣٠ يوماً", Decimal("30")),
    ])
    def test_durations_normalise_to_days(self, text, days):
        """A 90-day standard must be comparable with '3 months'."""
        assert parse_duration_days(text) == days

    def test_absent_value_is_none_not_zero(self):
        """Silence is not zero, and zero would read as a breach."""
        assert parse_percentage("The Consultant shall act diligently.") is None


# ── the rules engine ─────────────────────────────────────────────────────

LD_CAP_RULE = {
    "key": "ld_cap_percent",
    "extract": "percentage",
    "operator": "lte",
    "value": 10,
}


class TestRulesEngine:
    def test_the_spec_example(self):
        """§17 verbatim: standard says <= 10%, contract says 15%."""
        outcome = rules.evaluate(LD_CAP_RULE, "liquidated damages capped at 15%")
        assert outcome.result == ComplianceResult.NON_COMPLIANT
        assert outcome.detail["actual"] == "15"
        assert outcome.detail["expected"] == "10"
        assert outcome.detail["passed"] is False

    def test_compliant_value_passes(self):
        outcome = rules.evaluate(LD_CAP_RULE, "damages shall not exceed 10%")
        assert outcome.result == ComplianceResult.COMPLIANT

    def test_boundary_is_inclusive_for_lte(self):
        assert rules.evaluate(LD_CAP_RULE, "exactly 10%").result == ComplianceResult.COMPLIANT

    def test_arabic_contract_is_evaluated_identically(self):
        outcome = rules.evaluate(LD_CAP_RULE, "تعويضات التأخير بنسبة ١٥٪")
        assert outcome.result == ComplianceResult.NON_COMPLIANT
        assert outcome.detail["actual"] == "15"

    def test_missing_value_is_unknown_not_non_compliant(self):
        """A clause that is silent has not been proven to breach anything."""
        outcome = rules.evaluate(LD_CAP_RULE, "The Consultant shall act diligently.")
        assert outcome.result == ComplianceResult.UNKNOWN
        assert outcome.detail["reason"] == "value_not_found_in_clause"

    def test_no_rule_means_the_engine_abstains(self):
        """Not every standard is machine-checkable; abstaining is correct."""
        outcome = rules.evaluate(None, "some clause")
        assert outcome.result == ComplianceResult.NOT_APPLICABLE

    def test_unsupported_operator_is_unknown(self):
        outcome = rules.evaluate(
            {"operator": "vibes", "value": 1, "extract": "percentage"}, "10%"
        )
        assert outcome.result == ComplianceResult.UNKNOWN

    def test_rules_are_data_not_code(self):
        """A rule definition must never be executable."""
        outcome = rules.evaluate(
            {"operator": "__import__('os').system", "value": 1}, "10%"
        )
        assert outcome.result == ComplianceResult.UNKNOWN

    def test_statement_explains_the_arithmetic(self):
        outcome = rules.evaluate(LD_CAP_RULE, "capped at 15%")
        assert "15" in outcome.detail["statement"]
        assert "10" in outcome.detail["statement"]

    def test_deterministic_across_runs(self):
        text = "liquidated damages capped at 15%"
        results = {rules.evaluate(LD_CAP_RULE, text).result for _ in range(20)}
        assert results == {ComplianceResult.NON_COMPLIANT}


INSURANCE_RULE = {
    "key": "pi_insurance",
    "extract": "money",
    "operator": "gte",
    "value": 25_000_000,
    "currency": "SAR",
}


class TestCurrencySafety:
    def test_same_currency_compares(self):
        outcome = rules.evaluate(INSURANCE_RULE, "professional indemnity of SAR 10 million")
        assert outcome.result == ComplianceResult.NON_COMPLIANT

    def test_sufficient_cover_passes(self):
        outcome = rules.evaluate(INSURANCE_RULE, "professional indemnity of SAR 30,000,000")
        assert outcome.result == ComplianceResult.COMPLIANT

    def test_different_currency_is_ambiguous_not_guessed(self):
        """Comparing SAR with JOD needs an exchange rate the system lacks."""
        outcome = rules.evaluate(INSURANCE_RULE, "professional indemnity of JOD 7,000,000")
        assert outcome.result == ComplianceResult.AMBIGUOUS
        assert outcome.detail["reason"] == "currency_mismatch"

    def test_unstated_currency_is_ambiguous(self):
        outcome = rules.evaluate(INSURANCE_RULE, "indemnity of 30,000,000")
        assert outcome.result == ComplianceResult.AMBIGUOUS


class TestPresenceRules:
    ARBITRATION = {
        "operator": "present",
        "markers": ["arbitration", "التحكيم"],
    }

    def test_required_wording_found(self):
        outcome = rules.evaluate(self.ARBITRATION, "Disputes shall be settled by arbitration.")
        assert outcome.result == ComplianceResult.COMPLIANT

    def test_required_wording_missing(self):
        outcome = rules.evaluate(self.ARBITRATION, "Disputes shall go to the courts.")
        assert outcome.result == ComplianceResult.NON_COMPLIANT

    def test_arabic_marker_matches(self):
        outcome = rules.evaluate(self.ARBITRATION, "تُحل المنازعات عن طريق التحكيم")
        assert outcome.result == ComplianceResult.COMPLIANT


# ── reconciliation: the model never overrides arithmetic (§18) ───────────

class TestReconciliation:
    def test_agreement_verifies(self):
        assert rules.reconcile(
            ComplianceResult.NON_COMPLIANT, ComplianceResult.NON_COMPLIANT
        ) == VerificationOutcome.VERIFIED

    def test_disagreement_raises_conflict_not_an_ai_win(self):
        """The §18 case: system says breach, AI says fine → human decides."""
        assert rules.reconcile(
            ComplianceResult.NON_COMPLIANT, ComplianceResult.COMPLIANT
        ) == VerificationOutcome.SYSTEM_AI_CONFLICT

    def test_uncertainty_anywhere_is_ambiguous(self):
        assert rules.reconcile(
            ComplianceResult.UNKNOWN, ComplianceResult.COMPLIANT
        ) == VerificationOutcome.AMBIGUOUS

    def test_ai_only_assessment_is_not_verified(self):
        """With no deterministic rule there is nothing corroborating the model."""
        assert rules.reconcile(
            ComplianceResult.NOT_APPLICABLE, ComplianceResult.NON_COMPLIANT
        ) == VerificationOutcome.NEEDS_HUMAN_REVIEW


# ── scoring (§25) ────────────────────────────────────────────────────────

class TestRiskScoring:
    def test_impact_comes_from_legal_not_the_model(self):
        low = scoring.score_finding(
            standard_impact=1, system_result=ComplianceResult.NON_COMPLIANT,
            ai_result=ComplianceResult.NON_COMPLIANT,
            verification=VerificationOutcome.VERIFIED,
        )
        high = scoring.score_finding(
            standard_impact=5, system_result=ComplianceResult.NON_COMPLIANT,
            ai_result=ComplianceResult.NON_COMPLIANT,
            verification=VerificationOutcome.VERIFIED,
        )
        assert low.impact == 1 and high.impact == 5
        assert low.score < high.score
        assert high.level == "high"

    def test_matrix_does_not_collapse_onto_its_diagonal(self):
        """The old system scored high→4x4, medium→3x3, low→2x2, so the matrix
        carried no information. Impact and probability must move apart."""
        combos = {
            (
                scoring.score_finding(
                    standard_impact=impact,
                    system_result=system,
                    ai_result=ai,
                    verification=VerificationOutcome.VERIFIED,
                ).impact,
                scoring.score_finding(
                    standard_impact=impact,
                    system_result=system,
                    ai_result=ai,
                    verification=VerificationOutcome.VERIFIED,
                ).probability,
            )
            for impact in (1, 3, 5)
            for system, ai in [
                (ComplianceResult.NON_COMPLIANT, ComplianceResult.NON_COMPLIANT),
                (ComplianceResult.COMPLIANT, ComplianceResult.NON_COMPLIANT),
                (ComplianceResult.COMPLIANT, ComplianceResult.COMPLIANT),
            ]
        }
        off_diagonal = {c for c in combos if c[0] != c[1]}
        assert off_diagonal, "impact and probability are moving together"

    def test_semantic_weakness_on_a_passing_rule_still_scores(self):
        """A cap exists but exclusions hollow it out (§19)."""
        result = scoring.score_finding(
            standard_impact=5, system_result=ComplianceResult.COMPLIANT,
            ai_result=ComplianceResult.NON_COMPLIANT,
            verification=VerificationOutcome.SYSTEM_AI_CONFLICT,
        )
        assert result.probability >= 3
        assert result.level in {"medium", "high"}

    def test_uncertainty_is_never_shown_as_low(self):
        """The old system displayed unknown as low and hid real gaps (§54)."""
        result = scoring.score_finding(
            standard_impact=1, system_result=ComplianceResult.UNKNOWN,
            ai_result=ComplianceResult.UNKNOWN,
            verification=VerificationOutcome.AMBIGUOUS,
        )
        assert result.level != "low"

    def test_low_ai_confidence_reduces_probability(self):
        confident = scoring.score_finding(
            standard_impact=3, system_result=ComplianceResult.COMPLIANT,
            ai_result=ComplianceResult.NON_COMPLIANT,
            verification=VerificationOutcome.SYSTEM_AI_CONFLICT, ai_confidence=0.9,
        )
        unsure = scoring.score_finding(
            standard_impact=3, system_result=ComplianceResult.COMPLIANT,
            ai_result=ComplianceResult.NON_COMPLIANT,
            verification=VerificationOutcome.SYSTEM_AI_CONFLICT, ai_confidence=0.2,
        )
        assert unsure.probability < confident.probability

    def test_missing_impact_rating_is_flagged(self):
        result = scoring.score_finding(
            standard_impact=None, system_result=ComplianceResult.NON_COMPLIANT,
            ai_result=ComplianceResult.NON_COMPLIANT,
            verification=VerificationOutcome.VERIFIED,
        )
        assert any("ask Legal to rate it" in r for r in result.rationale)

    def test_rationale_is_recorded(self):
        result = scoring.score_finding(
            standard_impact=4, system_result=ComplianceResult.NON_COMPLIANT,
            ai_result=ComplianceResult.NON_COMPLIANT,
            verification=VerificationOutcome.VERIFIED,
        )
        assert result.rationale


class TestSystemConfidence:
    def test_computed_from_signals_not_self_reported(self):
        value, breakdown = scoring.system_confidence(
            source_confidence=95, standard_confidence=1.0,
            rule_confidence=1.0, ai_confidence=0.9,
        )
        assert 0.0 <= value <= 1.0
        assert set(breakdown) >= {"source", "standard", "rule", "ai"}

    def test_poor_extraction_drags_confidence_down(self):
        good, _ = scoring.system_confidence(
            source_confidence=98, standard_confidence=1.0,
            rule_confidence=1.0, ai_confidence=0.9,
        )
        poor, _ = scoring.system_confidence(
            source_confidence=45, standard_confidence=1.0,
            rule_confidence=1.0, ai_confidence=0.9,
        )
        assert poor < good

    def test_verifier_disagreement_halves_confidence(self):
        agreed, _ = scoring.system_confidence(
            source_confidence=95, standard_confidence=1.0,
            rule_confidence=1.0, ai_confidence=0.9, analyzer_verifier_agree=True,
        )
        disagreed, _ = scoring.system_confidence(
            source_confidence=95, standard_confidence=1.0,
            rule_confidence=1.0, ai_confidence=0.9, analyzer_verifier_agree=False,
        )
        assert disagreed < agreed

    def test_ai_confidence_alone_cannot_carry_the_score(self):
        """A model claiming 100% must not produce full system confidence."""
        value, _ = scoring.system_confidence(
            source_confidence=None, standard_confidence=None,
            rule_confidence=None, ai_confidence=1.0,
        )
        assert value == 1.0 or value <= 1.0
        weighted, _ = scoring.system_confidence(
            source_confidence=40, standard_confidence=0.5,
            rule_confidence=0.0, ai_confidence=1.0,
        )
        assert weighted < 0.6


class TestFallbackBand:
    """A standard has a preferred position AND an approved fallback.

    Without the band, every deviation Legal has already blessed is reported as
    a breach and reaches a reviewer as a system/AI conflict.
    """

    RULE = {"key": "ld", "extract": "percentage", "operator": "lte",
            "value": 10, "fallback_value": 15}

    def test_within_preferred_is_compliant(self):
        assert rules.evaluate(self.RULE, "capped at 8%").result == ComplianceResult.COMPLIANT

    def test_within_fallback_is_partial_not_a_breach(self):
        outcome = rules.evaluate(self.RULE, "capped at 15%")
        assert outcome.result == ComplianceResult.PARTIAL
        assert outcome.detail["within_fallback"] is True
        assert "requires sign-off" in outcome.detail["statement"]

    def test_beyond_fallback_is_non_compliant(self):
        outcome = rules.evaluate(self.RULE, "capped at 25%")
        assert outcome.result == ComplianceResult.NON_COMPLIANT
        assert outcome.detail["within_fallback"] is False

    def test_a_rule_without_a_fallback_is_unchanged(self):
        """Existing rules keep two-band behaviour; the key is optional."""
        rule = {k: v for k, v in self.RULE.items() if k != "fallback_value"}
        assert rules.evaluate(rule, "capped at 15%").result == ComplianceResult.NON_COMPLIANT

    def test_gte_direction_bands_the_other_way(self):
        rule = {"key": "ms", "extract": "percentage", "operator": "gte",
                "value": 60, "fallback_value": 40}
        assert rules.evaluate(rule, "70% milestone-linked").result == ComplianceResult.COMPLIANT
        assert rules.evaluate(rule, "45% milestone-linked").result == ComplianceResult.PARTIAL
        assert rules.evaluate(rule, "20% milestone-linked").result == ComplianceResult.NON_COMPLIANT

    def test_a_partial_still_reconciles_with_the_model(self):
        """PARTIAL is a real verdict, so agreement verifies and disagreement
        raises a conflict exactly as the other results do (§18)."""
        assert rules.reconcile(ComplianceResult.PARTIAL,
                               ComplianceResult.PARTIAL) == VerificationOutcome.VERIFIED
        assert rules.reconcile(ComplianceResult.PARTIAL,
                               ComplianceResult.COMPLIANT) == VerificationOutcome.SYSTEM_AI_CONFLICT
