"""Standards resolution (§14, §15, §33, §48)."""
from __future__ import annotations

from datetime import date

import pytest

from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractType
from apps.standards.models import (
    ClauseCategory,
    Standard,
    StandardScope,
    StandardStatus,
    StandardVersion,
)
from apps.standards.resolution import resolve_for_category, resolve_for_contract

pytestmark = pytest.mark.django_db


@pytest.fixture
def liability():
    return ClauseCategory.objects.create(code="liability", name="Liability")


@pytest.fixture
def design_consultancy():
    return ContractType.objects.create(code="design", name="Design Consultancy")


@pytest.fixture
def entity():
    return Entity.objects.create(
        name="AJi - Saudi Arabia", code="aji-sa", country=Country.SAUDI_ARABIA
    )


def make_standard(category, scope, *, country="", contract_type=None,
                  position="", version=1, status=StandardStatus.APPROVED,
                  effective=date(2020, 1, 1), expiry=None, impact=None):
    standard, _ = Standard.objects.get_or_create(
        category=category, scope=scope, country=country,
        contract_type=contract_type, reference_template="aji_standard",
        defaults={"title": f"{category.code} {scope}"},
    )
    StandardVersion.objects.create(
        standard=standard, version=version, preferred_position=position,
        status=status, effective_date=effective, expiry_date=expiry, impact=impact,
    )
    return standard


class TestPrecedence:
    def test_global_used_when_nothing_more_specific(self, liability):
        make_standard(liability, StandardScope.GLOBAL, position="Global cap")
        resolved = resolve_for_category(
            liability, country=Country.SAUDI_ARABIA, contract_type_id=None
        )
        assert resolved is not None
        assert resolved.scope == StandardScope.GLOBAL
        assert resolved.version.preferred_position == "Global cap"

    def test_country_override_beats_global(self, liability):
        make_standard(liability, StandardScope.GLOBAL, position="Global cap")
        make_standard(liability, StandardScope.COUNTRY,
                      country=Country.SAUDI_ARABIA, position="KSA cap")
        resolved = resolve_for_category(
            liability, country=Country.SAUDI_ARABIA, contract_type_id=None
        )
        assert resolved.scope == StandardScope.COUNTRY
        assert resolved.version.preferred_position == "KSA cap"

    def test_contract_type_override_beats_country(self, liability, design_consultancy):
        make_standard(liability, StandardScope.GLOBAL, position="Global cap")
        make_standard(liability, StandardScope.COUNTRY,
                      country=Country.SAUDI_ARABIA, position="KSA cap")
        make_standard(liability, StandardScope.CONTRACT_TYPE,
                      contract_type=design_consultancy, position="Design cap")
        resolved = resolve_for_category(
            liability, country=Country.SAUDI_ARABIA,
            contract_type_id=design_consultancy.pk,
        )
        assert resolved.scope == StandardScope.CONTRACT_TYPE
        assert resolved.version.preferred_position == "Design cap"

    def test_another_countrys_override_is_not_used(self, liability):
        """A Jordanian standard must never be applied to a Saudi contract."""
        make_standard(liability, StandardScope.GLOBAL, position="Global cap")
        make_standard(liability, StandardScope.COUNTRY,
                      country=Country.JORDAN, position="Jordan cap")
        resolved = resolve_for_category(
            liability, country=Country.SAUDI_ARABIA, contract_type_id=None
        )
        assert resolved.scope == StandardScope.GLOBAL

    def test_resolution_path_is_recorded(self, liability):
        make_standard(liability, StandardScope.GLOBAL, position="Global cap")
        resolved = resolve_for_category(
            liability, country=Country.JORDAN, contract_type_id=None
        )
        assert resolved.resolution_path            # evidence of why, not just what


class TestApprovalAndTime:
    def test_draft_standard_is_never_used(self, liability):
        make_standard(liability, StandardScope.GLOBAL,
                      position="Draft", status=StandardStatus.DRAFT)
        assert resolve_for_category(
            liability, country=Country.JORDAN, contract_type_id=None
        ) is None

    def test_expired_standard_is_not_used(self, liability):
        make_standard(liability, StandardScope.GLOBAL, position="Old",
                      effective=date(2019, 1, 1), expiry=date(2020, 1, 1))
        assert resolve_for_category(
            liability, country=Country.JORDAN, contract_type_id=None,
            as_of=date(2026, 1, 1),
        ) is None

    def test_future_standard_is_not_used_yet(self, liability):
        make_standard(liability, StandardScope.GLOBAL, position="Future",
                      effective=date(2030, 1, 1))
        assert resolve_for_category(
            liability, country=Country.JORDAN, contract_type_id=None,
            as_of=date(2026, 1, 1),
        ) is None

    def test_version_in_force_on_the_date_is_chosen(self, liability):
        """A contract analysed under v3 must keep resolving to v3 (§33)."""
        standard = make_standard(liability, StandardScope.GLOBAL, position="v1",
                                 version=1, effective=date(2020, 1, 1))
        StandardVersion.objects.create(
            standard=standard, version=2, preferred_position="v2",
            status=StandardStatus.APPROVED, effective_date=date(2026, 6, 1),
        )
        before = resolve_for_category(liability, country=Country.JORDAN,
                                      contract_type_id=None, as_of=date(2026, 1, 1))
        after = resolve_for_category(liability, country=Country.JORDAN,
                                     contract_type_id=None, as_of=date(2026, 9, 1))
        assert before.version.preferred_position == "v1"
        assert after.version.preferred_position == "v2"

    def test_falls_back_when_specific_override_has_no_approved_version(
        self, liability, design_consultancy
    ):
        """An unapproved override must not leave the category unresolved."""
        make_standard(liability, StandardScope.GLOBAL, position="Global cap")
        make_standard(liability, StandardScope.CONTRACT_TYPE,
                      contract_type=design_consultancy, position="Draft design cap",
                      status=StandardStatus.DRAFT)
        resolved = resolve_for_category(
            liability, country=Country.SAUDI_ARABIA,
            contract_type_id=design_consultancy.pk,
        )
        assert resolved.scope == StandardScope.GLOBAL


