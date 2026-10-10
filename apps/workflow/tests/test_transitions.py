"""Server-enforced workflow transitions (§27, §30, §36, §48)."""
from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import translation

from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractStatus, ContractType
from apps.workflow import services as wf
from apps.workflow.models import Workflow, WorkflowStage, WorkflowTransition

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(
        name="AJi - Saudi Arabia", code="aji-sa", country=Country.SAUDI_ARABIA
    )


@pytest.fixture
def contract_type():
    return ContractType.objects.create(code="design", name="Design Consultancy")


def make_user(email, role, entity):
    return User.objects.create_user(
        email=email, password="test-password-1234", role=role, entity=entity
    )


@pytest.fixture
def uploader(entity):
    return make_user("uploader@aji.test", Role.CONTRACT_REVIEWER, entity)


@pytest.fixture
def contract(entity, contract_type, uploader):
    return Contract.objects.create(
        project_name="Riyadh Business District",
        contract_title="Design Consultancy Agreement",
        client_name="Ministry of Culture",
        country=Country.SAUDI_ARABIA,
        entity=entity,
        contract_type=contract_type,
        uploaded_by=uploader,
        status=ContractStatus.PENDING_CONTRACT_REVIEW,
    )


@pytest.fixture
def at_management(contract):
    workflow = wf.get_or_create_workflow(contract)
    workflow.current_stage = WorkflowStage.MANAGEMENT_REVIEW
    workflow.save(update_fields=["current_stage"])
    return contract


class TestTransitionValidity:
    def test_contract_review_can_send_to_legal(self, contract, uploader):
        record = wf.transition(contract=contract, user=uploader, action_key="send_to_legal")
        contract.refresh_from_db()
        assert contract.status == ContractStatus.LEGAL_REVIEW
        assert record.to_stage == WorkflowStage.LEGAL_REVIEW

    def test_illegal_transition_from_wrong_stage_is_refused(self, contract, entity):
        """Archive is only reachable from final approval."""
        management = make_user("mgmt@aji.test", Role.MANAGEMENT, entity)
        with translation.override("en"), pytest.raises(ValidationError, match="not available from stage"):
            wf.transition(contract=contract, user=management, action_key="archive")

    def test_unknown_action_is_refused(self, contract, uploader):
        with translation.override("en"), pytest.raises(ValidationError, match="Unknown workflow action"):
            wf.transition(contract=contract, user=uploader, action_key="delete_everything")

    def test_role_without_capability_is_refused(self, contract, entity):
        """A contract reviewer cannot make the legal decision."""
        wf.transition(contract=contract, user=contract.uploaded_by, action_key="send_to_legal")
        reviewer = make_user("rev2@aji.test", Role.CONTRACT_REVIEWER, entity)
        with pytest.raises(PermissionDenied):
            wf.transition(contract=contract, user=reviewer, action_key="legal_complete")


class TestSeparationOfDuties:
    def test_uploader_cannot_approve_own_contract(self, at_management, entity):
        """Even holding the Management role, the uploader is blocked."""
        uploader = at_management.uploaded_by
        uploader.role = Role.MANAGEMENT
        uploader.save(update_fields=["role"])

        with translation.override("en"), pytest.raises(PermissionDenied, match="Separation of duties"):
            wf.transition(contract=at_management, user=uploader, action_key="approve")

        at_management.refresh_from_db()
        assert at_management.status != ContractStatus.APPROVED

    def test_a_different_manager_can_approve(self, at_management, entity):
        approver = make_user("boss@aji.test", Role.MANAGEMENT, entity)
        wf.transition(contract=at_management, user=approver, action_key="approve")

        at_management.refresh_from_db()
        assert at_management.status == ContractStatus.APPROVED
        assert at_management.approved_at is not None

    def test_approve_is_not_offered_to_the_uploader(self, at_management):
        """The button is hidden too — but hiding it was never the protection."""
        uploader = at_management.uploaded_by
        uploader.role = Role.MANAGEMENT
        uploader.save(update_fields=["role"])
        keys = {a.key for a in wf.available_actions(uploader, at_management)}
        assert "approve" not in keys

    def test_approve_is_offered_to_another_manager(self, at_management, entity):
        approver = make_user("boss2@aji.test", Role.MANAGEMENT, entity)
        keys = {a.key for a in wf.available_actions(approver, at_management)}
        assert "approve" in keys


