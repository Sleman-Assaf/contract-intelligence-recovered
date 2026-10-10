"""Carrying a reader's earlier exchanges into a new question (§38).

Most of what follows is about what must NOT be carried. Retrieval across
conversations is the one feature here that can manufacture the failure the
whole system exists to prevent -- a fluent answer attributing one contract's
terms to another -- so the boundaries are asserted, not assumed.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import Entity, Role, User
from apps.ai.models import AdvisorConversation, AdvisorMessage
from apps.ai.recall import MAX_EXCHANGES, recall_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def entity():
    return Entity.objects.create(name="AJi KSA", code="aji-ksa-recall", country="SA")


@pytest.fixture
def reader(entity):
    return User.objects.create_user(
        email="reader@aji.test", password="a-long-enough-passphrase-42",
        role=Role.CONTRACT_REVIEWER, entity=entity,
    )


@pytest.fixture
def other_reader(entity):
    return User.objects.create_user(
        email="other@aji.test", password="a-long-enough-passphrase-42",
        role=Role.CONTRACT_REVIEWER, entity=entity,
    )


def _analysed_file(user):
    """A contract of `user`'s the chat can answer from (it has a RAG index)."""
    from apps.analysis.models import AnalysisRun, RagIndex
    from apps.contracts.models import Contract, ContractType

    kind = ContractType.objects.create(code="recall-design", name="Design")
    contract = Contract.objects.create(project_name="Recall Tower", client_name="Client",
                                       entity=user.entity, uploaded_by=user, country="SA",
                                       contract_type=kind)
    run = AnalysisRun.objects.create(contract=contract, version=1, status="succeeded")
    contract.current_analysis = run
    contract.save(update_fields=["current_analysis"])
    RagIndex.objects.create(analysis=run, contract=contract)
    return contract


def exchange(user, question, answer, *, title="", contract="", remembered=True):
    """One question and its answer, in a thread of their own."""
    convo = AdvisorConversation.objects.create(
        user=user, title=title or question[:60], remembered=remembered)
    AdvisorMessage.objects.create(
        conversation=convo, role=AdvisorMessage.Role.USER, body=question)
    AdvisorMessage.objects.create(
        conversation=convo, role=AdvisorMessage.Role.ASSISTANT, body=answer,
        contract_name=contract)
    return convo


class TestWhatIsFound:
    def test_a_related_earlier_question_is_recalled(self, reader):
        exchange(reader, "ما سقف غرامة التأخير المعقول؟", "حتى 10% من قيمة العقد.")
        found = recall_for(reader, "وما سقف غرامة التأخير في العقود الطويلة؟")

        assert len(found) == 1
        assert "غرامة" in found[0]["question"]

    def test_an_unrelated_one_is_not(self, reader):
        exchange(reader, "ما هي مدة الضمان المعتادة؟", "عادة سنة من التسليم.")
        assert recall_for(reader, "اشرح بند التحكيم المؤسسي") == []

    def test_the_definite_article_does_not_hide_a_match(self, reader):
        """"العقد" and "عقد" are one word to a reader and two to a plain match."""
        exchange(reader, "ما قيمة العقد الإجمالية؟", "تُحدد في ملحق التسعير.")
        assert recall_for(reader, "كيف تُحتسب قيمة عقد الاستشارات؟")

    def test_english_works_too(self, reader):
        exchange(reader, "What is a fair liability cap?", "Typically the fees paid.")
        assert recall_for(reader, "Explain the liability cap for consultants")

    def test_at_most_a_few_are_carried(self, reader):
        """The prompt already holds the standards and the current thread, and
        the service drops the head of anything too long."""
        for i in range(10):
            exchange(reader, f"سؤال {i} عن غرامة التأخير", f"إجابة {i}")
        assert len(recall_for(reader, "غرامة التأخير")) <= MAX_EXCHANGES

    def test_one_per_thread(self, reader):
        """Three follow-ups from one conversation say one thing three times."""
        convo = AdvisorConversation.objects.create(user=reader, title="غرامة")
        for i in range(3):
            AdvisorMessage.objects.create(
                conversation=convo, role=AdvisorMessage.Role.USER,
                body=f"سؤال {i} عن غرامة التأخير والتعويضات")
            AdvisorMessage.objects.create(
                conversation=convo, role=AdvisorMessage.Role.ASSISTANT,
                body=f"إجابة {i}")
        exchange(reader, "ما حد غرامة التأخير والتعويضات؟", "10%")

        found = recall_for(reader, "غرامة التأخير والتعويضات")
        assert len({item["conversation"] for item in found}) == len(found)

    def test_an_unanswered_question_carries_nothing(self, reader):
        convo = AdvisorConversation.objects.create(user=reader, title="فشل")
        AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.USER,
            body="ما سقف غرامة التأخير؟")
        AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.ASSISTANT,
            error="تعذّر الوصول إلى الخدمة.")

        assert recall_for(reader, "سقف غرامة التأخير") == []


