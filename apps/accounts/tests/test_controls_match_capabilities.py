"""A control a reader is not allowed to use is not shown to them.

Found by crawling the site as each demo role: every role saw "تحليل جديد" in
the sidebar and the top bar, and "تصدير PDF" on the report page, and three of
the six roles were forbidden the moment they clicked -- with Django's bare
English "403 Forbidden" as the answer, and no way back but the browser.

Two things are held here. The controls follow the capabilities, so the page
never offers what the click would refuse. And when a reader does reach a
forbidden or missing page -- by URL, from an old bookmark -- they get one of
ours, in their language, with a way out.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts import permissions as perms
from apps.accounts.models import Country, Entity, Role, User
from apps.contracts.models import Contract, ContractType

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi - Jordan", code="aji-jo", country=Country.JORDAN)


@pytest.fixture
def contract(entity):
    kind = ContractType.objects.create(code="arch", name="Architectural Design")
    uploader = User.objects.create_user(
        email="uploader@aji.test", password="test-password-1234",
        role=Role.CONTRACT_REVIEWER, entity=entity)
    return Contract.objects.create(
        project_name="Amman Gateway", contract_title="Design Consultancy",
        client_name="GAM", country=Country.JORDAN, entity=entity,
        contract_type=kind, uploaded_by=uploader,
    )


def signed_in(client, role, entity):
    user = User.objects.create_user(
        email=f"{role}@aji.test", password="test-password-1234", role=role, entity=entity)
    client.force_login(user)
    return user


UPLOAD_URL_IN_PAGE = 'href="/contracts/upload/"'


class TestTheUploadControl:
    @pytest.mark.parametrize("role", [r.value for r in Role])
    def test_shown_exactly_to_those_who_may_upload(self, client, entity, role):
        user = signed_in(client, role, entity)
        body = client.get(reverse("contracts:dashboard")).content.decode()
        allowed = perms.has_capability(user, perms.UPLOAD_CONTRACT)
        assert (UPLOAD_URL_IN_PAGE in body) is allowed, (
            f"{role}: upload {'may' if allowed else 'may not'} but the page "
            f"{'does not offer' if allowed else 'offers'} it"
        )

    @pytest.mark.parametrize("role", [Role.LEGAL_REVIEWER, Role.MANAGEMENT, Role.STANDARD_MANAGER])
    def test_the_empty_states_do_not_offer_it_either(self, client, entity, role):
        """The empty contracts list used to say "رفع عقد جديد" to everyone."""
        signed_in(client, role, entity)
        body = client.get(reverse("contracts:list")).content.decode()
        assert UPLOAD_URL_IN_PAGE not in body


class TestTheExportControls:
    @pytest.mark.parametrize("role", [r.value for r in Role])
    def test_shown_exactly_to_those_who_may_export(self, client, entity, contract, role):
        user = signed_in(client, role, entity)
        body = client.get(reverse("reports:index") + f"?contract={contract.pk}").content.decode()
        allowed = perms.has_capability(user, perms.EXPORT_REPORT)
        pdf = reverse("reports:pdf", args=[contract.pk])
        assert (pdf in body) is allowed, f"{role}: export button vs capability disagree"


class TestTheSendToLegalControl:
    @pytest.mark.parametrize("role", [r.value for r in Role])
    def test_the_report_offers_it_exactly_when_the_workflow_would_accept_it(
            self, client, entity, contract, role):
        """An administrator holds no review authority by design, and was
        sent from the report to a workflow page that offered nothing."""
        from apps.workflow import services as wf

        user = signed_in(client, role, entity)
        response = client.get(reverse("reports:index") + f"?contract={contract.pk}")
        if response.status_code != 200:
            pytest.skip(f"{role} cannot open this contract at all")
        offered = any(a.key == "send_to_legal" for a in wf.available_actions(user, contract))
        assert ("إرسال للمراجعة القانونية" in response.content.decode()) is offered, role


class TestTheDownloadControl:
    @pytest.mark.parametrize("role", [r.value for r in Role])
    def test_shown_exactly_to_those_who_may_download(self, client, entity, contract, role):
        """The source file leaves AJi too. The standards manager saw "تحميل"
        beside every file and was forbidden on the click."""
        from apps.documents.models import SourceFile

        SourceFile.objects.create(
            contract=contract, original_filename="c.pdf", stored_path="c.pdf",
            content_type="application/pdf", size_bytes=8, sha256="0" * 64)
        user = signed_in(client, role, entity)
        response = client.get(reverse("contracts:detail", args=[contract.pk]))
        if response.status_code != 200:
            pytest.skip(f"{role} cannot open this contract at all")
        allowed = perms.has_capability(user, perms.DOWNLOAD_SOURCE_FILE)
        assert ("/download/" in response.content.decode()) is allowed, (
            f"{role}: download button vs capability disagree")


class TestOurErrorPages:
    """The page a reader lands on is ours, not the framework's."""

    def test_forbidden_is_a_real_page_in_the_readers_language(self, client, entity):
        signed_in(client, Role.LEGAL_REVIEWER, entity)
        response = client.get(reverse("contracts:upload"))
        assert response.status_code == 403
        body = response.content.decode()
        assert 'class="nf nf--403"' in body and "nf__badge--lock" in body
        # The reader's own role, named.
        assert 'class="nf__role"' in body
        assert 'lang="ar"' in body and 'dir="rtl"' in body
        assert "هذه الصفحة خارج صلاحياتك" in body
        # And a way out.
        assert 'href="/"' in body

    def test_forbidden_in_english(self, client, entity):
        user = signed_in(client, Role.LEGAL_REVIEWER, entity)
        user.preferred_language = "en"
        user.save(update_fields=["preferred_language"])
        body = client.get(reverse("contracts:upload")).content.decode()
        assert 'lang="en"' in body and 'dir="ltr"' in body
        assert "You do not have access to this page" in body

    def test_not_found_is_ours_too(self, client, entity, settings):
        settings.DEBUG = False
        signed_in(client, Role.ADMIN, entity)
        response = client.get("/contracts/00000000-0000-0000-0000-000000000000/")
        assert response.status_code == 404
        body = response.content.decode()
        # The platform's own page: the matrix "404", the finding card, the
        # address that was asked for, and ways out.
        assert 'class="nf nf--404"' in body and "nf__matrix" in body and "nf__finding" in body
        assert 'lang="ar"' in body and 'dir="rtl"' in body
        assert "/contracts/00000000-0000-0000-0000-000000000000/" in body
        assert 'href="/"' in body and 'action="/search/"' in body

    def test_not_found_in_english_and_signed_out(self, client, settings):
        settings.DEBUG = False
        session = client.session; session["ui_lang"] = "en"; session.save()
        body = client.get("/no-such-page/").content.decode()
        assert 'lang="en"' in body and 'dir="ltr"' in body
        assert "This clause does not exist on the platform" in body

    def test_the_server_error_page_renders_with_no_context_at_all(self):
        """A 500 gets an empty context: no request, no processors. The page
        must still come out whole, and in Arabic by default."""
        from django.template.loader import render_to_string

        body = render_to_string("500.html")
        assert 'class="nf nf--500"' in body and "data-nf-retry" in body
        # No lookups that could fail with whatever broke: plain paths only.
        assert 'href="/"' in body and "nf__search" not in body
        assert 'lang="ar"' in body and 'dir="rtl"' in body