class TestAtomicityAndHistory:
    def test_stage_status_and_history_move_together(self, contract, uploader):
        """The Base44 version updated these as separate client calls."""
        wf.transition(contract=contract, user=uploader, action_key="send_to_legal",
                      note="Escalating liability wording")

        contract.refresh_from_db()
        workflow = Workflow.objects.get(contract=contract)
        record = WorkflowTransition.objects.get(contract=contract)

        assert contract.status == ContractStatus.LEGAL_REVIEW
        assert workflow.current_stage == WorkflowStage.LEGAL_REVIEW
        assert record.to_status == ContractStatus.LEGAL_REVIEW
        assert record.note == "Escalating liability wording"

    def test_failed_transition_leaves_no_trace(self, contract, entity):
        reviewer = make_user("rev3@aji.test", Role.CONTRACT_REVIEWER, entity)
        before = contract.status
        with pytest.raises(PermissionDenied):
            wf.transition(contract=contract, user=reviewer, action_key="approve")

        contract.refresh_from_db()
        assert contract.status == before
        assert WorkflowTransition.objects.filter(contract=contract).count() == 0

    def test_transitions_are_immutable(self, contract, uploader):
        record = wf.transition(contract=contract, user=uploader, action_key="send_to_legal")
        record.note = "rewritten"
        with pytest.raises(ValueError, match="immutable"):
            record.save()

    def test_actor_and_role_are_recorded(self, contract, uploader):
        record = wf.transition(contract=contract, user=uploader, action_key="send_to_legal")
        assert record.actor == uploader
        assert record.actor_role == Role.CONTRACT_REVIEWER


class TestAvailableActions:
    def test_actions_depend_on_the_current_stage(self, contract, uploader):
        keys = {a.key for a in wf.available_actions(uploader, contract)}
        assert keys == {"send_to_legal"}

    def test_plain_user_gets_nothing(self, contract, entity):
        outsider = make_user("plain@aji.test", Role.USER, entity)
        assert wf.available_actions(outsider, contract) == []


class TestMandatoryEscalation:
    def test_unresolved_mandatory_escalation_blocks_approval(self, at_management, entity):
        """§30: 'unlimited liability -> mandatory escalation' must actually block."""
        from apps.analysis.models import AnalysisRun, Finding
        from apps.standards.models import ClauseCategory
        from apps.workflow.models import ApprovalRule

        category = ClauseCategory.objects.create(code="liability", name="Liability")
        ApprovalRule.objects.create(
            name="Unlimited liability", category=category,
            is_mandatory_escalation=True,
        )
        run = AnalysisRun.objects.create(contract=at_management, version=1)
        Finding.objects.create(
            analysis=run, contract=at_management, category=category,
            risk_level="high", title="Unlimited liability",
        )
        at_management.current_analysis = run
        at_management.save(update_fields=["current_analysis"])

        approver = make_user("boss3@aji.test", Role.MANAGEMENT, entity)
        with translation.override("en"), pytest.raises(ValidationError, match="mandatory-escalation"):
            wf.transition(contract=at_management, user=approver, action_key="approve")

    def test_accepted_finding_unblocks_approval(self, at_management, entity):
        from apps.analysis.models import AnalysisRun, Finding, ReviewDecision
        from apps.standards.models import ClauseCategory
        from apps.workflow.models import ApprovalRule

        category = ClauseCategory.objects.create(code="liability", name="Liability")
        ApprovalRule.objects.create(
            name="Unlimited liability", category=category, is_mandatory_escalation=True,
        )
        run = AnalysisRun.objects.create(contract=at_management, version=1)
        finding = Finding.objects.create(
            analysis=run, contract=at_management, category=category,
            risk_level="high", title="Unlimited liability",
        )
        at_management.current_analysis = run
        at_management.save(update_fields=["current_analysis"])

        legal = make_user("legal@aji.test", Role.LEGAL_REVIEWER, entity)
        ReviewDecision.objects.create(
            finding=finding, contract=at_management, decision="accept",
            decided_by=legal, decided_by_role=legal.role,
        )

        approver = make_user("boss4@aji.test", Role.MANAGEMENT, entity)
        wf.transition(contract=at_management, user=approver, action_key="approve")
        at_management.refresh_from_db()
        assert at_management.status == ContractStatus.APPROVED


