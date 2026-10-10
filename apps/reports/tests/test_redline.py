"""The redline export.

Two things matter here and neither is cosmetic:

1. The changes must be *real* Word revisions (`w:ins` / `w:del`), or counsel
   cannot Accept or Reject them and the file is only a picture of a redline.
2. The proposed wording must never be text a model wrote. This document leaves
   AJi and goes to a counterparty; once language is inside tracked changes,
   nobody reviewing it can tell generated text from approved text.
"""
from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from django.core.management import call_command

from apps.accounts.models import Entity, User
from apps.analysis.models import AnalysisRun, AnalysisStatus, Finding
from apps.contracts.models import Contract, ContractType
from apps.reports import redline
from apps.standards.models import ClauseCategory

pytestmark = pytest.mark.django_db

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def document_xml(data: bytes) -> str:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


@pytest.fixture
def seeded(settings):
    settings.DEBUG = True
    call_command("seed_reference", verbosity=0)
    call_command("seed_demo_standards", verbosity=0)


@pytest.fixture
def contract(seeded):
    user = User.objects.create_user(
        email="redline@aji.test", password="test-password-1234", role="user"
    )
    return Contract.objects.create(
        project_name="Coastal District Masterplan",
        contract_title="Consultancy Agreement",
        client_name="NEOM Development Company",
        country="SA",
        entity=Entity.objects.first(),
        contract_type=ContractType.objects.first(),
        uploaded_by=user,
        reference="AJI-SA-2026-0417",
    )


@pytest.fixture
def run(contract):
    return AnalysisRun.objects.create(
        contract=contract, version=1, status=AnalysisStatus.SUCCEEDED
    )


def make_finding(run, **kwargs):
    defaults = dict(
        analysis=run,
        contract=run.contract,
        category=ClauseCategory.objects.get(code="delayLiq"),
        title="Delay damages deviate from the AJi standard",
        clause_number="3.1",
        risk_level="high",
        risk_score=15,
        original_wording="capped at 15% of the total contract value",
        fallback_wording="capped at 10% of the total contract value",
        deviation="The cap exceeds the AJi preferred position.",
    )
    defaults.update(kwargs)
    return Finding.objects.create(**defaults)


class TestATranslationNeverLeavesTheBuilding:
    """The document goes to a counterparty.

    A translated clause is a different clause. The reader may switch the whole
    screen to Arabic, but the redline must still carry the wording the
    contract was signed in and the wording Legal approved -- never the
    machine translation shown beside it, and never the model's prose.
    """

    def test_the_clause_translation_is_not_exported(self, contract, run):
        make_finding(
            run,
            wording_translations={
                "ar": "TRANSLATED-CLAUSE-AR",
                "en": "TRANSLATED-CLAUSE-EN",
            },
        )
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        ))
        assert "TRANSLATED-CLAUSE-AR" not in xml
        assert "TRANSLATED-CLAUSE-EN" not in xml
        # the verbatim clause and the approved replacement are still there
        assert "capped at 15%" in xml
        assert "capped at 10%" in xml

    def test_the_translated_prose_is_not_exported(self, contract, run):
        make_finding(
            run,
            prose={
                "ar": {"recommended_action": "PROSE-AR", "title": "TITLE-AR"},
                "en": {"recommended_action": "PROSE-EN", "title": "TITLE-EN"},
            },
        )
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        ))
        for marker in ("PROSE-AR", "PROSE-EN", "TITLE-AR", "TITLE-EN"):
            assert marker not in xml

    def test_switching_the_reader_to_arabic_does_not_change_the_export(
        self, contract, run
    ):
        from django.utils import translation

        make_finding(
            run, wording_translations={"ar": "TRANSLATED-CLAUSE-AR"}
        )
        with translation.override("en"):
            english_view = document_xml(redline.build_redline(
                contract=contract, run=run, changes=redline.changes_for_run(run)
            ))
        with translation.override("ar"):
            arabic_view = document_xml(redline.build_redline(
                contract=contract, run=run, changes=redline.changes_for_run(run)
            ))
        assert "capped at 15%" in english_view
        assert "capped at 15%" in arabic_view
        assert "TRANSLATED-CLAUSE-AR" not in arabic_view


class TestTrackedChanges:
    def test_the_file_is_a_real_docx(self, contract, run):
        make_finding(run)
        data = redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        )
        with zipfile.ZipFile(BytesIO(data)) as archive:
            assert "word/document.xml" in archive.namelist()
            assert "[Content_Types].xml" in archive.namelist()

    def test_the_original_wording_is_a_word_deletion(self, contract, run):
        make_finding(run)
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        ))
        assert "<w:del " in xml
        # A deletion must use w:delText; w:t inside w:del is invalid and Word
        # silently drops it.
        assert "<w:delText" in xml
        assert "capped at 15%" in xml

    def test_the_replacement_is_a_word_insertion(self, contract, run):
        make_finding(run)
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        ))
        assert "<w:ins " in xml
        assert "capped at 10%" in xml

    def test_revisions_are_attributed_and_dated(self, contract, run):
        """Word needs an author and timestamp per revision to show them in the
        reviewing pane."""
        make_finding(run)
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        ))
        assert 'w:author="Sanad"' in xml
        assert 'w:date="' in xml

    def test_revision_ids_are_unique(self, contract, run):
        import re

        make_finding(run)
        make_finding(run, clause_number="4.2", title="Second finding")
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        ))
        ids = re.findall(r'<w:(?:ins|del) w:id="(\d+)"', xml)
        assert len(ids) == len(set(ids)), "duplicate revision ids confuse Word"


