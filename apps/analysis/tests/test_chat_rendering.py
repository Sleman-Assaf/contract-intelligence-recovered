"""How an answer is laid out, and how a file on its own is treated.

Answers were printed as one run of text with the model's markdown left in and
the page's direction imposed on them, so an Arabic answer on the English page
(or the reverse) was laid out backwards: list numbers at the wrong end, and an
English term in brackets inside an Arabic sentence reordered around it. Each
paragraph and list item now takes its direction from its own text.
"""
from __future__ import annotations

import io

import pytest
from django.urls import reverse

from apps.contracts.templatetags.aji import chat_markdown

MIXED = (
    "هناك عدة مخاطر:\n\n"
    "1. **غرامات التأخير غير المحدودة**: هذا يُعد خطاً أحمر (Red Line) حسب معيار AJi.\n\n"
    "2. **الملكية الفكرية**: تنتقل عند التوقيع.\n"
    "- نقطة أولى\n- نقطة ثانية\n\n"
    "### Summary\nThe clause is a red line."
)


class TestTheLayout:
    def test_every_block_carries_its_own_direction(self):
        html = chat_markdown(MIXED)
        assert '<p dir="rtl">هناك عدة مخاطر:</p>' in html
        assert '<ol dir="rtl">' in html and '<ul dir="rtl">' in html
        assert html.count("<li>") == 4
        assert '<p class="md-h" dir="ltr"><strong>Summary</strong></p>' in html
        assert '<p dir="ltr">The clause is a red line.</p>' in html

    def test_an_arabic_list_is_right_to_left_even_with_english_inside(self):
        """The defect seen in the browser: dir=auto on every item left the
        list itself with no letters of its own, so it fell back to LTR and
        the numbers of an Arabic list sat on the wrong side."""
        html = chat_markdown("1. **غرامات** (Liquidated Damages) بلا سقف\n2. Article 9.1 transfers IP")
        assert '<ol dir="rtl">' in html
        assert '<li dir="ltr">Article 9.1 transfers IP</li>' in html

    def test_bold_becomes_strong_and_the_asterisks_go(self):
        html = chat_markdown(MIXED)
        assert "<strong>غرامات التأخير غير المحدودة</strong>" in html
        assert "**" not in html

    def test_a_list_split_by_blank_lines_keeps_counting(self):
        """The model writes "1. …\n\n2. …"; the second item must not restart at 1."""
        html = chat_markdown(MIXED)
        assert '<ol dir="rtl" start="2">' in html

    def test_a_heading(self):
        assert '<p class="md-h" dir="ltr"><strong>Summary</strong></p>' in chat_markdown(MIXED)

    def test_a_wrapped_list_item_stays_one_item(self):
        html = chat_markdown("1. first line\n   continues here\n2. second")
        assert html.count("<li") == 2 and "first line continues here" in html

    def test_nothing_the_model_writes_becomes_markup(self):
        html = chat_markdown('<script>alert(1)</script> **<img src=x onerror=alert(1)>**')
        assert "<script>" not in html and "<img" not in html
        assert "&lt;script&gt;" in html

    def test_empty(self):
        assert chat_markdown("") == "" and chat_markdown(None) == ""


pytestmark_db = pytest.mark.django_db


@pytest.mark.django_db
def test_the_thread_uses_it(client, reviewer):
    from apps.ai.models import AdvisorConversation, AdvisorMessage

    convo = AdvisorConversation.objects.create(user=reviewer, title="q")
    AdvisorMessage.objects.create(conversation=convo, role="user", body="What about delay?")
    AdvisorMessage.objects.create(conversation=convo, role="assistant", body=MIXED)
    client.force_login(reviewer)
    session = client.session; session["advisor.conversation"] = str(convo.pk); session.save()
    body = client.get(reverse("analysis:advisor")).content.decode()
    assert 'class="bubble bubble--answer"' in body
    assert '<ol dir="rtl"><li><strong>' in body
    assert 'class="bubble bubble--mine" dir="auto"' in body


@pytest.mark.django_db
def test_the_chat_takes_no_uploads(client, reviewer, chat_file):
    """The chat answers from analysed files only: nothing on the page uploads
    a file, and the old upload endpoints are gone."""
    from django.urls import NoReverseMatch

    client.force_login(reviewer)
    body = client.get(reverse("analysis:advisor")).content.decode()
    for marker in ("data-attach-tray", "composer__attach", "composer__drop", 'type="file"',
                   'name="photo"', "attach-action", 'enctype="multipart/form-data"'):
        assert marker not in body, marker
    for name in ("advisor_attach", "advisor_pending", "advisor_attach_clear",
                 "advisor_attach_status"):
        with pytest.raises(NoReverseMatch):
            reverse(f"analysis:{name}")


@pytest.mark.django_db
def test_a_posted_file_is_ignored_and_no_file_means_no_question(client, reviewer, monkeypatch):
    """A form hand-built to send a photo gets no further than the rule: with
    no analysed file chosen there is nothing to answer from."""
    from apps.ai.models import AdvisorMessage
    from apps.ai.services import ai_service

    monkeypatch.setattr(ai_service, "answer_question", lambda **kw: pytest.fail("asked the model"))
    f = io.BytesIO(b"PNG fake"); f.name = "p.png"
    client.force_login(reviewer)
    response = client.post(reverse("analysis:advisor"), {"question": "ما هذا؟", "photo": f})
    assert response.status_code == 302
    assert not AdvisorMessage.objects.exists()


class TestEachTurnTakesItsLanguagesSide:
    """Asked for: an Arabic message is laid out right to left as a whole --
    card, AJi mark, labels -- on either interface; an English one is left as
    it was."""

    def test_the_filter(self):
        from apps.contracts.templatetags.aji import text_dir

        assert text_dir("مرحبا") == "rtl"
        assert text_dir("مرحباً، ما هو البند 4.3 (Delay Damages)؟") == "rtl"
        assert text_dir("in general") == ""
        assert text_dir("Clause 4.3 مرحبا") == ""
        assert text_dir("") == "" and text_dir(None) == ""

    @pytest.mark.django_db
    def test_on_the_english_page(self, client, reviewer, monkeypatch):
        from apps.ai.models import AdvisorConversation, AdvisorMessage

        reviewer.preferred_language = "en"; reviewer.save()
        client.force_login(reviewer)
        session = client.session; session["ui_lang"] = "en"; session.save()
        c = AdvisorConversation.objects.create(user=reviewer, title="t")
        for role, body in (("user", "in general"), ("assistant", "In general, a contract is..."),
                           ("user", "مرحبا"), ("assistant", "مرحباً بك!")):
            AdvisorMessage.objects.create(conversation=c, role=role, body=body)
        html = client.get(reverse("analysis:advisor") + f"?c={c.pk}").content.decode()
        assert html.count('class="turn turn--mine" dir="rtl"') == 1
        assert html.count('<div class="turn" dir="rtl">') == 1
        assert html.count('class="turn turn--mine">') == 1 and html.count('<div class="turn">') == 1
