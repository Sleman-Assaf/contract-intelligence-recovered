"""Uploading standards in Settings, the uniform form, approval, and the
FIDIC baseline beneath AJi's own positions."""
from __future__ import annotations

import io
import json
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.urls import reverse

from apps.accounts.models import Country, Entity, Role, User
from apps.standards import normalization
from apps.standards.models import (
    ClauseCategory,
    Standard,
    StandardProfile,
    StandardScope,
    StandardStatus,
    StandardUpload,
    StandardVersion,
)
from apps.standards.resolution import resolve_for_category

pytestmark = pytest.mark.django_db

POLICY = """AJi Legal Affairs - Contract Positions 2026

Section 7.2 Delay damages
Liquidated damages for delay must not exceed 10% of the contract value. Up to 15% may be accepted with written approval of Legal Affairs. An uncapped liability for delay is a red line.

Section 9 Confidentiality
Confidential information is protected for five years."""


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.STANDARDS_DEFAULT_PROFILE = "fidic"


@pytest.fixture
def cats():
    return {code: ClauseCategory.objects.create(code=code, name=name, name_ar=ar)
            for code, name, ar in [("delayLiq", "Liquidated damages for delay", "تعويضات التأخير"),
                                   ("confidentiality", "Confidentiality", "السرية"),
                                   ("liability", "Liability cap and indemnities", "سقف المسؤولية"),
                                   ("dispute", "Dispute resolution and arbitration", "تسوية المنازعات")]}


def _user(role, email):
    entity, _ = Entity.objects.get_or_create(code="e", defaults={"name": "E", "country": Country.JORDAN})
    return User.objects.create_user(email=email, password="x-long-passphrase-42", role=role, entity=entity)


@pytest.fixture
def admin():
    return _user(Role.ADMIN, "admin@aji.test")


@pytest.fixture
def manager():
    return _user(Role.STANDARD_MANAGER, "standards@aji.test")


@pytest.fixture
def reviewer():
    return _user(Role.CONTRACT_REVIEWER, "reviewer@aji.test")


@pytest.fixture
def model_reads(monkeypatch):
    """The model's reading of POLICY: two positions, one quote invented."""
    from apps.ai.providers.base import AICallResult
    from apps.ai.schemas import StandardExtraction
    from apps.ai.services import ai_service

    calls = []

    def fake(**kw):
        calls.append(kw)
        parsed = StandardExtraction(positions=[
            {"category": "delayLiq",
             "preferred_position": "Delay damages capped at 10% of the contract value.",
             "preferred_position_ar": "سقف تعويضات التأخير 10% من قيمة العقد.",
             "acceptable_range": "Up to 15% with Legal Affairs' written approval.",
             "red_flags": "Uncapped delay damages.",
             "rule": {"extract": "percentage", "operator": "lte", "value": 10, "fallback_value": 15},
             "source_quote": "Liquidated damages for delay must not exceed 10% of the contract value.",
             "source_section": "7.2"},
            {"category": "confidentiality",
             "preferred_position": "Confidentiality for seven years.",
             "source_quote": "Confidential information is protected for seven years.",
             "source_section": "9"},
        ])
        return AICallResult(parsed=parsed, provider="f", model="m", request_type="normalize_standard",
                            latency_ms=1, prompt_tokens=1, completion_tokens=1, raw_text="{}")

    monkeypatch.setattr(ai_service, "normalize_standard", fake)
    return calls


def upload(user, data: bytes, name: str, **kw):
    kw.setdefault("profile", StandardProfile.AJI)
    kw.setdefault("country", "")
    return normalization.create_upload(user, data, name, **kw)