class TestHowArabicIsWritten:
    """Four ways a question can be the same question and not look like it.

    Every one of these was found by asking the running assistant a natural
    follow-up and watching nothing be recalled -- not by reading the code.
    """

    def test_a_question_mark_is_not_part_of_the_word(self, reader):
        """؟ sits in the Arabic block with the letters, so a range that
        looks right glues it onto the noun before it."""
        exchange(reader, "ما مدة عقد الاستشارات؟", "سنتان عادةً.")
        assert recall_for(reader, "وما مدة الاستشارات المعتادة؟")

    def test_diacritics_do_not_hide_a_word(self, reader):
        """Tashkeel is optional and inconsistently typed: "تُحتسب" and
        "تحتسب" are the same word to everyone except a string compare."""
        exchange(reader, "كيف تحتسب غرامة التاخير؟", "أسبوعياً.")
        assert recall_for(reader, "كيف تُحتسب غرامة التأخير؟")

    def test_a_preposition_written_onto_the_noun(self, reader):
        """"لغرامة" is "ل" + "غرامة" -- one word to the writer."""
        exchange(reader, "ما السقف المعقول لغرامة التأخير؟", "10% من قيمة العقد.")
        assert recall_for(reader, "وكيف تُحتسب غرامة التأخير أسبوعياً؟")

    def test_a_prefix_is_not_assumed_away(self, reader):
        """The "ل" of "لجنة" is part of the word. Stripping is a guess, so
        both spellings are kept and the guess cannot destroy the real one."""
        exchange(reader, "من يرأس لجنة المشروع؟", "مدير المشروع.")
        assert recall_for(reader, "ما صلاحيات لجنة المشروع؟")

    def test_a_conjunction_does_not_make_a_stopword_count(self, reader):
        """"وما" is "ما". Counting it as a real word inflates the divisor
        and quietly pushes honest matches under the threshold."""
        from apps.ai.recall import _terms

        assert _terms("وما وكيف فهل") == set()


