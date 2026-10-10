"""Filtering the findings log by risk band.

The buttons carry counts, and the counts were taken from the findings *after*
the filter had been applied -- so choosing "مرتفعة" left that button showing
its own total and the other two showing zero, as though the medium and low
findings had been deleted rather than hidden. The numbers label the controls
that apply the filter, so they have to describe the whole log.

What the filter does change is everything below those buttons: the grid, the
concentration list and the table all count the filtered findings.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.analysis.models import Finding

pytestmark = pytest.mark.django_db

MATRIX = reverse("analysis:risk_matrix")


@pytest.fixture
def findings(run_with_finding, contract):
    """One high already exists; add two medium and one low.

    All in the same category, because the concentration list counts only
    findings that have one.
    """
    category = Finding.objects.get(analysis=run_with_finding).category
    for level, impact, score in (("medium", 3, 9), ("medium", 3, 9), ("low", 1, 2)):
        Finding.objects.create(
            analysis=run_with_finding, contract=contract, category=category,
            risk_level=level, impact=impact, probability=3, risk_score=score,
            title="A %s finding" % level,
            system_result="non_compliant", ai_result="non_compliant",
            verification_status="verified",
        )
    return run_with_finding


def totals(client, query=""):
    return client.get(MATRIX + query).context["totals"]


class TestTheCountsOnTheButtons:
    def test_unfiltered(self, client, reviewer, findings):
        client.force_login(reviewer)
        assert totals(client) == {"high": 1, "medium": 2, "low": 1, "all": 4}

    @pytest.mark.parametrize("band", ["high", "medium", "low"])
    def test_they_do_not_change_when_a_band_is_chosen(
            self, client, reviewer, findings, band):
        """The whole point of the numbers is to say what choosing each band
        would give you. Counting the filtered set makes them say nothing."""
        client.force_login(reviewer)
        assert totals(client, "?risk=" + band) == {
            "high": 1, "medium": 2, "low": 1, "all": 4}

    def test_all_is_the_sum_of_the_bands_when_everything_is_classified(
            self, client, reviewer, findings):
        client.force_login(reviewer)
        counts = totals(client)
        assert counts["all"] == counts["high"] + counts["medium"] + counts["low"]

    def test_an_unclassified_finding_is_still_counted_in_all(
            self, client, reviewer, contract, run_with_finding, findings):
        """`RiskLevel.UNKNOWN` is a real state, and §54 is explicit that it
        must not be quietly folded into another band. "الكل" is counted rather
        than added up, so a finding nobody has classified is still in it --
        and the three bands no longer summing to it is the visible sign that
        something is waiting to be classified."""
        Finding.objects.create(
            analysis=run_with_finding, contract=contract,
            risk_level="unknown", impact=None, probability=None,
            title="Not classified yet",
            system_result="non_compliant", ai_result="non_compliant",
            verification_status="unverified",
        )
        client.force_login(reviewer)
        counts = totals(client)

        bands = counts["high"] + counts["medium"] + counts["low"]
        assert counts["all"] == bands + 1
        # And clicking الكل really does list it.
        assert len(client.get(MATRIX).context["findings"]) == counts["all"]

    def test_a_nonsense_band_is_ignored_rather_than_obeyed(
            self, client, reviewer, findings):
        client.force_login(reviewer)
        response = client.get(MATRIX + "?risk=purple")
        assert response.context["risk_filter"] == "all"
        assert len(response.context["findings"]) == 4


class TestWhatTheFilterDoesChange:
    def test_the_table(self, client, reviewer, findings):
        client.force_login(reviewer)
        rows = client.get(MATRIX + "?risk=medium").context["findings"]
        assert len(rows) == 2
        assert {f.risk_level for f in rows} == {"medium"}

    def test_the_concentration_list(self, client, reviewer, findings):
        """It is inside the swapped region for this reason: filtered rows
        beside an unfiltered summary is two answers to the same question."""
        client.force_login(reviewer)
        everything = client.get(MATRIX).context["categories"]
        just_high = client.get(MATRIX + "?risk=high").context["categories"]

        assert sum(c["n"] for c in everything) == 4
        assert sum(c["n"] for c in just_high) == 1

    def test_the_grid(self, client, reviewer, findings):
        client.force_login(reviewer)
        everything = client.get(MATRIX).context["rows"]
        just_low = client.get(MATRIX + "?risk=low").context["rows"]

        def total(grid):
            return sum(cell["count"] for row in grid for cell in row["cells"])

        assert total(everything) == 4
        assert total(just_low) == 1


class TestTheConcentrationBars:
    """The bar is a picture of the fraction printed beside it.

    Both of the other things it could measure were tried, and both produced a
    bar that contradicted its own label. Against the largest *total* it drew a
    full bar for "0 من 7" -- the busiest category, with nothing severe in it.
    Against the largest *high count* it drew a full bar for "6 من 7", because
    some other category happened to be 6 out of 6.

    Six sevenths of the bar is the only reading a reader can check against the
    numbers they are looking at, which is what these tests hold it to.
    """

    def test_every_bar_equals_its_own_label(self, client, reviewer, findings):
        """The invariant, stated directly: the width is the fraction."""
        client.force_login(reviewer)
        for row in client.get(MATRIX).context["categories"]:
            expected = round(row["high"] * 100 / row["n"]) if row["n"] else 0
            assert row["percent"] == expected, (
                f"{row['label']}: the label reads {row['high']} of {row['n']} "
                f"({expected}%) and the bar is {row['percent']}%"
            )

    def test_a_category_that_is_entirely_high_risk_fills_the_bar(
            self, client, reviewer, findings):
        client.force_login(reviewer)
        for row in client.get(MATRIX).context["categories"]:
            if row["n"] and row["high"] == row["n"]:
                assert row["percent"] == 100

    def test_a_category_with_no_high_findings_draws_nothing(
            self, client, reviewer, findings):
        client.force_login(reviewer)
        rows = client.get(MATRIX).context["categories"]

        for row in rows:
            if row["high"] == 0:
                assert row["percent"] == 0, (
                    f"{row['label']}: no high-risk findings but the bar is "
                    f"{row['percent']}% wide"
                )

    def test_a_busy_category_with_nothing_severe_draws_nothing(
            self, client, reviewer, contract, run_with_finding):
        """The case that showed the first bug: the category with the most
        findings is not the one with the most severe ones."""
        from apps.standards.models import ClauseCategory

        busy = ClauseCategory.objects.create(code="busy", name="Busy but calm")
        for i in range(6):
            Finding.objects.create(
                analysis=run_with_finding, contract=contract, category=busy,
                risk_level="low", impact=1, probability=1, risk_score=1,
                title=f"A quiet finding {i}",
                system_result="compliant", ai_result="compliant",
                verification_status="verified",
            )

        client.force_login(reviewer)
        rows = {r["label"]: r for r in client.get(MATRIX).context["categories"]}

        # It has the most findings of any category and not one of them severe.
        assert rows["Busy but calm"]["n"] == 6
        assert rows["Busy but calm"]["high"] == 0
        assert rows["Busy but calm"]["percent"] == 0

    def test_no_high_findings_anywhere_leaves_every_bar_empty(
            self, client, reviewer, findings):
        """Filtering to a band with nothing severe in it. Dividing by a zero
        maximum must not raise, and empty bars are the honest answer."""
        client.force_login(reviewer)
        rows = client.get(MATRIX + "?risk=low").context["categories"]
        assert rows, "the filter should still list the categories"
        assert all(r["high"] == 0 and r["percent"] == 0 for r in rows)


class TestTheFilterSurvivesPaging:
    def test_the_band_rides_along_in_the_page_links(
            self, client, reviewer, findings):
        """A bare page number would drop the band and page through everything.
        Asserted on the markup because that is where the bug would live."""
        client.force_login(reviewer)
        body = client.get(MATRIX + "?risk=medium").content.decode()
        if "pagenum" in body:
            assert "risk=medium&amp;page=" in body


class TestSwappingWithoutAReload:
    def test_the_page_offers_one_region_for_the_script_to_replace(
            self, client, reviewer, findings):
        """The script finds its region by this attribute. If the markup is
        reorganised and the wrapper is lost, filtering silently goes back to
        reloading the whole document -- which still works, so nothing else
        would notice."""
        client.force_login(reviewer)
        body = client.get(MATRIX).content.decode()
        assert body.count("data-live") == 1

    def test_the_region_holds_everything_the_filter_changes(
            self, client, reviewer, findings):
        """Grid, concentration list and table together. Swapping less left
        the first two stale beside a filtered table."""
        client.force_login(reviewer)
        body = client.get(MATRIX).content.decode()
        region = body[body.index("data-live"):]

        for marker in ("matrix", "meterlist", "bandfilter", "<table"):
            assert marker in region, f"{marker} is outside the swapped region"

    def test_the_controls_are_still_links(self, client, reviewer, findings):
        """Progressive enhancement: with no JavaScript the band buttons have
        to work on their own."""
        client.force_login(reviewer)
        body = client.get(MATRIX).content.decode()
        assert 'href="?risk=high"' in body