class TestAnyLayoutBecomesOneForm:
    def test_a_document_is_restated_and_every_quote_checked(self, cats, admin, model_reads):
        item = upload(admin, POLICY.encode(), "policy.txt", country=Country.SAUDI_ARABIA)
        assert item.status == StandardUpload.Status.REVIEW and item.method == "ai"
        assert len(model_reads) == 1
        assert {c["code"] for c in model_reads[0]["categories"]} == set(cats)
        delay, conf = item.items
        assert delay["category"] == "delayLiq" and delay["quote_found"] is True
        assert delay["include"] is True and delay["country"] == "SA"
        assert delay["rule"] == {"extract": "percentage", "operator": "lte", "value": 10, "fallback_value": 15}
        # "seven years" is not what the document says: left unticked.
        assert conf["quote_found"] is False and conf["include"] is False
        assert "quote_not_found" in conf["problems"]

    def test_a_spreadsheet_in_the_legal_template_is_read_without_the_model(self, cats, admin, monkeypatch):
        from apps.ai.services import ai_service
        from openpyxl import Workbook

        monkeypatch.setattr(ai_service, "normalize_standard", lambda **k: pytest.fail("model called"))
        book = Workbook(); sheet = book.active
        sheet.append(["Category", "Country", "Preferred position", "Walk away", "Impact (1-5)"])
        sheet.append(["Liability cap and indemnities", "Jordan", "Liability capped at the fee.", "Unlimited liability.", 5])
        sheet.append(["السرية", "", "Five years of confidentiality.", "", 2])
        buf = io.BytesIO(); book.save(buf)
        item = upload(admin, buf.getvalue(), "positions.xlsx")
        assert item.method == "table" and item.ai_calls == 0
        liability, conf = item.items
        assert liability["category"] == "liability" and liability["country"] == "JO"
        assert liability["red_flags"] == "Unlimited liability." and liability["impact"] == 5
        assert conf["category"] == "confidentiality" and conf["country"] == ""

    def test_csv_and_json(self, cats, admin):
        csv_data = "category,preferred_position,red_flags\ndispute,Arbitration required.,No dispute clause.\n"
        assert upload(admin, csv_data.encode(), "d.csv").items[0]["category"] == "dispute"
        js = json.dumps([{"category": "delayLiq", "preferred_position": "Cap 10%.", "impact": 4}])
        assert upload(admin, js.encode(), "d.json").items[0]["impact"] == 4

    def test_what_is_refused(self, cats, admin, reviewer):
        with pytest.raises(ValidationError):
            upload(admin, b"GIF89a", "x.gif")
        with pytest.raises(PermissionDenied):
            upload(reviewer, b"category,preferred_position\n", "x.csv")

    def test_rules_the_engine_cannot_evaluate_are_dropped(self):
        assert normalization.clean_rule({"operator": "between", "value": 3}) == {}
        assert normalization.clean_rule({"extract": "percentage", "operator": "lte", "value": "10"}) == \
            {"extract": "percentage", "operator": "lte", "value": 10}
        assert normalization.clean_rule({"operator": "present", "markers": ["arbitration"]}) == \
            {"operator": "present", "markers": ["arbitration"]}

    def test_a_failure_is_recorded_not_raised(self, cats, admin, monkeypatch):
        from apps.ai.providers.base import AIProviderUnavailable
        from apps.ai.services import ai_service

        def down(**k):
            raise AIProviderUnavailable("down")

        monkeypatch.setattr(ai_service, "normalize_standard", down)
        item = upload(admin, POLICY.encode(), "policy.txt")
        assert item.status == StandardUpload.Status.FAILED and item.error


