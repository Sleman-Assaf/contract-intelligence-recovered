"""Notifications, de-duplication and lifecycle monitoring (§41, §42, §48)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractStatus, ContractType
from apps.notifications import services, tasks
from apps.notifications.models import Notification, NotificationKind

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(
        name="AJi - Saudi Arabia", code="aji-sa", country=Country.SAUDI_ARABIA
    )


def make_user(email, role, entity):
    return User.objects.create_user(
        email=email, password="test-password-1234", role=role, entity=entity
    )


@pytest.fixture
def reviewer(entity):
    return make_user("reviewer@aji.test", Role.CONTRACT_REVIEWER, entity)


@pytest.fixture
def contract(entity, reviewer):
    contract_type = ContractType.objects.create(code="design", name="Design")
    return Contract.objects.create(
        project_name="Riyadh Business District",
        contract_title="Design Consultancy Agreement",
        client_name="Ministry of Culture",
        country=Country.SAUDI_ARABIA, entity=entity,
        contract_type=contract_type, uploaded_by=reviewer,
        reviewer=reviewer,
        expiry_date=timezone.localdate() + timedelta(days=30),
    )


class TestDispatch:
    def test_creates_record_and_sends_email(self, reviewer, contract):
        mail.outbox.clear()
        created = services.notify(
            recipient=reviewer, kind=NotificationKind.ANALYSIS_COMPLETED,
            title="Analysis complete", body="3 high risk findings.",
            contract=contract,
        )
        assert created is not None
        assert Notification.objects.count() == 1
        assert len(mail.outbox) == 1
        assert created.emailed_at is not None

    def test_email_body_carries_no_contract_text(self, reviewer, contract):
        """Confidential wording must not leave the system by email (§45, §47)."""
        mail.outbox.clear()
        services.notify(
            recipient=reviewer, kind=NotificationKind.SOURCE_CONFLICT,
            title="Source conflict", body="2 regions disagree.", contract=contract,
        )
        body = mail.outbox[0].body
        assert "Open Sanad" in body
        assert contract.client_name not in body

    def test_inactive_user_is_not_notified(self, entity, contract):
        user = make_user("gone@aji.test", Role.CONTRACT_REVIEWER, entity)
        user.is_active = False
        user.save(update_fields=["is_active"])
        assert services.notify(
            recipient=user, kind=NotificationKind.ANALYSIS_COMPLETED, title="x"
        ) is None

    def test_notify_many_does_not_double_send(self, reviewer, contract):
        """uploaded_by and reviewer are often the same person."""
        created = services.notify_many(
            [reviewer, reviewer, None],
            kind=NotificationKind.ANALYSIS_COMPLETED, title="x", contract=contract,
        )
        assert len(created) == 1


class TestDeduplication:
    def test_same_event_is_sent_once(self, reviewer, contract):
        for _ in range(3):
            services.notify(
                recipient=reviewer, kind=NotificationKind.EXPIRY_APPROACHING,
                title="Expiring", contract=contract,
                dedupe_key=f"expiry:{contract.pk}:30",
            )
        assert Notification.objects.count() == 1

    def test_different_thresholds_are_separate_messages(self, reviewer, contract):
        for days in (90, 30, 7):
            services.notify(
                recipient=reviewer, kind=NotificationKind.EXPIRY_APPROACHING,
                title=f"{days} days", contract=contract,
                dedupe_key=f"expiry:{contract.pk}:{days}",
            )
        assert Notification.objects.count() == 3

    def test_blank_dedupe_key_never_collides(self, reviewer, contract):
        for _ in range(3):
            services.notify(
                recipient=reviewer, kind=NotificationKind.ANALYSIS_COMPLETED,
                title="x", contract=contract,
            )
        assert Notification.objects.count() == 3


class TestExpiryMonitoring:
    def test_warns_at_a_configured_threshold(self, settings, reviewer, contract):
        settings.EXPIRY_WARNING_DAYS = [90, 30, 7]
        result = tasks.check_expiring_contracts()
        assert result["notified"] == 1
        assert Notification.objects.filter(
            kind=NotificationKind.EXPIRY_APPROACHING
        ).exists()

    def test_thresholds_are_configuration(self, settings, reviewer, contract):
        """The same contract is ignored under a different warning window."""
        settings.EXPIRY_WARNING_DAYS = [7]
        assert tasks.check_expiring_contracts()["notified"] == 0

    def test_running_nightly_does_not_repeat_the_warning(self, settings, reviewer, contract):
        settings.EXPIRY_WARNING_DAYS = [30]
        tasks.check_expiring_contracts()
        tasks.check_expiring_contracts()
        tasks.check_expiring_contracts()
        assert Notification.objects.filter(
            kind=NotificationKind.EXPIRY_APPROACHING
        ).count() == 1

    def test_archived_contracts_are_left_alone(self, settings, reviewer, contract):
        settings.EXPIRY_WARNING_DAYS = [30]
        contract.status = ContractStatus.ARCHIVED
        contract.save(update_fields=["status"])
        assert tasks.check_expiring_contracts()["notified"] == 0

    def test_expired_contracts_are_flagged(self, reviewer, contract):
        contract.expiry_date = timezone.localdate() - timedelta(days=1)
        contract.save(update_fields=["expiry_date"])
        assert tasks.check_expired_contracts()["notified"] == 1


class TestStalledRecovery:
    def test_a_lost_job_is_recovered_not_left_forever(self, contract):
        """The Base44 failure mode: close the browser, contract stuck in analysis."""
        from apps.analysis.models import AnalysisRun, AnalysisStatus

        run = AnalysisRun.objects.create(
            contract=contract, version=1, status=AnalysisStatus.RUNNING,
            started_at=timezone.now() - timedelta(hours=3),
        )
        result = tasks.retry_stalled_analyses(max_age_minutes=60)

        run.refresh_from_db()
        contract.refresh_from_db()
        assert result["stalled"] == 1
        assert run.status == AnalysisStatus.FAILED
        assert contract.status == ContractStatus.ANALYSIS_FAILED
        assert Notification.objects.filter(
            kind=NotificationKind.ANALYSIS_FAILED
        ).exists()

    def test_a_running_job_is_left_alone(self, contract):
        from apps.analysis.models import AnalysisRun, AnalysisStatus

        AnalysisRun.objects.create(
            contract=contract, version=1, status=AnalysisStatus.RUNNING,
            started_at=timezone.now(),
        )
        assert tasks.retry_stalled_analyses(max_age_minutes=60)["stalled"] == 0


class TestRoleTargeting:
    def test_legal_review_request_reaches_legal_reviewers_only(self, entity, contract):
        legal = make_user("legal@aji.test", Role.LEGAL_REVIEWER, entity)
        make_user("mgmt@aji.test", Role.MANAGEMENT, entity)

        services.review_requested(
            contract, role="legal_reviewer",
            kind=NotificationKind.LEGAL_REVIEW_REQUESTED,
            title="Legal review requested",
        )
        recipients = set(
            Notification.objects.values_list("recipient__email", flat=True)
        )
        assert recipients == {legal.email}