class TestTheThingItWasBuiltFor:
    """The check a reader actually ran, in one place and automated.

    Told the assistant "انا اسمي سليمان مستشار عقود" in one thread, then opened
    a *new* one and asked "ما هو اسمي الذي اعطيتك اياه ؟ وما هي مهنتي". It
    answered "اسمك هو سليمان، ومهنتك مستشار عقود" -- which it could only do by
    reaching into the earlier conversation.

    That is the whole feature in one exchange, so it is worth holding onto as
    a test rather than as something someone remembers to try. The model is not
    called here: what is asserted is that the earlier exchange is found and
    reaches the prompt, which is everything this side of the system controls.
    """

    TOLD = "انا اسمي سليمان مستشار عقود"
    ANSWERED = "أهلاً بك يا سليمان. يسعدني التعامل مع زميل في مجال استشارات العقود."
    ASKED_LATER = "ما هو اسمي الذي اعطيتك اياه ؟ وما هي مهنتي"

    def test_a_fact_from_one_thread_is_found_from_another(self, reader):
        exchange(reader, self.TOLD, self.ANSWERED)

        found = recall_for(reader, self.ASKED_LATER)

        assert found, "the earlier conversation was not recalled"
        assert self.TOLD in found[0]["question"]
        assert "سليمان" in found[0]["answer"]

    def test_the_fact_reaches_the_prompt(self, reader):
        """Found is not the same as sent."""
        from apps.ai import prompts

        exchange(reader, self.TOLD, self.ANSWERED)
        recalled = recall_for(reader, self.ASKED_LATER)

        prompt = prompts.answer_question(
            question=self.ASKED_LATER, document_excerpts=[],
            standards_excerpts=[], recalled=recalled)

        assert "سليمان" in prompt
        assert "EARLIER CONVERSATIONS" in prompt

    def test_a_new_thread_is_what_makes_it_a_memory_test(self, reader):
        """Asked inside the same thread it would be ordinary history. The
        point is that this crosses from one conversation into another."""
        told_in = AdvisorConversation.objects.create(
            user=reader, title=self.TOLD[:60])
        AdvisorMessage.objects.create(
            conversation=told_in, role=AdvisorMessage.Role.USER, body=self.TOLD)
        AdvisorMessage.objects.create(
            conversation=told_in, role=AdvisorMessage.Role.ASSISTANT,
            body=self.ANSWERED)

        asked_in = AdvisorConversation.objects.create(user=reader, title="سؤال")
        found = recall_for(
            reader, self.ASKED_LATER, exclude_conversation=asked_in.pk)

        assert [item["conversation"] for item in found] == [str(told_in.pk)]

    def test_nobody_else_is_told_who_you_are(self, reader, other_reader):
        """The same question from a colleague finds nothing. A name given to
        the assistant is not a fact about the company."""
        exchange(reader, self.TOLD, self.ANSWERED)
        assert recall_for(other_reader, self.ASKED_LATER) == []

    def test_clearing_the_memory_makes_it_forget(self, client, reader):
        exchange(reader, self.TOLD, self.ANSWERED)
        client.force_login(reader)
        client.post(reverse("analysis:advisor_forget"))

        assert recall_for(reader, self.ASKED_LATER) == []


class TestTheSameMemoryInEnglish:
    """What a reader tells the assistant in English is found from an English
    question later -- and across the language line in both directions. The
    English page is the same memory, not a second one."""

    def test_told_in_english_asked_in_english(self, reader):
        exchange(reader, "My name is Omar and I work as a legal reviewer.",
                 "Nice to meet you, Omar.")
        found = recall_for(reader, "What is my name and my profession?")
        assert found and "Omar" in found[0]["question"]

    def test_the_plain_word_job_is_enough(self, reader):
        exchange(reader, "My name is Omar and I work as a legal reviewer.",
                 "Nice to meet you, Omar.")
        assert recall_for(reader, "what is my job?")

    def test_told_in_arabic_asked_in_english(self, reader):
        exchange(reader, "انا اسمي سليمان مستشار عقود", "أهلاً بك يا سليمان.")
        found = recall_for(reader, "What is my profession?")
        assert found and "سليمان" in found[0]["question"]

    def test_told_in_english_asked_in_arabic(self, reader):
        exchange(reader, "My name is Omar and I work as a legal reviewer.",
                 "Nice to meet you, Omar.")
        found = recall_for(reader, "ما هي وظيفتي؟")
        assert found and "Omar" in found[0]["question"]

    def test_a_contract_question_about_a_name_is_not_about_the_reader(self, reader):
        """"name" and "role" are deliberately not self-words."""
        exchange(reader, "My name is Omar and I work as a legal reviewer.",
                 "Nice to meet you, Omar.")
        assert not recall_for(reader, "What is the name of the client in this contract?")


class TestMixedArabicAndEnglish:
    """Readers mix scripts mid-sentence -- "ما هي مدة ال warranty؟" -- and the
    memory reads each word in its own script. Measured on ten real mixed
    questions: nine found their earlier answer in both languages; the tenth,
    "my" with an Arabic noun, needed the possessive fold to cover it."""

    def test_an_english_word_inside_an_arabic_question(self, reader):
        exchange(reader, "What is the warranty period?", "Twelve months from handover.")
        assert recall_for(reader, "ما هي مدة ال warranty؟")

    def test_an_arabic_phrase_inside_an_english_question(self, reader):
        exchange(reader, "ما سقف المسؤولية؟", "عشرة بالمئة من قيمة العقد.")
        assert recall_for(reader, "what is the سقف المسؤولية in this contract?")

    def test_the_article_glued_onto_an_english_word(self, reader):
        exchange(reader, "What is the liability cap?", "10% of the fee.")
        assert recall_for(reader, "ما هو الliability cap في العقد؟")

    def test_my_with_an_arabic_noun(self, reader):
        exchange(reader, "انا اسمي سليمان مستشار عقود", "أهلاً بك يا سليمان.")
        assert recall_for(reader, "what is my مهنة؟")

    def test_an_arabic_question_word_with_an_english_noun(self, reader):
        exchange(reader, "My name is Omar and I work as a legal reviewer.", "Nice to meet you, Omar.")
        assert recall_for(reader, "شو هي my job؟")

    def test_a_mixed_question_about_the_contract_is_still_about_the_contract(self, reader):
        exchange(reader, "My name is Omar and I work as a legal reviewer.", "Nice to meet you, Omar.")
        assert not recall_for(reader, "what is the name of ال client?")