class TestWhatMayBeProposed:
    """The safety property: only a human's wording is ever inserted."""

    def test_legal_approved_fallback_is_proposed(self, contract, run):
        make_finding(run)
        change = redline.changes_for_run(run)[0]
        assert change.replacement == "capped at 10% of the total contract value"
        assert change.is_actionable

    def test_a_reviewers_edit_outranks_the_stock_wording(self, contract, run):
        from apps.analysis.models import ReviewDecision

        finding = make_finding(run)
        reviewer = User.objects.create_user(
            email="rev@aji.test", password="test-password-1234", role="contract_reviewer"
        )
        ReviewDecision.objects.create(
            finding=finding, contract=contract, decided_by=reviewer,
            decision="accept",
            edited_fallback_wording="capped at 12%, approved by Legal Affairs",
        )
        change = redline.changes_for_run(run)[0]
        assert change.replacement == "capped at 12%, approved by Legal Affairs"

    def test_a_finding_without_approved_wording_proposes_nothing(self, contract, run):
        """It is still listed -- as a flag for Legal, with no insertion."""
        make_finding(run, fallback_wording="")
        changes = redline.changes_for_run(run)
        assert len(changes) == 1
        assert changes[0].replacement == ""
        assert not changes[0].is_actionable

        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=changes
        ))
        assert "<w:ins " not in xml, "nothing may be inserted without approved wording"
        assert "capped at 15%" in xml, "the clause must still be reported"

    def test_model_wording_is_not_carried_into_the_document(self, contract, run):
        """`deviation` is the model's prose. It may explain a change; it must
        never become one."""
        make_finding(
            run, fallback_wording="",
            deviation="Suggest: the cap shall not exceed 10 per cent.",
        )
        changes = redline.changes_for_run(run)
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=changes
        ))
        assert "<w:ins " not in xml


class TestSuggestedWordingIsNotCalledApproved:
    """`fallback_wording` falls back to the model's suggestion when the
    standard has no approved text; the redline labelled that "approved"."""

    def test_wording_off_the_standard_is_labelled_approved(self, contract, run):
        from apps.standards.models import StandardVersion

        version = StandardVersion.objects.exclude(approved_fallback_wording="").first()
        make_finding(run, standard_version=version, fallback_wording=version.approved_fallback_wording)
        change = redline.changes_for_run(run)[0]
        assert change.is_actionable and not change.is_suggested

    def test_a_suggestion_says_it_is_not_approved(self, contract, run):
        from django.utils import translation

        make_finding(run)          # no standard version: the wording is a suggestion
        with translation.override("en"):
            changes = redline.changes_for_run(run)
            xml = document_xml(redline.build_redline(contract=contract, run=run, changes=changes))
        assert changes[0].is_suggested
        assert "Approved wording" not in xml
        assert "Suggested by Sanad" in xml
        assert "not been approved by Legal" in xml       # the warning at the top

    def test_the_clauses_own_words_are_not_proposed(self, contract, run):
        make_finding(run, fallback_wording="capped at 15% of the total contract value")
        assert not redline.changes_for_run(run)[0].is_actionable

    def test_a_note_is_not_proposed_as_wording(self, contract, run):
        make_finding(run, fallback_wording="N/A - Clause text is missing.")
        changes = redline.changes_for_run(run)
        assert not changes[0].is_actionable
        xml = document_xml(redline.build_redline(contract=contract, run=run, changes=changes))
        assert "<w:ins " not in xml
        assert "Clause text is missing" not in xml


class TestDocumentWarnings:
    def test_a_degraded_run_is_declared_in_the_file(self, contract, run):
        run.status = AnalysisStatus.DEGRADED
        run.save(update_fields=["status"])
        make_finding(run)
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=redline.changes_for_run(run)
        ))
        assert "غير مكتمل" in xml

    def test_demo_standards_are_declared_in_the_file(self, contract, run, settings):
        from apps.standards.models import StandardVersion

        version = StandardVersion.objects.filter(is_demo_fixture=True).first()
        make_finding(run, standard_version=version)
        changes = redline.changes_for_run(run)
        xml = document_xml(redline.build_redline(
            contract=contract, run=run, changes=changes
        ))
        assert "بيانات اختبار" in xml