class TestDraftsThenApproval:
    def test_saved_positions_are_drafts_that_drive_nothing_yet(self, cats, admin, model_reads):
        item = upload(admin, POLICY.encode(), "policy.txt", country=Country.SAUDI_ARABIA)
        created = normalization.save_drafts(item, item.items, admin)
        assert len(created) == 1                                   # the unticked one is not saved
        v = created[0]
        assert v.status == StandardStatus.PENDING_APPROVAL and v.standard.country == "SA"
        assert v.standard.scope == StandardScope.COUNTRY and v.source_reference.startswith("Uploaded: policy.txt")
        assert resolve_for_category(cats["delayLiq"], country="SA", contract_type_id=None,
                                    fallback=False) is None

    def test_the_uploader_cannot_approve_and_impact_is_required(self, cats, admin, manager, model_reads):
        item = upload(admin, POLICY.encode(), "policy.txt", country=Country.SAUDI_ARABIA)
        v = normalization.save_drafts(item, item.items, admin)[0]
        with pytest.raises(PermissionDenied):
            normalization.approve(v, admin)                         # admin manages, does not approve
        with pytest.raises(ValidationError):
            normalization.approve(v, manager)                       # no impact yet
        normalization.set_impact(v, 5, admin)
        normalization.approve(v, manager)
        resolved = resolve_for_category(cats["delayLiq"], country="SA", contract_type_id=None)
        assert resolved.version == v and not resolved.version.is_baseline

    def test_approving_supersedes_the_version_it_replaces(self, cats, manager):
        standard = Standard.objects.create(category=cats["liability"], scope=StandardScope.GLOBAL,
                                           title="t")
        old = StandardVersion.objects.create(standard=standard, version=1, preferred_position="old",
                                             status=StandardStatus.APPROVED, effective_date=date(2020, 1, 1),
                                             impact=3, is_demo_fixture=True)
        new = StandardVersion.objects.create(standard=standard, version=2, preferred_position="new",
                                             status=StandardStatus.PENDING_APPROVAL,
                                             effective_date=date.today(), impact=4)
        normalization.approve(new, manager)
        old.refresh_from_db()
        assert old.status == StandardStatus.SUPERSEDED               # kept, for the findings citing it
        assert resolve_for_category(cats["liability"], country="JO", contract_type_id=None).version == new


class TestTheFidicBaseline:
    def test_it_applies_where_aji_has_no_position_and_steps_aside_where_it_does(self, cats, manager):
        call_command("seed_fidic_baseline", stdout=io.StringIO())
        baseline = resolve_for_category(cats["liability"], country="SA", contract_type_id=None)
        assert baseline.version.is_baseline and "fallback:fidic" in baseline.resolution_path
        standard = Standard.objects.create(category=cats["liability"], scope=StandardScope.COUNTRY,
                                           country="SA", title="t")
        StandardVersion.objects.create(standard=standard, version=1, preferred_position="AJi KSA cap",
                                       status=StandardStatus.APPROVED, effective_date=date(2021, 1, 1), impact=5)
        assert resolve_for_category(cats["liability"], country="SA", contract_type_id=None) \
            .version.preferred_position == "AJi KSA cap"
        # Jordan has no AJi position: still FIDIC.
        assert resolve_for_category(cats["liability"], country="JO", contract_type_id=None).version.is_baseline

    def test_retiring_the_demo_hands_those_categories_to_fidic(self, cats):
        standard = Standard.objects.create(category=cats["dispute"], scope=StandardScope.GLOBAL, title="t")
        demo = StandardVersion.objects.create(standard=standard, version=1, preferred_position="demo",
                                              status=StandardStatus.APPROVED, effective_date=date(2020, 1, 1),
                                              is_demo_fixture=True, impact=4)
        call_command("seed_fidic_baseline", "--retire-demo", stdout=io.StringIO())
        demo.refresh_from_db()
        assert demo.status == StandardStatus.SUPERSEDED
        assert resolve_for_category(cats["dispute"], country="SA", contract_type_id=None).version.is_baseline

    def test_it_invents_no_figures_and_says_what_it_is(self, cats):
        call_command("seed_fidic_baseline", stdout=io.StringIO())
        for v in StandardVersion.objects.filter(standard__reference_template="fidic"):
            assert "not FIDIC text" in v.source_reference
            assert not {"extract", "value"} & set(v.rule_definition)