TOLD_IN_ENGLISH = [
    "My name is Suleiman and I am a contract advisor.",
    "I am Suleiman, contract advisor at AJi.",
    "Suleiman here, I work in contracts.",
    "This is Suleiman. I'm a contracts consultant.",
    "I'm Suleiman from the contracts department",
]
ASKED_IN_ARABIC = ["ما اسمي وما مهنتي؟", "شو اسمي؟", "مين انا؟", "من أنا؟", "ما هي وظيفتي؟",
                   "هل تتذكر اسمي؟", "شو بتعرف عني؟", "عرفني بنفسي"]
TOLD_IN_ARABIC = ["انا اسمي سليمان مستشار عقود", "أنا سليمان من قسم العقود", "معك سليمان، مستشار عقود"]
ASKED_IN_ENGLISH = ["What is my name and my profession?", "Who am I?", "Do you remember my name?",
                    "What do you know about me?", "what is my job?", "Introduce me"]


class TestToldInOneLanguageAskedInTheOther:
    """The reader's own complaint: introduced themselves in English, asked in
    Arabic, and was not remembered. Measured over every pairing of five ways
    to introduce yourself and eight ways to ask -- and the reverse -- so a
    phrasing that slips past the self-detection fails here, not in front of
    the reader."""

    @pytest.mark.parametrize("told", TOLD_IN_ENGLISH, ids=lambda v: v[:24])
    @pytest.mark.parametrize("asked", ASKED_IN_ARABIC, ids=lambda v: v[:16])
    def test_english_then_arabic(self, reader, told, asked):
        exchange(reader, told, "Noted.")
        found = recall_for(reader, asked)
        assert found and told in found[0]["question"]

    @pytest.mark.parametrize("told", TOLD_IN_ARABIC, ids=lambda v: v[:24])
    @pytest.mark.parametrize("asked", ASKED_IN_ENGLISH, ids=lambda v: v[:16])
    def test_arabic_then_english(self, reader, told, asked):
        exchange(reader, told, "أهلاً.")
        found = recall_for(reader, asked)
        assert found and told in found[0]["question"]

    @pytest.mark.parametrize("sentence", [
        "I am asking about the liability cap",
        "أنا بدي أعرف سقف الغرامة",
        "أنا أريد معرفة مدة الضمان",
        "The Consultant shall deliver the design",
        "this is the second draft",
        "Amman here means the city",
    ])
    def test_first_person_in_a_contract_question_is_not_an_introduction(self, reader, sentence):
        """"I am" and "أنا" open questions as often as introductions; only
        the shape "first person + a name" counts."""
        from apps.ai.recall import _fold_phrases

        assert "myself" not in _fold_phrases(sentence)


class TestAFollowUpIsNotAClarificationRequest:
    """"i need more" after an answer is a request to go deeper into it. The
    model was asking what the reader meant, because the prompt told it only
    not to repeat itself."""

    def test_the_prompt_says_what_a_bare_follow_up_means(self):
        from apps.ai import prompts

        text = prompts._conversation([
            {"role": "user", "body": "What are the main risks?"},
            {"role": "assistant", "body": "Scope, payment, liability."},
        ])
        assert "i need more" in text and "المزيد" in text
        assert "go deeper into YOUR LAST ANSWER" in text
        assert "do not ask what they meant" in text


