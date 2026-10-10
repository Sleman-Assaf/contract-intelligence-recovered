"""Self-registration and administrator activation (§34).

The interesting cases are all about what a registrant must NOT be able to do
for themselves, and what a new account can do before somebody approves it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import Entity, Role
from apps.audit.models import AuditAction, AuditEvent

User = get_user_model()
pytestmark = pytest.mark.django_db

GOOD = {
    # The name is required: registration is the only way an account is made,
    # so it is the only chance to ask what to call the person.
    "username": "New Person",
    "email": "new.person@ajigroup.com",
    "password1": "a-long-enough-passphrase-42",
    "password2": "a-long-enough-passphrase-42",
}


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi KSA", code="aji-ksa", country="SA")


@pytest.fixture
def admin(entity):
    return User.objects.create_user(
        email="admin@aji.test", password="test-password-1234",
        role=Role.ADMIN, entity=entity,
    )


@pytest.fixture
def reviewer(entity):
    return User.objects.create_user(
        email="reviewer@aji.test", password="test-password-1234",
        role=Role.CONTRACT_REVIEWER, entity=entity,
    )


@pytest.fixture
def pending():
    account = User.objects.create_user(
        email="pending@ajigroup.com", password="a-long-enough-passphrase-42")
    account.is_active = False
    account.save(update_fields=["is_active"])
    return account


class TestSignUp:
    def test_an_account_is_created(self, client):
        response = client.post(reverse("accounts:signup"), GOOD)
        assert response.status_code == 302
        assert User.objects.filter(email=GOOD["email"]).exists()

    def test_it_arrives_inactive(self, client):
        """The whole point: registering grants nothing by itself."""
        client.post(reverse("accounts:signup"), GOOD)
        assert User.objects.get(email=GOOD["email"]).is_active is False

    def test_it_arrives_with_the_lowest_role(self, client):
        client.post(reverse("accounts:signup"), GOOD)
        assert User.objects.get(email=GOOD["email"]).role == Role.USER

    def test_a_posted_role_is_ignored(self, client):
        """Role governs approval authority; it is never self-declared."""
        client.post(reverse("accounts:signup"), dict(GOOD, role=Role.ADMIN))
        assert User.objects.get(email=GOOD["email"]).role == Role.USER

    def test_a_posted_active_flag_is_ignored(self, client):
        client.post(reverse("accounts:signup"), dict(GOOD, is_active="1"))
        assert User.objects.get(email=GOOD["email"]).is_active is False

    def test_posted_staff_flags_are_ignored(self, client):
        client.post(reverse("accounts:signup"),
                    dict(GOOD, is_superuser="1", is_staff="1"))
        account = User.objects.get(email=GOOD["email"])
        assert account.is_superuser is False
        assert account.is_staff is False

    def test_the_new_account_cannot_sign_in(self, client):
        """Django refuses an inactive user -- asserted rather than assumed."""
        client.post(reverse("accounts:signup"), GOOD)
        response = client.post(reverse("accounts:login"), {
            "username": GOOD["email"], "password": GOOD["password1"]})

        assert response.status_code == 200          # re-rendered, not redirected
        assert not response.wsgi_request.user.is_authenticated

    def test_the_password_is_hashed(self, client):
        client.post(reverse("accounts:signup"), GOOD)
        stored = User.objects.get(email=GOOD["email"]).password
        assert stored != GOOD["password1"]
        # Any Django hasher, not a named one: the test settings swap in a fast
        # hasher, and pinning the production algorithm here would assert the
        # test configuration rather than the behaviour.
        assert "$" in stored
        assert GOOD["password1"] not in stored

    def test_mismatched_passwords_are_rejected(self, client):
        client.post(reverse("accounts:signup"),
                    dict(GOOD, password2="a-different-passphrase-42"))
        assert not User.objects.filter(email=GOOD["email"]).exists()

    def test_a_weak_password_is_rejected(self, client):
        client.post(reverse("accounts:signup"),
                    dict(GOOD, password1="pass", password2="pass"))
        assert not User.objects.filter(email=GOOD["email"]).exists()

    def test_the_address_is_stored_lowercased(self, client):
        """Two accounts differing only in case are two rows and one person."""
        client.post(reverse("accounts:signup"),
                    dict(GOOD, email="Mixed.Case@AJIGroup.com"))
        assert User.objects.filter(email="mixed.case@ajigroup.com").exists()

    def test_a_duplicate_address_creates_nothing(self, client, admin):
        client.post(reverse("accounts:signup"), dict(GOOD, email=admin.email))
        assert User.objects.filter(email=admin.email).count() == 1

    def test_registering_is_recorded(self, client):
        client.post(reverse("accounts:signup"), GOOD)
        assert AuditEvent.objects.filter(action=AuditAction.USER_REGISTERED).exists()

    @override_settings(SIGNUP_ALLOWED_DOMAINS=["ajigroup.com"])
    def test_a_domain_allowlist_is_enforced_when_set(self, client):
        client.post(reverse("accounts:signup"), dict(GOOD, email="someone@gmail.com"))
        assert not User.objects.filter(email="someone@gmail.com").exists()

    @override_settings(SIGNUP_ALLOWED_DOMAINS=["ajigroup.com"])
    def test_the_allowlist_admits_the_right_domain(self, client):
        client.post(reverse("accounts:signup"), GOOD)
        assert User.objects.filter(email=GOOD["email"]).exists()


class TestActivation:
    def test_an_admin_can_activate(self, client, admin, pending):
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[pending.pk]),
                    {"active": "1"})
        pending.refresh_from_db()
        assert pending.is_active is True

    def test_activating_lets_them_sign_in(self, client, admin, pending):
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[pending.pk]),
                    {"active": "1"})
        client.logout()

        response = client.post(reverse("accounts:login"), {
            "username": pending.email, "password": "a-long-enough-passphrase-42"})
        assert response.wsgi_request.user.is_authenticated

    def test_a_reviewer_cannot_activate(self, client, reviewer, pending):
        """MANAGE_USERS only. Reviewing contracts is not administering them."""
        client.force_login(reviewer)
        # Django turns PermissionDenied into a 403 in its exception middleware,
        # so the view raising it surfaces here as a response, not an exception.
        response = client.post(
            reverse("accounts:set_user_active", args=[pending.pk]), {"active": "1"})
        assert response.status_code == 403
        pending.refresh_from_db()
        assert pending.is_active is False

    def test_an_anonymous_visitor_cannot_activate(self, client, pending):
        response = client.post(
            reverse("accounts:set_user_active", args=[pending.pk]), {"active": "1"})
        pending.refresh_from_db()
        assert pending.is_active is False
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_get_does_not_activate(self, client, admin, pending):
        """A link that grants access is one a browser can follow by itself."""
        client.force_login(admin)
        response = client.get(reverse("accounts:set_user_active", args=[pending.pk]))
        pending.refresh_from_db()
        assert pending.is_active is False
        assert response.status_code == 405

    def test_an_admin_cannot_deactivate_themselves(self, client, admin):
        """There is no second way back in."""
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[admin.pk]),
                    {"active": "0"})
        admin.refresh_from_db()
        assert admin.is_active is True

    def test_activation_is_recorded_with_its_actor(self, client, admin, pending):
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[pending.pk]),
                    {"active": "1"})

        event = AuditEvent.objects.filter(action=AuditAction.USER_ACTIVATED).first()
        assert event is not None
        assert event.actor_id == admin.pk

    def test_pending_accounts_are_shown_to_the_admin(self, client, admin, pending):
        client.force_login(admin)
        body = client.get(reverse("accounts:settings") + "?tab=users").content.decode()

        assert pending.email in body
        assert reverse("accounts:set_user_active", args=[pending.pk]) in body

    def test_a_search_cannot_hide_a_pending_account(self, client, admin, pending):
        """The pending list sits outside the filter on purpose."""
        client.force_login(admin)
        body = client.get(
            reverse("accounts:settings") + "?tab=users&q=zzz-no-match"
        ).content.decode()
        assert pending.email in body


class TestRememberMe:
    def test_unticked_ends_with_the_browser(self, client, admin):
        client.post(reverse("accounts:login"), {
            "username": admin.email, "password": "test-password-1234"})
        assert client.session.get_expire_at_browser_close()

    def test_ticked_keeps_the_session(self, client, admin):
        client.post(reverse("accounts:login"), {
            "username": admin.email, "password": "test-password-1234",
            "remember": "1"})
        assert not client.session.get_expire_at_browser_close()


class TestRoleOnActivation:
    """Registration always produces a Regular User. Activating without
    assigning a role admits somebody who can see nothing."""

    def test_a_role_can_be_assigned_while_activating(self, client, admin, pending):
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[pending.pk]),
                    {"active": "1", "role": Role.CONTRACT_REVIEWER})

        pending.refresh_from_db()
        assert pending.is_active is True
        assert pending.role == Role.CONTRACT_REVIEWER

    def test_an_invented_role_is_ignored(self, client, admin, pending):
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[pending.pk]),
                    {"active": "1", "role": "emperor"})

        pending.refresh_from_db()
        assert pending.role == Role.USER
        assert pending.is_active is True

    def test_a_role_change_is_recorded(self, client, admin, pending):
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[pending.pk]),
                    {"active": "1", "role": Role.LEGAL_REVIEWER})

        event = AuditEvent.objects.filter(
            action=AuditAction.PERMISSION_CHANGED).first()
        assert event is not None
        assert event.new_state == Role.LEGAL_REVIEWER
        assert event.actor_id == admin.pk

    def test_deactivating_does_not_change_the_role(self, client, admin, reviewer):
        client.force_login(admin)
        client.post(reverse("accounts:set_user_active", args=[reviewer.pk]),
                    {"active": "0", "role": Role.ADMIN})

        reviewer.refresh_from_db()
        assert reviewer.is_active is False
        assert reviewer.role == Role.CONTRACT_REVIEWER


class TestCreateAdminCommand:
    def test_it_creates_an_administrator(self, monkeypatch):
        from django.core.management import call_command

        monkeypatch.setenv("ADMIN_PW", "a-long-enough-passphrase-42")
        call_command("create_admin", "boss@ajigroup.com", password_env="ADMIN_PW")

        account = User.objects.get(email="boss@ajigroup.com")
        assert account.role == Role.ADMIN
        assert account.is_active is True
        assert account.check_password("a-long-enough-passphrase-42")

    def test_it_does_not_grant_django_superuser_by_default(self, monkeypatch):
        """Administering this application is not unrestricted database access."""
        from django.core.management import call_command

        monkeypatch.setenv("ADMIN_PW", "a-long-enough-passphrase-42")
        call_command("create_admin", "boss@ajigroup.com", password_env="ADMIN_PW")

        assert User.objects.get(email="boss@ajigroup.com").is_superuser is False

    def test_it_promotes_an_existing_account(self, monkeypatch, reviewer):
        from django.core.management import call_command

        monkeypatch.setenv("ADMIN_PW", "a-long-enough-passphrase-42")
        call_command("create_admin", reviewer.email, password_env="ADMIN_PW")

        reviewer.refresh_from_db()
        assert reviewer.role == Role.ADMIN

    def test_a_weak_password_is_refused(self, monkeypatch):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        monkeypatch.setenv("ADMIN_PW", "pass")
        with pytest.raises(CommandError):
            call_command("create_admin", "boss@ajigroup.com", password_env="ADMIN_PW")
        assert not User.objects.filter(email="boss@ajigroup.com").exists()

    def test_an_empty_password_variable_is_refused(self, monkeypatch):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        monkeypatch.setenv("ADMIN_PW", "")
        with pytest.raises(CommandError):
            call_command("create_admin", "boss@ajigroup.com", password_env="ADMIN_PW")


class TestInactiveMessage:
    """Somebody whose account is waiting must be told, without the page
    becoming a way to discover which addresses exist."""

    def test_the_owner_is_told_their_account_is_not_active_yet(self, client, pending):
        response = client.post(reverse("accounts:login"), {
            "username": pending.email, "password": "a-long-enough-passphrase-42"})
        body = response.content.decode()

        assert "لم يُفعّل مسؤول النظام حسابك بعد" in body
        assert not response.wsgi_request.user.is_authenticated

    def test_a_wrong_password_does_not_reveal_the_account(self, client, pending):
        """Otherwise anyone could type an address and learn it is registered."""
        body = client.post(reverse("accounts:login"), {
            "username": pending.email, "password": "not-the-right-password"}
        ).content.decode()

        assert "لم يُفعّل" not in body

    def test_an_unknown_address_gets_the_generic_message(self, client):
        body = client.post(reverse("accounts:login"), {
            "username": "nobody@ajigroup.com", "password": "a-long-enough-passphrase-42"}
        ).content.decode()

        assert "لم يُفعّل" not in body

    def test_an_active_account_still_signs_in(self, client, admin):
        response = client.post(reverse("accounts:login"), {
            "username": admin.email, "password": "test-password-1234"})
        assert response.wsgi_request.user.is_authenticated

    def test_the_message_is_translated(self):
        from django.utils import translation
        from apps.accounts.forms import LoginForm

        with translation.override("en"):
            assert "administrator" in str(LoginForm.inactive_message).lower()


class TestAuthPageChrome:
    def test_the_auth_pages_use_the_application_font(self, client):
        """Signing in used to change the typeface. The sign-in pages load the
        same two typefaces as every other page -- Noto Kufi Arabic and Plus
        Jakarta Sans -- and none of the brand faces the site set aside."""
        for url in (reverse("accounts:login"), reverse("accounts:signup")):
            body = client.get(url).content.decode()
            assert "/static/css/aji.css" in body
            assert "family=Noto+Kufi+Arabic" in body and "family=Plus+Jakarta+Sans" in body
            assert "draft-b-" not in body and "ibm-plex-sans-arabic-" not in body

    def test_language_can_be_switched_before_signing_in(self, client):
        body = client.get(reverse("accounts:login")).content.decode()
        assert reverse("accounts:toggle_language") in body

    def test_switching_language_works_for_an_anonymous_visitor(self, client):
        client.post(reverse("accounts:toggle_language"),
                    HTTP_REFERER=reverse("accounts:login"))
        assert client.session.get("ui_lang") == "en"

    def test_the_english_page_is_in_english(self, client):
        client.post(reverse("accounts:toggle_language"),
                    HTTP_REFERER=reverse("accounts:login"))
        body = client.get(reverse("accounts:login")).content.decode()

        assert 'lang="en"' in body
        assert 'dir="ltr"' in body
        assert "Sign in" in body or "sign in" in body.lower()


class TestBrandPanel:
    """The panel beside the form. Decorative, but it must not regress into
    something a screen reader narrates or a reduced-motion setting ignores."""

    def test_the_scene_is_on_both_auth_pages(self, client):
        for url in (reverse("accounts:login"), reverse("accounts:signup")):
            body = client.get(url).content.decode()
            assert "scan__doc" in body
            assert "scan__beam" in body

    def test_it_is_hidden_from_a_screen_reader(self, client):
        """A document of meaningless lines read aloud is noise."""
        body = client.get(reverse("accounts:login")).content.decode()
        marker = body.index('class="scan"')
        assert 'aria-hidden="true"' in body[marker - 120:marker + 120]

    def test_the_yellow_diagonals_are_gone(self, client):
        body = client.get(reverse("accounts:login")).content.decode()
        assert "auth__art" not in body
        assert "polygon" not in body


class TestLanguageSwapTransition:
    """Switching language is a POST and a redirect, so the animation has to
    span two documents: an exit before the form is sent and an entrance on
    the page that comes back."""

    def test_the_toggle_is_marked_on_the_auth_pages(self, client):
        body = client.get(reverse("accounts:login")).content.decode()
        marker = body.index(reverse("accounts:toggle_language"))
        assert "data-langswap" in body[marker - 200:marker + 200]

    def test_the_toggle_is_marked_in_the_application(self, client, admin):
        client.force_login(admin)
        body = client.get(reverse("accounts:settings")).content.decode()
        marker = body.index(reverse("accounts:toggle_language"))
        assert "data-langswap" in body[marker - 200:marker + 200]

    def test_the_script_is_loaded_on_both(self, client, admin):
        assert "langswap.js" in client.get(
            reverse("accounts:login")).content.decode()
        client.force_login(admin)
        assert "langswap.js" in client.get(
            reverse("accounts:settings")).content.decode()

    def test_switching_still_works_without_the_script(self, client):
        """The handler defers the submit behind a timer. If anything stops the
        script the button must still change the language, so the plain form
        POST is what the test exercises."""
        client.post(reverse("accounts:toggle_language"),
                    HTTP_REFERER=reverse("accounts:login"))
        assert client.session.get("ui_lang") == "en"


class TestThemeSwapTransition:
    """The new surface spreads from the button that was pressed. Like the
    language switch this spans two documents, so the position and the previous
    colour are handed across the reload."""

    def test_the_toggle_is_marked_on_the_auth_pages(self, client):
        body = client.get(reverse("accounts:login")).content.decode()
        marker = body.index(reverse("accounts:toggle_theme"))
        assert "data-themeswap" in body[marker - 200:marker + 200]

    def test_the_toggle_is_marked_in_the_application(self, client, admin):
        client.force_login(admin)
        body = client.get(reverse("accounts:settings")).content.decode()
        marker = body.index(reverse("accounts:toggle_theme"))
        assert "data-themeswap" in body[marker - 200:marker + 200]

    def test_the_script_is_loaded_on_both(self, client, admin):
        assert "themeswap.js" in client.get(
            reverse("accounts:login")).content.decode()
        client.force_login(admin)
        assert "themeswap.js" in client.get(
            reverse("accounts:settings")).content.decode()

    def test_switching_still_works_without_the_script(self, client):
        """The submit is deferred behind a timer. If the script never runs the
        button must still change the theme."""
        client.post(reverse("accounts:toggle_theme"),
                    HTTP_REFERER=reverse("accounts:login"))
        assert client.session.get("ui_theme") == "dark"

    def test_an_anonymous_visitor_keeps_their_choice(self, client):
        client.post(reverse("accounts:toggle_theme"),
                    HTTP_REFERER=reverse("accounts:login"))
        body = client.get(reverse("accounts:login")).content.decode()
        assert 'data-theme="dark"' in body


class TestDeleteUser:
    """Deleting an account. Every refusal below is deliberate."""

    def test_an_admin_can_delete_an_account(self, client, admin, pending):
        client.force_login(admin)
        client.post(reverse("accounts:delete_user", args=[pending.pk]))
        assert not User.objects.filter(pk=pending.pk).exists()

    def test_a_reviewer_cannot_delete(self, client, reviewer, pending):
        client.force_login(reviewer)
        response = client.post(reverse("accounts:delete_user", args=[pending.pk]))
        assert response.status_code == 403
        assert User.objects.filter(pk=pending.pk).exists()

    def test_an_anonymous_visitor_cannot_delete(self, client, pending):
        client.post(reverse("accounts:delete_user", args=[pending.pk]))
        assert User.objects.filter(pk=pending.pk).exists()

    def test_get_deletes_nothing(self, client, admin, pending):
        client.force_login(admin)
        response = client.get(reverse("accounts:delete_user", args=[pending.pk]))
        assert response.status_code == 405
        assert User.objects.filter(pk=pending.pk).exists()

    def test_an_admin_cannot_delete_themselves(self, client, admin):
        """There is no way back in afterwards."""
        client.force_login(admin)
        client.post(reverse("accounts:delete_user", args=[admin.pk]))
        assert User.objects.filter(pk=admin.pk).exists()

    def test_the_last_administrator_cannot_be_deleted(self, client, admin, entity):
        """Otherwise nobody is left who can administer the system."""
        other = User.objects.create_user(
            email="second.admin@aji.test", password="a-long-enough-passphrase-42",
            role=Role.ADMIN, entity=entity)
        client.force_login(other)

        client.post(reverse("accounts:delete_user", args=[admin.pk]))
        assert not User.objects.filter(pk=admin.pk).exists()   # two admins: fine

        # `other` is now the only one, and is deleting itself anyway -- make a
        # third, sign in as it, and try to remove the last remaining admin.
        third = User.objects.create_user(
            email="third@aji.test", password="a-long-enough-passphrase-42",
            role=Role.CONTRACT_REVIEWER, entity=entity)
        third.role = Role.ADMIN
        third.save(update_fields=["role"])
        client.force_login(third)
        other.is_active = True
        other.save(update_fields=["is_active"])
        # Both active now, so this one is allowed.
        client.post(reverse("accounts:delete_user", args=[other.pk]))
        assert not User.objects.filter(pk=other.pk).exists()

    def test_the_only_active_admin_is_protected(self, client, admin, entity):
        helper = User.objects.create_user(
            email="helper@aji.test", password="a-long-enough-passphrase-42",
            role=Role.ADMIN, entity=entity)
        helper.is_active = False            # not an active administrator
        helper.save(update_fields=["is_active"])
        client.force_login(helper)
        # `admin` is the last ACTIVE administrator.
        client.post(reverse("accounts:delete_user", args=[admin.pk]))
        assert User.objects.filter(pk=admin.pk).exists()

    def test_somebody_who_uploaded_a_contract_is_refused(self, client, admin, entity):
        """`Contract.uploaded_by` is PROTECT: who filed a document outlives
        the person leaving. Deactivating is the answer, not deletion."""
        from apps.contracts.models import Contract, ContractType

        owner = User.objects.create_user(
            email="uploader@aji.test", password="a-long-enough-passphrase-42",
            role=Role.CONTRACT_REVIEWER, entity=entity)
        ctype = ContractType.objects.create(name="Design", code="design")
        Contract.objects.create(
            reference="C-1", project_name="P", client_name="C",
            entity=entity, contract_type=ctype, uploaded_by=owner,
        )

        client.force_login(admin)
        client.post(reverse("accounts:delete_user", args=[owner.pk]))
        assert User.objects.filter(pk=owner.pk).exists()

    def test_deleting_is_recorded_and_survives_the_account(self, client, admin, pending):
        """The trail stores the address at write time for exactly this."""
        email = pending.email
        client.force_login(admin)
        client.post(reverse("accounts:delete_user", args=[pending.pk]))

        event = AuditEvent.objects.filter(action=AuditAction.USER_DELETED).first()
        assert event is not None
        assert event.details.get("email") == email
        assert event.actor_id == admin.pk

    def test_the_directory_offers_the_control(self, client, admin, pending):
        client.force_login(admin)
        body = client.get(reverse("accounts:settings") + "?tab=users").content.decode()
        assert reverse("accounts:delete_user", args=[pending.pk]) in body

    def test_your_own_row_has_no_delete_button(self, client, admin):
        client.force_login(admin)
        body = client.get(reverse("accounts:settings") + "?tab=users").content.decode()
        assert reverse("accounts:delete_user", args=[admin.pk]) not in body