class TestTheSettingsTab:
    def test_everyone_sees_the_library_only_managers_upload(self, client, cats, reviewer, admin):
        call_command("seed_fidic_baseline", stdout=io.StringIO())
        client.force_login(reviewer)
        body = client.get(reverse("accounts:settings") + "?tab=standards").content.decode()
        assert "FIDIC" in body and 'action="/standards/upload/"' not in body
        client.force_login(admin)
        body = client.get(reverse("accounts:settings") + "?tab=standards").content.decode()
        assert 'action="/standards/upload/"' in body

    def test_upload_review_save_approve_through_the_pages(self, client, cats, admin, manager, model_reads):
        client.force_login(admin)
        f = io.BytesIO(POLICY.encode()); f.name = "policy.txt"
        response = client.post(reverse("standards:upload"),
                               {"file": f, "country": "JO", "profile": "aji_standard",
                                "effective_date": "2026-01-01"})
        item = StandardUpload.objects.get()
        assert response.url == reverse("standards:review", args=[item.pk])
        page = client.get(response.url).content.decode()
        assert "Delay damages capped at 10%" in page
        post = {"count": "2"}
        for i, it in enumerate(item.items):
            for field in normalization.TEXT_FIELDS + ("source_quote", "source_section", "category"):
                post[f"i{i}-{field}"] = it[field]
            post[f"i{i}-rule"] = json.dumps(it["rule"])
            post[f"i{i}-impact"] = "5" if i == 0 else ""
            if it["include"]:
                post[f"i{i}-include"] = "1"
        client.post(reverse("standards:review", args=[item.pk]), post)
        version = StandardVersion.objects.get(status=StandardStatus.PENDING_APPROVAL)
        assert version.impact == 5 and version.standard.country == "JO"
        assert version.rule_definition["value"] == 10
        assert client.post(reverse("standards:approve", args=[version.pk])).status_code == 403
        client.force_login(manager)
        client.post(reverse("standards:approve", args=[version.pk]))
        version.refresh_from_db()
        assert version.status == StandardStatus.APPROVED


class TestTheLibraryListing:
    @pytest.fixture
    def many(self, cats):
        """Fifteen approved standards: more than one page."""
        extra = [ClauseCategory.objects.create(code=f"c{i}", name=f"Clause {i}", name_ar=f"بند {i}")
                 for i in range(11)]
        for n, category in enumerate([*cats.values(), *extra]):
            standard = Standard.objects.create(category=category, scope=StandardScope.GLOBAL, title="t")
            StandardVersion.objects.create(
                standard=standard, version=1, status=StandardStatus.APPROVED,
                effective_date=date(2020, 1, 1), impact=3,
                preferred_position="Insurance of SAR 25,000,000." if n == 0 else f"Position {n}.")
        return extra

    def _page(self, client, **params):
        from urllib.parse import urlencode

        return client.get(reverse("accounts:settings") + "?" + urlencode({"tab": "standards", **params}))

    def test_it_is_paged(self, client, reviewer, many):
        client.force_login(reviewer)
        first = self._page(client)
        assert len(first.context["std_rows"]) == 12
        assert first.context["std_paginator"].count == 15
        assert "page=2" in first.content.decode()
        assert len(self._page(client, page=2).context["std_rows"]) == 3

    def test_search_finds_by_clause_position_or_arabic_name(self, client, reviewer, many):
        client.force_login(reviewer)
        assert [r["standard"].category.code for r in self._page(client, q="25,000,000").context["std_rows"]] \
            == ["delayLiq"]
        assert [r["standard"].category.code for r in self._page(client, q="السرية").context["std_rows"]] \
            == ["confidentiality"]
        assert self._page(client, q="Liquidated").context["std_rows"][0]["standard"].category.code == "delayLiq"
        empty = self._page(client, q="nothing like this")
        assert not empty.context["std_rows"] and "nothing like this" in empty.content.decode()

    def test_the_pages_keep_the_search(self, client, reviewer, many):
        client.force_login(reviewer)
        body = self._page(client, q="Position").content.decode()
        assert "q=Position" in body and "page=2" in body

    def test_the_file_is_chosen_in_a_drop_zone(self, client, admin, cats):
        client.force_login(admin)
        body = self._page(client).content.decode()
        assert "data-dropzone" in body and 'data-accept-ext="pdf,docx,xlsx,csv,json,txt,md"' in body
        assert "data-file-preview" in body and "js/upload.js" in body