class TestWhatIsNeverCarried:
    def test_nothing_crosses_between_people(self, reader, other_reader):
        """A thread can quote a contract the next reviewer may not see. This
        is a boundary, not a ranking preference."""
        exchange(other_reader, "ما سقف غرامة التأخير؟", "حتى 10%.")
        assert recall_for(reader, "ما سقف غرامة التأخير؟") == []

    def test_the_current_conversation_is_excluded(self, reader):
        """It already travels as `history`; carrying it twice wastes context
        and invites the model to answer the older turn."""
        convo = exchange(reader, "ما سقف غرامة التأخير؟", "حتى 10%.")
        assert recall_for(reader, "غرامة التأخير",
                          exclude_conversation=convo.pk) == []

    def test_a_forgotten_thread_is_not_recalled(self, reader):
        exchange(reader, "ما سقف غرامة التأخير؟", "حتى 10%.", remembered=False)
        assert recall_for(reader, "ما سقف غرامة التأخير؟") == []

    def test_an_empty_question_recalls_nothing(self, reader):
        exchange(reader, "ما سقف غرامة التأخير؟", "حتى 10%.")
        assert recall_for(reader, "") == []
        assert recall_for(reader, "؟؟؟") == []

    def test_stopwords_alone_do_not_match(self, reader):
        """Otherwise every question matches every other one."""
        exchange(reader, "ما هو هذا الذي في العقد؟", "غير واضح.")
        assert recall_for(reader, "ما هو هذا الذي على الجدول؟") == []


class TestTheAnswerShowsItsWorking:
    def test_the_prompt_fences_recalled_material_off(self):
        from apps.ai import prompts

        recalled = [{"question": "س", "answer": "ج", "when": "2026-09-20",
                     "contract": "Jeddah Corniche"}]
        for docs in ([], ["[SOURCE 1] البند 1"]):
            prompt = prompts.answer_question(
                question="q", document_excerpts=docs, standards_excerpts=[],
                recalled=recalled)
            assert "EARLIER CONVERSATIONS" in prompt
            # The rule that stops one contract's terms becoming another's.
            assert "never state anything from them as what this contract says" in prompt
            assert "the document is right" in prompt
            assert "Jeddah Corniche" in prompt

    def test_no_section_when_nothing_was_recalled(self):
        from apps.ai import prompts

        prompt = prompts.answer_question(
            question="q", document_excerpts=[], standards_excerpts=[])
        assert "EARLIER CONVERSATIONS" not in prompt

    def test_what_was_used_is_recorded_on_the_turn(self, reader):
        """Kept as text, so it still reads correctly after the conversation it
        came from is edited or deleted."""
        convo = AdvisorConversation.objects.create(user=reader, title="س")
        message = AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.ASSISTANT, body="ج",
            recalled=[{"question": "س", "answer": "ج", "when": "2026-09-20"}])
        message.refresh_from_db()
        assert message.recalled[0]["question"] == "س"

    def test_it_is_recorded_but_no_longer_shown(self, client, reader):
        """The panel under each answer was taken out at the reader's request.

        What it displayed is still written down, so which answers leaned on
        what is answerable from the database -- only the disclosure is gone.
        Asserted both ways round, because "we still record it" is the half
        that would rot silently if the field stopped being populated.
        """
        convo = AdvisorConversation.objects.create(user=reader, title="س")
        message = AdvisorMessage.objects.create(
            conversation=convo, role=AdvisorMessage.Role.ASSISTANT, body="الإجابة",
            recalled=[{"question": "سؤال سابق", "answer": "إجابة سابقة",
                       "when": "2026-09-20", "contract": "Jeddah"}])

        client.force_login(reader)
        body = client.get(
            reverse("analysis:advisor") + f"?c={convo.pk}").content.decode()

        assert "الإجابة" in body, "the answer itself should still render"
        assert "سؤال سابق" not in body
        assert "cites--recall" not in body

        message.refresh_from_db()
        assert message.recalled[0]["question"] == "سؤال سابق"
        assert message.recalled[0]["contract"] == "Jeddah"


