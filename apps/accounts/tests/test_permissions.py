"""RBAC and separation of duties (§34, §48).

These are the checks that the previous Base44 system did not have, so they are
the first ones written.
"""
from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import translation

from apps.accounts import permissions as perms
from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractType

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(
        name="AJi - Saudi Arabia", code="aji-sa", country=Country.SAUDI_ARABIA
    )


@pytest.fixture
def contract_type():
    return ContractType.objects.create(code="arch", name="Architectural Design")


def make_user(email: str, role: str, entity=None) -> User:
    return User.objects.create_user(
        email=email, password="test-password-1234", role=role, entity=entity
    )


@pytest.fixture
def contract(entity, contract_type):
    uploader = make_user("uploader@aji.test", Role.CONTRACT_REVIEWER, entity)
    return Contract.objects.create(
        project_name="Riyadh Business District",
        contract_title="Design Consultancy Agreement",
        client_name="Ministry of Culture",
        country=Country.SAUDI_ARABIA,
        entity=entity,
        contract_type=contract_type,
        uploaded_by=uploader,
    )


class TestCapabilities:
    def test_anonymous_has_nothing(self):
        class Anon:
            is_authenticated = False

        assert perms.capabilities_for(Anon()) == set()

    def test_inactive_user_has_nothing(self, entity):
        user = make_user("inactive@aji.test", Role.MANAGEMENT, entity)
        user.is_active = False
        assert perms.capabilities_for(user) == set()

    def test_admin_cannot_give_final_approval(self, entity):
        """Administering the system is not commercial authority."""
        admin = make_user("admin@aji.test", Role.ADMIN, entity)
        assert perms.has_capability(admin, perms.MANAGE_USERS)
        assert not perms.has_capability(admin, perms.FINAL_APPROVAL)

    def test_contract_reviewer_cannot_make_legal_decisions(self, entity):
        reviewer = make_user("rev@aji.test", Role.CONTRACT_REVIEWER, entity)
        assert perms.has_capability(reviewer, perms.DECIDE_FINDING)
        assert not perms.has_capability(reviewer, perms.LEGAL_DECISION)

    def test_management_can_approve_but_not_manage_standards(self, entity):
        mgmt = make_user("mgmt@aji.test", Role.MANAGEMENT, entity)
        assert perms.has_capability(mgmt, perms.FINAL_APPROVAL)
        assert not perms.has_capability(mgmt, perms.MANAGE_STANDARDS)

    def test_require_capability_raises(self, entity):
        user = make_user("plain@aji.test", Role.USER, entity)
        with pytest.raises(PermissionDenied):
            perms.require_capability(user, perms.FINAL_APPROVAL)


class TestSeparationOfDuties:
    """The single largest governance gap in the system being replaced."""

    def test_uploader_cannot_approve_own_contract(self, contract, entity):
        uploader = contract.uploaded_by
        uploader.role = Role.MANAGEMENT           # even with the authority
        uploader.save(update_fields=["role"])

        assert perms.has_capability(uploader, perms.FINAL_APPROVAL)
        assert not perms.can_give_final_approval(uploader, contract)

        with translation.override("en"), pytest.raises(PermissionDenied, match="Separation of duties"):
            perms.require_final_approval_rights(uploader, contract)

    def test_different_manager_can_approve(self, contract, entity):
        other = make_user("boss@aji.test", Role.MANAGEMENT, entity)
        assert perms.can_give_final_approval(other, contract)
        perms.require_final_approval_rights(other, contract)  # does not raise

    def test_reviewer_role_cannot_approve_at_all(self, contract, entity):
        reviewer = make_user("rev2@aji.test", Role.CONTRACT_REVIEWER, entity)
        assert not perms.can_give_final_approval(reviewer, contract)


class TestRowScoping:
    def test_plain_user_sees_only_own_contracts(self, contract, entity):
        outsider = make_user("outsider@aji.test", Role.USER, entity)
        scoped = perms.scope_contracts(outsider, Contract.objects.all())
        assert scoped.count() == 0
        assert not perms.can_view_contract(outsider, contract)

    def test_uploader_sees_own_contract(self, contract):
        uploader = contract.uploaded_by
        uploader.role = Role.USER
        uploader.save(update_fields=["role"])
        scoped = perms.scope_contracts(uploader, Contract.objects.all())
        assert scoped.count() == 1

    def test_assigned_reviewer_sees_contract(self, contract, entity):
        assignee = make_user("assigned@aji.test", Role.USER, entity)
        contract.reviewer = assignee
        contract.save(update_fields=["reviewer"])
        assert perms.scope_contracts(assignee, Contract.objects.all()).count() == 1

    def test_legal_reviewer_sees_all(self, contract, entity):
        legal = make_user("legal@aji.test", Role.LEGAL_REVIEWER, entity)
        assert perms.scope_contracts(legal, Contract.objects.all()).count() == 1

    def test_anonymous_sees_nothing(self, contract):
        class Anon:
            is_authenticated = False

        assert perms.scope_contracts(Anon(), Contract.objects.all()).count() == 0

    def test_require_contract_access_raises_for_outsider(self, contract, entity):
        outsider = make_user("nope@aji.test", Role.USER, entity)
        with pytest.raises(PermissionDenied):
            perms.require_contract_access(outsider, contract)