class TestFidicHasNoCountry:
    """FIDIC is an international form: one position for every country.
    Country-specific positions are AJi's own, and those already take
    precedence over FIDIC country by country."""

    def test_a_country_posted_with_fidic_is_dropped(self, cats, admin):
        csv_data = "category,preferred_position\nliability,Cap at the fee.\n"
        item = upload(admin, csv_data.encode(), "f.csv", profile=StandardProfile.FIDIC,
                      country=Country.SAUDI_ARABIA)
        assert item.country == "" and item.items[0]["country"] == ""

    def test_a_spreadsheet_row_cannot_make_fidic_national(self, cats, admin):
        csv_data = "category,country,preferred_position\nliability,Jordan,Cap at the fee.\n"
        item = upload(admin, csv_data.encode(), "f.csv", profile=StandardProfile.FIDIC)
        assert item.items[0]["country"] == ""
        rows = [dict(item.items[0], country="JO")]          # even if the review posts one
        version = normalization.save_drafts(item, rows, admin)[0]
        assert version.standard.scope == StandardScope.GLOBAL and version.standard.country == ""
        assert version.standard.reference_template == StandardProfile.FIDIC

    def test_aji_uploads_keep_their_country(self, cats, admin):
        csv_data = "category,preferred_position\nliability,Cap at the fee.\n"
        item = upload(admin, csv_data.encode(), "a.csv", country=Country.JORDAN)
        version = normalization.save_drafts(item, item.items, admin)[0]
        assert version.standard.country == "JO"

    def test_the_form_locks_the_country_when_fidic_is_chosen(self, client, cats, admin):
        client.force_login(admin)
        body = client.get(reverse("accounts:settings") + "?tab=standards").content.decode()
        assert "data-std-profile" in body and "data-std-country" in body
        assert "data-std-fidic-hint" in body and "js/standards.js" in body
        # The reference comes before the country it decides.
        assert body.index('name="profile"') < body.index('name="country"')

    def test_the_review_of_a_fidic_upload_offers_no_country(self, client, cats, admin):
        client.force_login(admin)
        item = upload(admin, b"category,preferred_position\nliability,Cap.\n", "f.csv",
                      profile=StandardProfile.FIDIC)
        body = client.get(reverse("standards:review", args=[item.pk])).content.decode()
        assert 'name="i0-country"' not in body and "fidic-0" in body


def test_the_library_locks_the_country_filter_for_fidic(client, cats, reviewer):
    """Like the upload form: shown, but locked on "all countries"."""
    call_command("seed_fidic_baseline", stdout=io.StringIO())
    client.force_login(reviewer)
    url = reverse("accounts:settings")

    def toolbar(body):
        part = body[body.index('class="std-toolbar"'):]
        return part[:part.index("</form>")]

    plain = toolbar(client.get(url + "?tab=standards").content.decode())
    assert 'name="country"' in plain and "is-locked" not in plain
    assert plain.index('name="profile"') < plain.index('name="country"')
    fidic = client.get(url + "?tab=standards&profile=fidic&country=SA")
    bar = toolbar(fidic.content.decode())
    country = bar[bar.index('name="country"'):]
    assert "is-locked" in bar and country[:country.index(">")].rstrip().endswith("disabled")
    assert 'value="SA" selected' not in bar                    # locked on "all countries"
    assert fidic.context["std_country"] == ""                  # ignored, not applied
    assert "country=" not in fidic.context["std_qs"]
    assert len(fidic.context["std_rows"]) > 0


def test_the_dropdowns_are_drawn_lists_and_the_upload_form_never_submits_on_change(client, cats, admin):
    client.force_login(admin)
    body = client.get(reverse("accounts:settings") + "?tab=standards").content.decode()
    assert body.count('data-richselect="field"') == 4            # two in the form, two in the filter
    form = body[body.index('<form class="std-upload"'):]
    # The live filter submits a form when one of its selects changes; this one
    # uploads, so it must opt out.
    assert "data-no-live" in form[:form.index(">")]