class TestForgetting:
    def test_clearing_stops_recall_without_deleting_anything(self, client, reader):
        convo = exchange(reader, "ما سقف غرامة التأخير؟", "حتى 10%.")
        client.force_login(reader)

        client.post(reverse("analysis:advisor_forget"))

        convo.refresh_from_db()
        assert convo.remembered is False
        assert AdvisorConversation.objects.filter(pk=convo.pk).exists()
        assert AdvisorMessage.objects.filter(conversation=convo).count() == 2
        assert recall_for(reader, "غرامة التأخير") == []

    def test_it_clears_only_your_own(self, client, reader, other_reader):
        mine = exchange(reader, "سؤالي عن الغرامة", "جوابي")
        theirs = exchange(other_reader, "سؤالهم عن الغرامة", "جوابهم")

        client.force_login(reader)
        client.post(reverse("analysis:advisor_forget"))

        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.remembered is False
        assert theirs.remembered is True

    def test_get_clears_nothing(self, client, reader):
        convo = exchange(reader, "سؤال عن الغرامة", "جواب")
        client.force_login(reader)

        response = client.get(reverse("analysis:advisor_forget"))
        convo.refresh_from_db()
        assert response.status_code == 405
        assert convo.remembered is True

    def test_signing_in_is_required(self, client, reader):
        convo = exchange(reader, "سؤال عن الغرامة", "جواب")
        response = client.post(reverse("analysis:advisor_forget"))
        convo.refresh_from_db()
        assert convo.remembered is True
        assert response.status_code == 302

    def test_it_is_recorded(self, client, reader):
        from apps.audit.models import AuditAction, AuditEvent

        exchange(reader, "سؤال عن الغرامة", "جواب")
        client.force_login(reader)
        client.post(reverse("analysis:advisor_forget"))

        assert AuditEvent.objects.filter(
            action=AuditAction.ADVISOR_MEMORY_CLEARED, actor=reader).exists()


class TestTheAnswerComesBackInTheLanguageOfTheQuestion:
    """Typing English and getting Arabic back reads as "it did not understand
    me". The interface language is only the fallback."""

    def test_english_question(self):
        from apps.ai.prompts import language_of
        assert language_of("What are the main risks in consultancy contracts?", default="ar") == "en"

    def test_arabic_question(self):
        from apps.ai.prompts import language_of
        assert language_of("ما أبرز المخاطر في عقود الاستشارات؟", default="en") == "ar"

    def test_a_mixed_question_uses_the_interface_language(self):
        from apps.ai.prompts import language_of
        assert language_of("ما هي مدة ال warranty period في العقد؟", default="ar") == "ar"
        assert language_of("what is the سقف المسؤولية in this contract?", default="en") == "en"

    def test_an_arabic_question_naming_aji_is_still_arabic(self):
        from apps.ai.prompts import language_of
        assert language_of("ما موقف AJi من سقف غرامات التأخير؟", default="en") == "ar"

    def test_the_advisor_passes_it_through(self, client, reader, monkeypatch):
        from django.urls import reverse
        from apps.ai.services import ai_service
        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            from apps.ai.providers.base import AIProviderUnavailable
            raise AIProviderUnavailable("not now")

        monkeypatch.setattr(ai_service, "answer_question", fake)
        reader.preferred_language = "ar"; reader.save(update_fields=["preferred_language"])
        contract = _analysed_file(reader)
        client.force_login(reader)
        client.post(reverse("analysis:advisor"),
                    {"question": "What is the liability cap?", "contract": contract.pk})
        assert seen["language"] == "en"


class TestWhenTwoIntroductionsDisagree:
    """Seen in the live deep test: a reader introduced as a contract advisor
    in one thread and a legal reviewer in a later one; the model merged the
    two and answered with the older role. The most recent statement about
    the reader is the current one, and the prompt has to say so."""

    def test_the_prompt_says_the_latest_wins(self):
        from apps.ai import prompts

        text = prompts._recalled([
            {"when": "2026-09-20", "question": "I am Omar, a contract advisor", "answer": "Hello Omar"},
            {"when": "2026-09-27", "question": "I am Omar, now a legal reviewer", "answer": "Noted"},
        ])
        assert "MOST RECENT" in text and "later date" in text

    def test_recalled_rows_are_listed_oldest_first_with_the_time(self):
        """Same-day introductions could not be told apart by date alone."""
        from apps.ai import prompts

        text = prompts._recalled([
            {"when": "2026-09-27 14:10", "question": "I am Omar, now a legal reviewer", "answer": "Noted"},
            {"when": "2026-09-27 09:02", "question": "I am Omar, a contract advisor", "answer": "Hello Omar"},
        ])
        assert text.index("contract advisor") < text.index("legal reviewer")
        assert "oldest first" in text
