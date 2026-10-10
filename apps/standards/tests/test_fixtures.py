"""Development fixtures and the standards importer (§49, §33, §48)."""
from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import ContractType
from apps.standards.models import (
    ClauseCategory,
    Standard,
    StandardScope,
    StandardStatus,
    StandardVersion,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def reference():
    call_command("seed_reference", verbosity=0)


class TestReferenceData:
    def test_creates_entities_types_and_categories(self, reference):
        assert Entity.objects.count() == 8
        assert ContractType.objects.count() == 6
        assert ClauseCategory.objects.count() == 22

    def test_entities_map_to_four_jurisdictions(self, reference):
        """Standards resolve on jurisdiction; several entities share one."""
        jurisdictions = set(Entity.objects.values_list("country", flat=True))
        assert jurisdictions == {
            Country.SAUDI_ARABIA, Country.JORDAN, Country.OMAN, Country.UAE
        }

    def test_is_idempotent(self, reference):
        call_command("seed_reference", verbosity=0)
        call_command("seed_reference", verbosity=0)
        assert Entity.objects.count() == 8
        assert ClauseCategory.objects.count() == 22


class TestDemoStandards:
    def test_refuses_to_load_outside_debug(self, settings, reference):
        settings.DEBUG = False
        with pytest.raises(CommandError, match="Refusing to load demo standards"):
            call_command("seed_demo_standards", verbosity=0)

    def test_requires_reference_data_first(self, settings):
        settings.DEBUG = True
        with pytest.raises(CommandError, match="Run `manage.py seed_reference` first"):
            call_command("seed_demo_standards", verbosity=0)

    def test_every_version_is_flagged_as_test_data(self, settings, reference):
        """The whole point of §49: nothing here may look like approved policy."""
        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)

        versions = StandardVersion.objects.all()
        assert versions.exists()
        assert all(v.is_demo_fixture for v in versions)
        assert all("DEMO FIXTURE" in v.source_reference for v in versions)

    def test_loads_global_positions_and_country_overrides(self, settings, reference):
        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)

        assert Standard.objects.filter(scope=StandardScope.GLOBAL).count() == 12
        assert Standard.objects.filter(scope=StandardScope.COUNTRY).exists()

    def test_override_only_where_jurisdictions_actually_differ(self, settings, reference):
        """Most positions are jurisdiction-neutral; only a few need an override."""
        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)

        overrides = Standard.objects.filter(scope=StandardScope.COUNTRY).count()
        globals_ = Standard.objects.filter(scope=StandardScope.GLOBAL).count()
        assert overrides < globals_

    def test_sharia_aware_late_payment_override(self, settings, reference):
        """Interest is unenforceable in KSA; Jordan permits it."""
        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)

        ksa = StandardVersion.objects.get(
            standard__category__code="late_payment", standard__country="SA"
        )
        jordan = StandardVersion.objects.get(
            standard__category__code="late_payment", standard__country="JO"
        )
        assert "not interest" in ksa.preferred_position
        assert "Interest" in jordan.preferred_position

    def test_rules_are_machine_checkable(self, settings, reference):
        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)

        delay = StandardVersion.objects.get(standard__category__code="delayLiq")
        assert delay.rule_definition["operator"] == "lte"
        assert delay.rule_definition["value"] == 10

    def test_the_seeded_rule_actually_runs(self, settings, reference):
        """A fixture that cannot be evaluated would be useless for testing.

        The three bands come straight from how the position is written: at most
        10% preferred, up to 15% with Legal's approval, uncapped is a red flag.
        """
        from apps.analysis import rules
        from apps.analysis.models import ComplianceResult

        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)

        delay = StandardVersion.objects.get(standard__category__code="delayLiq")
        bands = [
            ("damages capped at 8%", ComplianceResult.COMPLIANT),
            ("damages capped at 15%", ComplianceResult.PARTIAL),
            ("damages capped at 25%", ComplianceResult.NON_COMPLIANT),
            ("no cap on damages", ComplianceResult.UNKNOWN),
        ]
        for clause, expected in bands:
            assert rules.evaluate(delay.rule_definition, clause).result == expected, clause

    def test_loading_twice_does_not_duplicate(self, settings, reference):
        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)
        before = StandardVersion.objects.count()
        call_command("seed_demo_standards", verbosity=0)
        assert StandardVersion.objects.count() == before

    def test_can_be_removed_cleanly(self, settings, reference):
        settings.DEBUG = True
        call_command("seed_demo_standards", verbosity=0)
        assert StandardVersion.objects.filter(is_demo_fixture=True).exists()

        call_command("seed_demo_standards", "--remove", verbosity=0)
        assert not StandardVersion.objects.filter(is_demo_fixture=True).exists()