def test_the_forms_explain_themselves(client, cats, admin):
    """A guide above the review cards and a hint under every field, so the
    form can be used without being walked through it."""
    client.force_login(admin)
    tab = client.get(reverse("accounts:settings") + "?tab=standards").content.decode()
    form = tab[tab.index('<form class="std-upload"'):]
    form = form[:form.index("</form>")]
    assert form.count('class="field__hint"') >= 4
    assert "data-std-country-hint" in form                      # swapped for the FIDIC note by standards.js
    item = upload(admin, b"category,preferred_position\nliability,Cap at the fee.\n", "a.csv")
    page = client.get(reverse("standards:review", args=[item.pk])).content.decode()
    assert 'class="std-guide"' in page and page.count("<li>") >= 4
    card = page[page.index('class="std-item'):]
    assert card.count('class="field__hint"') >= 12
    assert "std-switch__hint" in card


class TestTheUploadedFilesList:
    def test_it_is_paged_on_its_own(self, client, cats, admin):
        client.force_login(admin)
        for n in range(10):
            upload(admin, f"category,preferred_position\nliability,Cap {n}.\n".encode(), f"f{n}.csv")
        url = reverse("accounts:settings") + "?tab=standards"
        first = client.get(url)
        assert len(first.context["std_uploads"]) == 8 and first.context["std_upaginator"].count == 10
        assert "upage=2" in first.content.decode()
        second = client.get(url + "&upage=2")
        assert len(second.context["std_uploads"]) == 2
        assert len(second.context["std_rows"]) == len(first.context["std_rows"])   # the library untouched

    def test_delete_asks_first_and_removes_the_file(self, client, cats, admin, settings):
        from pathlib import Path

        client.force_login(admin)
        item = upload(admin, b"category,preferred_position\nliability,Cap.\n", "gone.csv")
        path = Path(settings.PRIVATE_MEDIA_ROOT) / item.stored_path
        assert path.exists()
        body = client.get(reverse("accounts:settings") + "?tab=standards").content.decode()
        assert f'action="{reverse("standards:discard", args=[item.pk])}"' in body
        assert 'data-confirm-title=' in body                       # the confirmation dialog, not a bare submit
        response = client.post(reverse("standards:discard", args=[item.pk]),
                               {"next": "/accounts/settings/?tab=standards&upage=2"})
        assert response.url == "/accounts/settings/?tab=standards&upage=2"
        assert not StandardUpload.objects.filter(pk=item.pk).exists() and not path.exists()

    def test_deleting_a_saved_file_keeps_its_standards(self, client, cats, admin):
        client.force_login(admin)
        item = upload(admin, b"category,preferred_position\nliability,Cap at the fee.\n", "kept.csv")
        version = normalization.save_drafts(item, item.items, admin)[0]
        client.post(reverse("standards:discard", args=[item.pk]))
        assert StandardVersion.objects.filter(pk=version.pk).exists()

    def test_a_shared_file_stays_for_the_other_upload(self, client, cats, admin, settings):
        from pathlib import Path

        client.force_login(admin)
        data = b"category,preferred_position\nliability,Cap.\n"
        a, b = upload(admin, data, "a.csv"), upload(admin, data, "b.csv")
        assert a.stored_path == b.stored_path
        client.post(reverse("standards:discard", args=[a.pk]))
        assert (Path(settings.PRIVATE_MEDIA_ROOT) / b.stored_path).exists()

    def test_only_standards_managers_can_delete(self, client, cats, admin, reviewer):
        item = upload(admin, b"category,preferred_position\nliability,Cap.\n", "x.csv")
        client.force_login(reviewer)
        assert client.post(reverse("standards:discard", args=[item.pk])).status_code == 403
        assert StandardUpload.objects.filter(pk=item.pk).exists()

    def test_an_outside_next_address_is_ignored(self, client, cats, admin):
        client.force_login(admin)
        item = upload(admin, b"category,preferred_position\nliability,Cap.\n", "x.csv")
        response = client.post(reverse("standards:discard", args=[item.pk]), {"next": "//evil.example/"})
        assert response.url.startswith("/accounts/settings/")