class TestAccess:
    def test_export_requires_the_capability(self, client, contract, run):
        """The uploader can see their own contract, so this isolates the
        capability check from the row scoping."""
        make_finding(run)
        contract.current_analysis = run
        contract.save(update_fields=["current_analysis"])

        client.force_login(contract.uploaded_by)
        response = client.get(f"/reports/{contract.pk}/redline/")
        assert response.status_code == 403

    def test_a_contract_outside_your_scope_is_not_even_acknowledged(
        self, client, contract, run
    ):
        """404 rather than 403: a refusal that confirms the contract exists
        leaks the portfolio to anyone who can guess an id."""
        make_finding(run)
        contract.current_analysis = run
        contract.save(update_fields=["current_analysis"])

        outsider = User.objects.create_user(
            email="outsider@aji.test", password="test-password-1234", role="user",
        )
        client.force_login(outsider)
        response = client.get(f"/reports/{contract.pk}/redline/")
        assert response.status_code == 404

    def test_a_reviewer_gets_a_docx(self, client, contract, run):
        make_finding(run)
        contract.current_analysis = run
        contract.save(update_fields=["current_analysis"])

        reviewer = User.objects.create_user(
            email="r2@aji.test", password="test-password-1234",
            role="contract_reviewer", entity=contract.entity,
        )
        client.force_login(reviewer)
        response = client.get(f"/reports/{contract.pk}/redline/")
        assert response.status_code == 200
        assert "wordprocessingml" in response["Content-Type"]
        assert "redline.docx" in response["Content-Disposition"]

    def test_a_contract_with_no_analysis_is_refused_clearly(self, client, contract):
        reviewer = User.objects.create_user(
            email="r3@aji.test", password="test-password-1234",
            role="contract_reviewer", entity=contract.entity,
        )
        client.force_login(reviewer)
        response = client.get(f"/reports/{contract.pk}/redline/")
        assert response.status_code == 409


class TestTheFont:
    def test_noto_kufi_is_named_and_described(self, contract, run):
        """Described as a sans-serif ANSI face, so Word neither falls back to
        a serif nor treats it as Arabic-only (which put Latin in Calibri).
        No alternate name while it is embedded: with Arial named, Word used
        Arial for digits and bold runs."""
        make_finding(run)
        data = redline.build_redline(contract=contract, run=run, changes=redline.changes_for_run(run))
        with zipfile.ZipFile(BytesIO(data)) as archive:
            table = archive.read("word/fontTable.xml").decode("utf-8")
            styles = archive.read("word/styles.xml").decode("utf-8")
        entry = table.split('w:name="Noto Kufi Arabic"', 1)[1].split("</w:font>", 1)[0]
        assert 'w:family w:val="swiss"' in entry and 'w:charset w:val="00"' in entry
        assert "w:altName" not in entry
        assert 'w:ascii="Noto Kufi Arabic"' in styles and "Draft B" not in styles

    def test_noto_kufi_is_carried_inside_the_file(self, contract, run):
        """Embedded, so the redline is in Noto Kufi Arabic on a machine that
        does not have it; both files decode back to the font exactly."""
        from lxml import etree

        make_finding(run)
        data = redline.build_redline(contract=contract, run=run, changes=redline.changes_for_run(run))
        with zipfile.ZipFile(BytesIO(data)) as archive:
            table = etree.fromstring(archive.read("word/fontTable.xml"))
            rels = {r.get("Id"): r.get("Target") for r in etree.fromstring(archive.read("word/_rels/fontTable.xml.rels"))}
            ns = {"w": W, "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
            font = table.find(".//w:font[@w:name='Noto Kufi Arabic']", ns)
            for slot, name in (("embedRegular", "noto-kufi-arabic-400.ttf"), ("embedBold", "noto-kufi-arabic-700.ttf")):
                embed = font.find(f"w:{slot}", ns)
                raw = archive.read("word/" + rels[embed.get(f"{{{ns['r']}}}id")])
                assert redline._obfuscate(raw, embed.get(f"{{{W}}}fontKey")) == redline._font_bytes(name)
            assert "embedTrueTypeFonts" in archive.read("word/settings.xml").decode()

    def test_pdf_ligatures_are_written_out(self):
        """No glyph for "ﬁ" in the font: Word set the rest of the run in Arial."""
        assert redline._clean("ﬁxed ﬂoor eﬀect  area 500 m²") == "fixed floor effect area 500 m²"

    def test_a_character_the_font_lacks_gets_its_own_substitute_run(self, contract, run):
        """"±" is not in Noto Kufi: inside a run, Word set it and the rest of
        the run in a face of its choosing, and a long clause broke apart."""
        make_finding(run, fallback_wording="", original_wording="cost estimate (±25%) for each stage")
        data = redline.build_redline(contract=contract, run=run, changes=redline.changes_for_run(run))
        xml = document_xml(data)
        segment = xml.split("±", 1)[0].rsplit("<w:r>", 1)[-1]
        assert 'w:ascii="Arial"' in segment, "the ± run names the substitute face"
        after = xml.split("±", 1)[1].split("</w:r>", 2)[1]
        assert 'w:ascii="Arial"' not in after, "the text after it goes back to the document face"
        assert "cost estimate (" in xml and "25%) for each stage" in xml