class TestStandardsImport:
    def _write(self, tmp_path, rows, name="positions.json"):
        path = tmp_path / name
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return path

    def test_imports_as_draft_not_approved(self, reference, tmp_path):
        """Loading a file is not Legal approving its contents."""
        path = self._write(tmp_path, [{
            "category": "delayLiq",
            "preferred_position": "Cap at 8% of contract value.",
            "impact": 5,
        }])
        call_command("import_standards", str(path), verbosity=0)

        version = StandardVersion.objects.get(is_demo_fixture=False)
        assert version.status == StandardStatus.DRAFT
        assert version.impact == 5

    def test_a_draft_import_is_not_resolved(self, reference, tmp_path):
        """Nothing imported becomes active until someone approves it."""
        from apps.standards.resolution import resolve_for_category

        path = self._write(tmp_path, [{
            "category": "delayLiq", "preferred_position": "Cap at 8%.",
        }])
        call_command("import_standards", str(path), verbosity=0)

        assert resolve_for_category(
            ClauseCategory.objects.get(code="delayLiq"),
            country=Country.SAUDI_ARABIA, contract_type_id=None,
        ) is None

    def test_import_creates_a_new_version_not_an_overwrite(self, reference, tmp_path):
        path = self._write(tmp_path, [{
            "category": "delayLiq", "preferred_position": "Cap at 8%.",
        }])
        call_command("import_standards", str(path), verbosity=0)
        call_command("import_standards", str(path), verbosity=0)

        versions = StandardVersion.objects.filter(
            standard__category__code="delayLiq"
        ).order_by("version")
        assert [v.version for v in versions] == [1, 2]

    def test_unknown_category_aborts_the_whole_import(self, reference, tmp_path):
        path = self._write(tmp_path, [
            {"category": "delayLiq", "preferred_position": "ok"},
            {"category": "not_a_category", "preferred_position": "x"},
        ])
        with pytest.raises(CommandError, match="failed validation"):
            call_command("import_standards", str(path), verbosity=0)
        assert StandardVersion.objects.count() == 0      # all-or-nothing

    def test_impact_outside_one_to_five_is_rejected(self, reference, tmp_path):
        path = self._write(tmp_path, [{
            "category": "delayLiq", "preferred_position": "x", "impact": 9,
        }])
        with pytest.raises(CommandError):
            call_command("import_standards", str(path), verbosity=0)

    def test_country_row_becomes_a_country_override(self, reference, tmp_path):
        path = self._write(tmp_path, [{
            "category": "insurance", "country": "JO",
            "preferred_position": "JOD 7,000,000 minimum.",
        }])
        call_command("import_standards", str(path), verbosity=0)
        standard = Standard.objects.get(category__code="insurance")
        assert standard.scope == StandardScope.COUNTRY
        assert standard.country == "JO"

    def test_dry_run_writes_nothing(self, reference, tmp_path):
        path = self._write(tmp_path, [{
            "category": "delayLiq", "preferred_position": "Cap at 8%.",
        }])
        call_command("import_standards", str(path), "--dry-run", verbosity=0)
        assert StandardVersion.objects.count() == 0

    def test_csv_from_the_legal_template_loads(self, reference, tmp_path):
        """The spreadsheet sent to country legal teams, saved as CSV."""
        path = tmp_path / "ksa.csv"
        path.write_text(
            "category,country,preferred_position,acceptable_fallback,walk_away,impact_1_5\n"
            "delayLiq,SA,Cap at 10%,Up to 15% with Legal approval,No cap,5\n",
            encoding="utf-8",
        )
        call_command("import_standards", str(path), verbosity=0)

        version = StandardVersion.objects.get(standard__category__code="delayLiq")
        assert version.preferred_position == "Cap at 10%"
        assert version.red_flags == "No cap"
        assert version.impact == 5


class TestDemoUsers:
    def test_refuses_outside_debug(self, settings, reference):
        settings.DEBUG = False
        with pytest.raises(CommandError, match="Refusing to create demo users"):
            call_command("seed_demo_users", verbosity=0)

    def test_creates_one_user_per_role_plus_a_second_reviewer(self, settings, reference):
        settings.DEBUG = True
        call_command("seed_demo_users", verbosity=0)

        assert User.objects.filter(email__endswith="@aji.demo").count() == 7
        # Two reviewers, so separation of duties can actually be walked through.
        assert User.objects.filter(role=Role.CONTRACT_REVIEWER).count() == 2

    def test_every_demo_account_is_named(self, settings, reference):
        """The name is what the site shows now, so an unnamed demo account
        would appear as a bare address on every screen it touches."""
        settings.DEBUG = True
        call_command("seed_demo_users", verbosity=0)

        for account in User.objects.filter(email__endswith="@aji.demo"):
            assert account.username, f"{account.email} has no display name"
            assert account.display_name == account.username

    def test_the_name_says_DEMO_on_every_screen(self, settings, reference):
        """The prefix is a safety marker, and a marker in a field nothing
        renders protects nobody."""
        settings.DEBUG = True
        call_command("seed_demo_users", verbosity=0)

        for account in User.objects.filter(email__endswith="@aji.demo"):
            assert account.display_name.startswith("DEMO")

    def test_re_running_names_an_account_seeded_before_the_field_existed(
            self, settings, reference):
        """Installs already carrying these accounts are repaired by running
        the command again, rather than by hand."""
        settings.DEBUG = True
        call_command("seed_demo_users", verbosity=0)

        account = User.objects.get(email="admin@aji.demo")
        account.username = ""
        account.save(update_fields=["username"])

        call_command("seed_demo_users", verbosity=0)
        account.refresh_from_db()
        assert account.username == "DEMO Admin"

    def test_re_running_never_overwrites_a_chosen_name(self, settings, reference):
        """Only a blank is filled. Somebody who renamed an account keeps it."""
        settings.DEBUG = True
        call_command("seed_demo_users", verbosity=0)

        account = User.objects.get(email="admin@aji.demo")
        account.username = "أبو محمد"
        account.save(update_fields=["username"])

        call_command("seed_demo_users", verbosity=0)
        account.refresh_from_db()
        assert account.username == "أبو محمد"

    def test_demo_admin_still_cannot_approve_a_contract(self, settings, reference):
        """Superuser status is infrastructure, not commercial authority."""
        from apps.accounts import permissions as perms

        settings.DEBUG = True
        call_command("seed_demo_users", verbosity=0)

        admin = User.objects.get(email="admin@aji.demo")
        assert admin.is_superuser is True
        assert not perms.has_capability(admin, perms.FINAL_APPROVAL)
