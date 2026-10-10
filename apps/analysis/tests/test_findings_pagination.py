"""The findings tab is paged.

Each finding is a tall card -- verdicts, the clause against the standard, the
reasoning -- so a contract with thirty of them is unreadable in one scroll.

Two things must survive paging: the tab label counts every finding, not the
page, and a page link must not drop the reader back to the summary tab.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.analysis.models import Finding
from apps.analysis.views import AnalysisDetailView

pytestmark = pytest.mark.django_db

PER_PAGE = AnalysisDetailView.FINDINGS_PER_PAGE


@pytest.fixture
def many_findings(run_with_finding):
    """Enough findings to need more than one page."""
    run = run_with_finding
    first = run.findings.first()
    for i in range(PER_PAGE + 2):
        Finding.objects.create(
            analysis=run,
            contract=run.contract,
            category=first.category,
            title=f"Finding number {i}",
            risk_level="medium",
            risk_score=10,
        )
    return run


def _get(client, run, **params):
    url = reverse("analysis:detail", args=[run.pk])
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"{url}?{query}" if query else url)


class TestTheFindingsTabIsPaged:
    def test_a_page_holds_at_most_the_page_size(self, signed_in, many_findings):
        body = _get(signed_in, many_findings, tab="findings").content.decode()
        assert body.count('class="finding"') == PER_PAGE

    def test_the_second_page_holds_the_remainder(self, signed_in, many_findings):
        total = many_findings.findings.count()
        body = _get(signed_in, many_findings, tab="findings", page=2).content.decode()
        assert body.count('class="finding"') == total - PER_PAGE

    def test_the_tab_label_counts_every_finding_not_the_page(
        self, signed_in, many_findings
    ):
        total = many_findings.findings.count()
        body = _get(signed_in, many_findings, tab="findings").content.decode()
        assert f"({total})" in body

    def test_a_page_link_keeps_the_reader_on_the_findings_tab(
        self, signed_in, many_findings
    ):
        body = _get(signed_in, many_findings, tab="findings").content.decode()
        assert "tab=findings&amp;page=2" in body

    def test_a_page_past_the_end_shows_the_last_page_not_an_error(
        self, signed_in, many_findings
    ):
        response = _get(signed_in, many_findings, tab="findings", page=999)
        assert response.status_code == 200
        assert response.content.decode().count('class="finding"') > 0

    def test_a_page_that_is_not_a_number_is_not_an_error(
        self, signed_in, many_findings
    ):
        response = _get(signed_in, many_findings, tab="findings", page="abc")
        assert response.status_code == 200

    def test_a_short_list_shows_no_pager_at_all(self, signed_in, run_with_finding):
        body = _get(signed_in, run_with_finding, tab="findings").content.decode()
        assert 'class="listfoot"' not in body


class TestThePagesAreNumbered:
    def test_every_page_has_its_own_numbered_link(self, signed_in, many_findings):
        body = _get(signed_in, many_findings, tab="findings").content.decode()
        pages = -(-many_findings.findings.count() // PER_PAGE)
        for n in range(2, pages + 1):
            assert f"tab=findings&amp;page={n}" in body

    def test_the_current_page_is_marked_and_is_not_a_link(
        self, signed_in, many_findings
    ):
        body = _get(signed_in, many_findings, tab="findings", page=2).content.decode()
        assert 'aria-current="page"' in body
        # the number you are on should not link back to itself
        assert 'class="pagenum num" href="?tab=findings&amp;page=2"' not in body

    def test_a_long_list_elides_rather_than_printing_every_number(
        self, signed_in, run_with_finding
    ):
        """Forty pages of numbers is not navigation, it is a wall."""
        run = run_with_finding
        first = run.findings.first()
        Finding.objects.bulk_create([
            Finding(
                analysis=run, contract=run.contract, category=first.category,
                title=f"Bulk finding {i}", risk_level="low", risk_score=4,
            )
            for i in range(PER_PAGE * 30)
        ])
        body = _get(signed_in, run, tab="findings").content.decode()
        assert "pagenum--gap" in body


class TestTheMatrixSideColumn:
    """The panel beside the grid must be real, filtered data.

    It exists to fill the space next to a short summary column -- but a panel
    that ignored the band filter would contradict the cards directly above it.
    """

    def test_it_counts_only_the_filtered_band(self, signed_in, run_with_finding):
        from django.urls import reverse

        url = reverse("analysis:risk_matrix")
        everything = signed_in.get(url).context["categories"]
        high_only = signed_in.get(f"{url}?risk=high").context["categories"]
        low_only = signed_in.get(f"{url}?risk=low").context["categories"]

        assert sum(c["n"] for c in everything) >= sum(c["n"] for c in high_only)
        # the fixture's single finding is high risk, so low must be empty
        assert low_only == []

    def test_it_is_absent_rather_than_empty_when_nothing_matches(
        self, signed_in, run_with_finding
    ):
        from django.urls import reverse

        body = signed_in.get(
            f"{reverse('analysis:risk_matrix')}?risk=low"
        ).content.decode()
        assert "أين تتركّز المخاطر" not in body


class TestTheMatrixLogIsPaged:
    """The log under the matrix is the long part of that page.

    The band filter sits directly above it, so a page link that dropped the
    filter would show every risk level while the button still read "مرتفعة".
    """

    @staticmethod
    def _url(**params):
        from django.urls import reverse

        url = reverse("analysis:risk_matrix")
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{query}" if query else url

    def test_a_page_holds_at_most_the_page_size(self, signed_in, run_with_finding):
        from apps.analysis.views import RiskMatrixView

        response = signed_in.get(self._url())
        assert len(response.context["findings"]) <= RiskMatrixView.LOG_PER_PAGE

    def test_a_page_link_carries_the_band_filter(self, signed_in, run_with_finding):
        from apps.analysis.models import Finding
        from apps.analysis.views import RiskMatrixView

        run = run_with_finding
        first = run.findings.first()
        Finding.objects.bulk_create([
            Finding(
                analysis=run, contract=run.contract, category=first.category,
                title=f"High finding {i}", risk_level="high",
                impact=5, probability=4, risk_score=20,
            )
            for i in range(RiskMatrixView.LOG_PER_PAGE + 1)
        ])
        body = signed_in.get(self._url(risk="high")).content.decode()
        assert "risk=high&amp;page=2" in body

    def test_an_out_of_range_page_is_not_an_error(self, signed_in, run_with_finding):
        assert signed_in.get(self._url(page=999)).status_code == 200