class TestErrorPagesFollowTheSiteLanguage:
    """Each error page in the language of the site the reader is using --
    words, direction and theme -- the 500 included, which Django itself
    renders with no context at all."""

    def _english(self, client, entity, role=Role.ADMIN, theme="light"):
        user = signed_in(client, role, entity)
        user.preferred_language = "en"
        user.preferred_theme = theme
        user.save(update_fields=["preferred_language", "preferred_theme"])
        return user

    def test_404_in_english(self, client, entity, settings):
        settings.DEBUG = False
        self._english(client, entity)
        body = client.get("/no-such-page/").content.decode()
        assert 'lang="en"' in body and 'dir="ltr"' in body
        assert "This clause does not exist on the platform" in body and "Finding #404" in body

    def test_403_in_english(self, client, entity):
        self._english(client, entity, role=Role.LEGAL_REVIEWER)
        body = client.get(reverse("contracts:upload")).content.decode()
        assert 'lang="en"' in body and "Permission required" in body and "Finding #403" in body

    def test_a_real_crash_shows_the_500_in_english_and_dark(self, entity, settings, monkeypatch):
        from django.test import Client

        from apps.contracts import views as contract_views

        settings.DEBUG = False
        client = Client(raise_request_exception=False)
        self._english(client, entity, theme="dark")

        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(contract_views.DashboardView, "get", boom)
        response = client.get(reverse("contracts:dashboard"))
        assert response.status_code == 500
        body = response.content.decode()
        assert 'lang="en"' in body and 'dir="ltr"' in body and 'data-theme="dark"' in body
        assert "Server error" in body and "Try again" in body and 'class="nf nf--500"' in body

    def test_the_500_in_arabic_by_default(self, entity, settings, monkeypatch):
        from django.test import Client

        from apps.contracts import views as contract_views

        settings.DEBUG = False
        client = Client(raise_request_exception=False)
        signed_in(client, Role.ADMIN, entity)
        monkeypatch.setattr(contract_views.DashboardView, "get",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        body = client.get(reverse("contracts:dashboard")).content.decode()
        assert 'lang="ar"' in body and 'dir="rtl"' in body and "خطأ في الخادم" in body

    def test_the_500_survives_a_broken_session(self, rf):
        """If even the reader's preferences cannot be read, the page still renders."""
        from apps.common.errors import server_error

        class Broken:
            @property
            def is_authenticated(self):
                raise RuntimeError("database down")

        request = rf.get("/")
        request.user = Broken()
        response = server_error(request)
        assert response.status_code == 500 and 'class="nf nf--500"' in response.content.decode()
