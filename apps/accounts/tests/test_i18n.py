"""Arabic actually reaching the page.

Both halves of this were broken and neither showed up as an error: there was no
compiled catalogue, and nothing activated the reader's language, so an Arabic
UI rendered every model label in English. The failure mode is silent -- gettext
falls back to the msgid -- so it needs tests rather than a glance.
"""
from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import translation

from apps.accounts.models import Entity, User
from apps.contracts.models import ContractType

pytestmark = pytest.mark.django_db


class TestCatalogue:
    """The compiled .mo must be present and loadable."""

    @pytest.mark.parametrize("msgid,expected", [
        ("Needs Human Review", "يحتاج مراجعة بشرية"),
        ("Approved", "معتمد"),
        ("High", "مرتفعة"),
        ("Non-compliant", "غير مطابق"),
        ("Completed with gaps", "اكتمل مع نواقص"),
        ("Contract Reviewer", "مراجع العقود"),
        ("Saudi Arabia", "السعودية"),
    ])
    def test_model_labels_are_translated(self, msgid, expected):
        with translation.override("ar"):
            assert translation.gettext(msgid) == expected

    def test_operator_diagnostics_are_translated(self):
        """Status-page messages are read by whoever fixes the configuration,
        and that person reads the same Arabic UI as everyone else."""
        with translation.override("ar"):
            assert translation.gettext(
                "AJi Brain is not configured. Missing: %(missing)s."
            ) != "AJi Brain is not configured. Missing: %(missing)s."

    def test_english_is_left_alone(self):
        with translation.override("en"):
            assert translation.gettext("Approved") == "Approved"


class TestPoToMoCompiler:
    """`compilemessages_py` replaces GNU msgfmt, which cannot be installed here."""

    def test_round_trips_a_catalogue(self, tmp_path):
        from apps.accounts.management.commands.compilemessages_py import (
            parse_po,
            write_mo,
        )
        import gettext as gettext_module

        po = tmp_path / "django.po"
        po.write_text(
            'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
            'msgid "Approved"\nmsgstr "معتمد"\n\n'
            'msgid "High"\nmsgstr "مرتفعة"\n',
            encoding="utf-8",
        )
        catalog = parse_po(po)
        assert catalog["Approved"] == "معتمد"

        mo = tmp_path / "django.mo"
        write_mo(catalog, mo)
        with mo.open("rb") as fh:
            loaded = gettext_module.GNUTranslations(fh)
        assert loaded.gettext("Approved") == "معتمد"
        assert loaded.gettext("High") == "مرتفعة"

    def test_untranslated_entries_are_skipped(self, tmp_path):
        """An empty msgstr must not enter the catalogue: gettext would return
        the empty string and the label would vanish from the page."""
        from apps.accounts.management.commands.compilemessages_py import parse_po

        po = tmp_path / "django.po"
        po.write_text(
            'msgid "Approved"\nmsgstr "معتمد"\n\n'
            'msgid "Rejected"\nmsgstr ""\n',
            encoding="utf-8",
        )
        catalog = parse_po(po)
        assert "Approved" in catalog
        assert "Rejected" not in catalog

    def test_wrapped_entries_are_joined(self, tmp_path):
        from apps.accounts.management.commands.compilemessages_py import parse_po

        po = tmp_path / "django.po"
        po.write_text(
            'msgid ""\n"a long "\n"message"\nmsgstr ""\n"رسالة "\n"طويلة"\n',
            encoding="utf-8",
        )
        assert parse_po(po)["a long message"] == "رسالة طويلة"


class TestLanguageMiddleware:
    """The stored preference wins over the browser's Accept-Language."""

    @pytest.fixture
    def arabic_user(self):
        return User.objects.create_user(
            email="ar@aji.test", password="test-password-1234",
            role="user", preferred_language="ar",
        )

    @pytest.fixture
    def english_user(self):
        return User.objects.create_user(
            email="en@aji.test", password="test-password-1234",
            role="user", preferred_language="en",
        )

    def test_arabic_preference_beats_an_english_browser(self, client, arabic_user):
        client.force_login(arabic_user)
        response = client.get(reverse("contracts:dashboard"),
                              HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")
        body = response.content.decode()
        assert "يحتاج مراجعة بشرية" in body or "لوحة التحكم" in body
        assert response["Content-Language"] == "ar"

    def test_english_preference_is_honoured_too(self, client, english_user):
        client.force_login(english_user)
        response = client.get(reverse("contracts:dashboard"),
                              HTTP_ACCEPT_LANGUAGE="ar")
        assert response["Content-Language"] == "en"

    def test_an_unsupported_preference_falls_back(self, client, arabic_user, settings):
        arabic_user.preferred_language = "fr"
        arabic_user.save(update_fields=["preferred_language"])
        client.force_login(arabic_user)
        response = client.get(reverse("contracts:dashboard"))
        assert response["Content-Language"] == settings.LANGUAGE_CODE

    def test_the_language_does_not_leak_to_the_next_request(self, client, english_user):
        """Workers are reused; a language left active would bleed across."""
        before = translation.get_language()
        client.force_login(english_user)
        client.get(reverse("contracts:dashboard"))
        assert translation.get_language() == before


class TestBilingualReferenceData:
    """Contract types and clause categories are stored in both languages."""

    @pytest.fixture
    def contract_type(self):
        return ContractType.objects.create(
            code="arch", name="Architectural Design Consultancy",
            name_ar="استشارات تصميم معماري",
        )

    def test_arabic_name_is_used_on_an_arabic_page(self, contract_type):
        with translation.override("ar"):
            assert contract_type.display_name == "استشارات تصميم معماري"

    def test_english_name_is_used_on_an_english_page(self, contract_type):
        with translation.override("en"):
            assert contract_type.display_name == "Architectural Design Consultancy"

    def test_a_missing_arabic_name_falls_back(self):
        """A blank label is worse than one in the wrong language."""
        entity = Entity.objects.create(name="Limana", name_ar="", code="limana", country="SA")
        with translation.override("ar"):
            assert entity.display_name == "Limana"