class TestContractResolution:
    @pytest.fixture
    def contract(self, entity, design_consultancy):
        user = User.objects.create_user(
            email="u@aji.test", password="test-password-1234",
            role=Role.CONTRACT_REVIEWER, entity=entity,
        )
        return Contract.objects.create(
            project_name="P", contract_title="T", client_name="C",
            country=Country.SAUDI_ARABIA, entity=entity,
            contract_type=design_consultancy, uploaded_by=user,
            effective_date=date(2026, 9, 1),
        )

    def test_unresolved_categories_are_reported_not_invented(self, contract, liability):
        """NO_STANDARD_FOUND is a real outcome (§54)."""
        ClauseCategory.objects.create(code="insurance", name="Insurance")
        make_standard(liability, StandardScope.GLOBAL, position="Global cap")

        result = resolve_for_contract(contract)
        assert "liability" in result.resolved
        assert "insurance" in result.unresolved_categories

    def test_snapshot_freezes_what_applied(self, contract, liability):
        """Without this a finding cannot be explained months later (§32)."""
        make_standard(liability, StandardScope.COUNTRY,
                      country=Country.SAUDI_ARABIA, position="KSA cap", version=3)
        snapshot = resolve_for_contract(contract).snapshot

        assert snapshot["liability"]["version"] == 3
        assert snapshot["liability"]["scope"] == StandardScope.COUNTRY
        assert "resolution_path" in snapshot["liability"]

    def test_demo_fixtures_are_flagged(self, contract, liability):
        """Test data must never be presented as approved AJi policy (§49)."""
        standard = Standard.objects.create(
            category=liability, scope=StandardScope.GLOBAL, title="demo",
        )
        StandardVersion.objects.create(
            standard=standard, version=1, preferred_position="DEMO",
            status=StandardStatus.APPROVED, effective_date=date(2020, 1, 1),
            is_demo_fixture=True,
        )
        result = resolve_for_contract(contract)
        assert result.uses_demo_fixtures is True
        assert result.snapshot["liability"]["is_demo_fixture"] is True


class TestImmutability:
    def test_approved_version_cannot_be_edited(self, liability):
        """Legal changing a position must create a new version (§33)."""
        make_standard(liability, StandardScope.GLOBAL, position="Original")
        version = StandardVersion.objects.get(standard__category=liability)

        version.preferred_position = "Silently changed"
        with pytest.raises(ValueError, match="immutable"):
            version.save()

    def test_draft_version_can_still_be_edited(self, liability):
        make_standard(liability, StandardScope.GLOBAL, position="Draft",
                      status=StandardStatus.DRAFT)
        version = StandardVersion.objects.get(standard__category=liability)
        version.preferred_position = "Revised draft"
        version.save()
        version.refresh_from_db()
        assert version.preferred_position == "Revised draft"


class TestAjisSide:
    """A standard written for AJi's side beats one for every side; a standard
    for another side never applies."""

    def _standard(self, category, perspective, position):
        standard = Standard.objects.create(category=category, scope=StandardScope.GLOBAL,
                                           title=f"{category.code} {perspective or 'any'}",
                                           perspective=perspective)
        StandardVersion.objects.create(standard=standard, version=1, status=StandardStatus.APPROVED,
                                       effective_date=date(2024, 1, 1), preferred_position=position)
        return standard

    def test_the_side_specific_standard_wins(self, liability):
        self._standard(liability, "", "cap at fee")
        self._standard(liability, "client", "sub-consultant liability at least the fee")
        resolved = resolve_for_category(liability, country="JO", contract_type_id=None,
                                        perspective="client", as_of=date(2026, 1, 1), fallback=False)
        assert resolved.version.preferred_position == "sub-consultant liability at least the fee"
        assert "client" in resolved.resolution_path[-1]

    def test_without_a_side_specific_one_the_general_applies(self, liability):
        self._standard(liability, "", "cap at fee")
        resolved = resolve_for_category(liability, country="JO", contract_type_id=None,
                                        perspective="jv_partner", as_of=date(2026, 1, 1), fallback=False)
        assert resolved.version.preferred_position == "cap at fee"

    def test_a_standard_for_another_side_never_applies(self, liability):
        self._standard(liability, "client", "hiring-party position")
        assert resolve_for_category(liability, country="JO", contract_type_id=None,
                                    perspective="consultant", as_of=date(2026, 1, 1), fallback=False) is None
