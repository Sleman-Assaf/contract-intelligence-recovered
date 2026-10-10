"""Fixes from the pre-launch audit: who may do what, where a redirect may go,
how many passwords may be guessed, and settings that refuse to run unsafe."""
from __future__ import annotations

from unittest import mock

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractType

pytestmark = pytest.mark.django_db
PASSWORD = "a-long-test-password-1234"


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi - Jordan", code="aji-jo", country=Country.JORDAN)


def person(entity, role, email):
    return User.objects.create_user(email=email, password=PASSWORD, role=role, entity=entity)


@pytest.fixture
def contract(entity):
    reviewer = person(entity, Role.CONTRACT_REVIEWER, "rev@aji.test")
    kind = ContractType.objects.create(code="design", name="Design")
    return Contract.objects.create(project_name="P", contract_title="T", client_name="C", country=Country.JORDAN,
                                   entity=entity, contract_type=kind, uploaded_by=reviewer)


@pytest.mark.parametrize("role", [Role.MANAGEMENT, Role.LEGAL_REVIEWER, Role.STANDARD_MANAGER])
def test_only_uploaders_may_upload_a_negotiation_round(client, entity, contract, role):
    client.force_login(person(entity, role, f"{role}@aji.test"))
    with mock.patch("apps.contracts.services.start_new_round") as started:
        response = client.post(reverse("contracts:new_round", args=[contract.pk]),
                               {"file": SimpleUploadedFile("r2.pdf", b"%PDF-1.4")})
    assert response.status_code == 403
    started.assert_not_called()


def test_an_approval_never_redirects_off_site(client, entity):
    from apps.standards.models import ClauseCategory, Standard, StandardVersion

    manager = person(entity, Role.STANDARD_MANAGER, "sm@aji.test")
    category = ClauseCategory.objects.create(code="liability", name="Liability")
    standard = Standard.objects.create(category=category, scope="global", title="Cap")
    version = StandardVersion.objects.create(standard=standard, version=1)
    client.force_login(manager)
    with mock.patch("apps.standards.normalization.approve"):
        response = client.post(reverse("standards:approve", args=[version.pk]),
                               {"next": "https://evil.example/phish"})
    assert response.status_code == 302 and "evil.example" not in response["Location"]


class TestSignInLockout:
    @pytest.fixture(autouse=True)
    def limits(self, settings):
        settings.LOGIN_MAX_FAILURES = 5
        settings.LOGIN_MAX_FAILURES_PER_IP = 30
        cache.clear()
        yield
        cache.clear()

    def attempt(self, client, email, password):
        return client.post(reverse("accounts:login"), {"username": email, "password": password})

    def test_the_right_password_is_refused_after_five_wrong_ones(self, client, entity):
        person(entity, Role.CONTRACT_REVIEWER, "guess@aji.test")
        for _ in range(5):
            self.attempt(client, "guess@aji.test", "wrong-password")
        response = self.attempt(client, "guess@aji.test", PASSWORD)
        assert "_auth_user_id" not in client.session
        assert "15" in response.content.decode() or "١٥" in response.content.decode()

    def test_a_few_mistakes_do_not_lock_anyone_out(self, client, entity):
        person(entity, Role.CONTRACT_REVIEWER, "typo@aji.test")
        for _ in range(3):
            self.attempt(client, "typo@aji.test", "wrong-password")
        self.attempt(client, "typo@aji.test", PASSWORD)
        assert "_auth_user_id" in client.session

    def test_the_admin_login_is_covered_too(self, client, entity):
        admin = person(entity, Role.ADMIN, "root@aji.test")
        admin.is_staff = admin.is_superuser = True
        admin.save()
        for _ in range(5):
            client.post(reverse("admin:login"), {"username": "root@aji.test", "password": "nope"})
        client.post(reverse("admin:login"), {"username": "root@aji.test", "password": PASSWORD})
        assert "_auth_user_id" not in client.session

    def test_a_forged_forwarded_address_does_not_reset_the_count(self, client, entity):
        person(entity, Role.CONTRACT_REVIEWER, "spoof@aji.test")
        for n in range(5):
            client.post(reverse("accounts:login"), {"username": "spoof@aji.test", "password": "x"},
                        HTTP_X_FORWARDED_FOR=f"10.0.0.{n}")
        client.post(reverse("accounts:login"), {"username": "spoof@aji.test", "password": PASSWORD},
                    HTTP_X_FORWARDED_FOR="10.9.9.9")
        assert "_auth_user_id" not in client.session


def test_the_system_tab_is_for_administrators(client, entity):
    client.force_login(person(entity, Role.CONTRACT_REVIEWER, "r2@aji.test"))
    with mock.patch("apps.documents.ocr.get_ocr_engine") as engine:
        body = client.get(reverse("accounts:settings") + "?tab=system").content.decode()
        engine.assert_not_called()
    assert "?tab=system" not in body


def test_a_signed_out_link_leads_to_sign_in(client, contract):
    response = client.get(reverse("documents:confirm", args=[contract.pk]))
    assert response.status_code == 302 and reverse("accounts:login") in response["Location"]


def test_production_refuses_a_weak_secret_key(monkeypatch):
    import importlib
    import sys

    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.setenv("SECRET_KEY", "insecure-dev-key-change-me")
    monkeypatch.setenv("ALLOWED_HOSTS", "sanad.example")
    sys.modules.pop("config.settings.prod", None)
    with mock.patch("environ.Env.read_env"), pytest.raises(ImproperlyConfigured):
        importlib.import_module("config.settings.prod")
    sys.modules.pop("config.settings.prod", None)


def test_article_numbers_are_not_re_read_as_figures():
    from apps.documents.ocr.pipeline import _BRACKET_INSIDE, _NUMBERING

    assert all(_NUMBERING.fullmatch(t) for t in ["(5):", "(16)", ")4(", "12-", "5:"])
    assert not any(_NUMBERING.fullmatch(t) for t in ["30", "10%", "87,792.05", "180"])
    assert _BRACKET_INSIDE.search("5(5") and not _BRACKET_INSIDE.search("87,792.05")


def test_a_session_signed_in_before_the_lockout_stays_valid(client, entity):
    """Updating the site must not sign everyone out."""
    user = person(entity, Role.CONTRACT_REVIEWER, "old@aji.test")
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    assert client.get(reverse("accounts:settings")).status_code == 200


def test_the_second_backend_cannot_get_round_a_lock(client, entity, settings):
    settings.LOGIN_MAX_FAILURES = 2
    cache.clear()
    person(entity, Role.CONTRACT_REVIEWER, "lock2@aji.test")
    for _ in range(2):
        client.post(reverse("accounts:login"), {"username": "lock2@aji.test", "password": "x"})
    client.post(reverse("accounts:login"), {"username": "lock2@aji.test", "password": PASSWORD})
    assert "_auth_user_id" not in client.session
    cache.clear()