class TestComments:
    def test_comment_requires_access(self, contract, entity):
        outsider = make_user("nope@aji.test", Role.USER, entity)
        with pytest.raises(PermissionDenied):
            wf.add_comment(contract=contract, user=outsider, body="hello")

    def test_empty_comment_is_refused(self, contract, uploader):
        with pytest.raises(ValidationError):
            wf.add_comment(contract=contract, user=uploader, body="   ")

    def test_comment_is_stored_with_its_stage(self, contract, uploader):
        comment = wf.add_comment(contract=contract, user=uploader, body="Needs legal input")
        assert comment.author == uploader
        assert comment.stage == WorkflowStage.CONTRACT_REVIEW


class TestDegradedAnalysisBlocksApproval:
    """Approval may not rest on an analysis that never read the contract.

    A degraded run creates few findings, so every content-based guard passes
    vacuously — the mandatory-escalation check finds nothing blocking exactly
    because the clauses it cares about were never classified. Without this
    guard, a total AI outage is indistinguishable from a clean contract.
    """

    @pytest.fixture
    def degraded_run(self, contract):
        from apps.analysis.models import AnalysisRun, AnalysisStatus

        run = AnalysisRun.objects.create(
            contract=contract,
            version=1,
            status=AnalysisStatus.DEGRADED,
            confidence_breakdown={"gaps": ["7 clause(s) could not be categorised"]},
        )
        contract.current_analysis = run
        contract.save(update_fields=["current_analysis"])
        return run

    @pytest.fixture
    def clean_run(self, contract):
        from apps.analysis.models import AnalysisRun, AnalysisStatus

        run = AnalysisRun.objects.create(
            contract=contract, version=1, status=AnalysisStatus.SUCCEEDED
        )
        contract.current_analysis = run
        contract.save(update_fields=["current_analysis"])
        return run

    def test_approval_is_refused(self, at_management, entity, degraded_run):
        approver = make_user("manager@aji.test", Role.MANAGEMENT, entity)
        with translation.override("en"), pytest.raises(ValidationError) as exc:
            wf.transition(contract=at_management, user=approver, action_key="approve")
        assert "incomplete" in str(exc.value).lower()

    def test_the_reason_names_the_gap(self, at_management, entity, degraded_run):
        approver = make_user("manager2@aji.test", Role.MANAGEMENT, entity)
        with translation.override("en"), pytest.raises(ValidationError) as exc:
            wf.transition(contract=at_management, user=approver, action_key="approve")
        assert "could not be categorised" in str(exc.value)

    def test_the_gap_is_named_in_the_readers_language(self, at_management, entity, degraded_run):
        degraded_run.confidence_breakdown = {"gaps": ["7 clause(s) could not be categorised"],
                                             "unclassified_clauses": 7, "ai_failures": 9}
        degraded_run.save(update_fields=["confidence_breakdown"])
        approver = make_user("manager4@aji.test", Role.MANAGEMENT, entity)
        with translation.override("ar"), pytest.raises(ValidationError) as exc:
            wf.transition(contract=at_management, user=approver, action_key="approve")
        assert "تعذّر تصنيف 7 بند؛ فشل 2 طلب للذكاء الاصطناعي" in exc.value.messages[0].replace("; ", "؛ ")
        assert "could not be categorised" not in exc.value.messages[0]

    def test_a_complete_analysis_is_not_blocked(self, at_management, entity, clean_run):
        approver = make_user("manager3@aji.test", Role.MANAGEMENT, entity)
        wf.transition(contract=at_management, user=approver, action_key="approve")
        at_management.refresh_from_db()
        assert at_management.status == ContractStatus.APPROVED

    def test_a_contract_with_no_analysis_is_unaffected(self, at_management, entity):
        """This guard is about degraded analyses only; other rules cover the
        case of no analysis at all."""
        approver = make_user("manager4@aji.test", Role.MANAGEMENT, entity)
        wf.transition(contract=at_management, user=approver, action_key="approve")
        at_management.refresh_from_db()
        assert at_management.status == ContractStatus.APPROVED
