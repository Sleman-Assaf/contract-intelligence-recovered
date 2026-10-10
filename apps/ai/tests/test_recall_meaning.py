"""Matching a question against an earlier one by what it is about.

"ما مدة الضمان؟" and "كم فترة الكفالة؟" are one question asked twice. They
share no word at all, so anything comparing strings finds nothing, and the
reader gets no memory of having asked.

── How the pairs below were arrived at ──────────────────────────────────────

They are the measurement, not an illustration of it. The first set was written
first and the concept table tuned against it, which took it to 18 of 20 -- and
a second set, written afterwards and scored once, got 2 of 8. The tuning had
taught the table those twenty pairs and nothing else.

What fixed it was not more vocabulary but a second way of scoring. Coverage of
the wording asks "how much of this question does that one repeat", which is
right for search and wrong here: relatedness usually rests on one word, and
dividing it by everything else asked around it buries the match. Scoring the
subjects two questions share as well, and taking whichever reads higher, took
the unseen set from 2 of 8 to 7 of 8 with no new words added.

Both sets are kept. Neither is held out any more, so treat a change that
raises these numbers with suspicion unless it also raises numbers measured
somewhere else.
"""
from __future__ import annotations

import pytest

from apps.ai.recall import MIN_SCORE, _score, _terms, informativeness

#: Pairs a reviewer would say are the same question.
RELATED = [
    ("ما مدة الضمان في العقد؟", "كم فترة الكفالة المعتادة؟"),
    ("ما سقف المسؤولية؟", "هل هناك حد أقصى للالتزامات؟"),
    ("متى يمكن إنهاء العقد؟", "ما شروط فسخ الاتفاقية؟"),
    ("ما غرامة التأخير؟", "كيف تُحتسب الجزائية عند التأخر؟"),
    ("What is the liability cap?", "ما سقف المسؤولية في العقد؟"),
    ("what is my job?", "My name is Omar and I work as a legal reviewer."),
    ("What is my profession?", "انا اسمي سليمان مستشار عقود"),
    ("How much can the client charge for late delivery?", "ما سقف غرامات التأخير عند AJi؟"),
    # Broken plurals the light stemmer cannot reach: they need table entries.
    ("ما هي المواعيد النهائية للتسليم؟", "ما جدول التسليم؟"),
    ("من يصدر شهادات الإنجاز؟", "ما إجراءات القبول والتسليم؟"),
    ("هل الخسائر مشمولة بالتعويض؟", "ما بنود التعويض عن الأضرار؟"),
    ("ما آجال تنفيذ العقد؟", "ما مدة العقد؟"),
    ("ما آلية الدفع؟", "متى تُسدد الفواتير المستحقة؟"),
    ("هل يوجد بند تحكيم؟", "كيف تُحل النزاعات بين الطرفين؟"),
    ("ما حقوق الملكية الفكرية؟", "من يملك حقوق التأليف للتصاميم؟"),
    ("ما قيمة العقد الإجمالية؟", "كم المبلغ الإجمالي للمشروع؟"),
    ("warranty period for the works", "ما مدة الصيانة للأعمال؟"),
    ("ما بنود السرية؟", "هل هناك التزام بعدم الإفشاء؟"),
    ("هل التأمين مطلوب؟", "ما تغطية بوليصة التأمين؟"),
    ("ما الحد الأقصى للتعويض؟", "كم سقف التعويضات المستحقة؟"),
    ("هل يمكن تمديد العقد؟", "ما شروط التجديد بعد انتهاء المدة؟"),
    ("who owns the design copyright?", "لمن تعود ملكية التصاميم؟"),
    ("ما إجراءات الإشعار؟", "كيف يتم الإخطار الرسمي للطرف الآخر؟"),
    ("هل هناك تأمين مسؤولية مهنية؟", "ما بوليصة التأمين المطلوبة؟"),
    ("ما عقوبة التأخر في التسليم؟", "كم غرامة التأخير؟"),
    ("هل يحق للاستشاري التنازل عن العقد؟", "ما شروط حوالة الحقوق؟"),
    ("ما القانون الواجب التطبيق؟", "أي جهة لها الاختصاص القضائي؟"),
    # Written after the tuning stopped.
    ("ما شروط إنهاء الاتفاقية مبكراً؟", "متى يجوز فسخ العقد؟"),
    ("كم مدة سريان العرض؟", "ما فترة صلاحية العرض؟"),
    ("هل تغطي البوليصة الأضرار؟", "ما حدود التغطية التأمينية؟"),
    ("من يتحمل تكاليف التأخير؟", "من يدفع غرامات التأخر؟"),
    ("هل يمكن تعديل نطاق الأعمال؟", "ما شروط أوامر التغيير؟"),
    ("ما التزامات السرية بعد الانتهاء؟", "هل يستمر بند عدم الإفشاء؟"),
    ("what is the retention percentage?", "ما نسبة المبلغ المحتجز؟"),
    ("ما التزامات السرية بعد الانتهاء من الأعمال؟", "هل يستمر بند عدم الإفشاء؟"),
    # Added when the table was widened; "سلفة" used to be the standing example
    # of a word it did not know.
    ("ما نسبة الدفعة المقدمة؟", "كم قيمة السلفة عند التوقيع؟"),
    ("متى يسقط حق المطالبة؟", "ما مهلة تقديم المطالبات؟"),
    ("هل هناك إخلال جوهري؟", "ما نتائج التقصير في التنفيذ؟"),
    ("ما قيمة خطاب الضمان البنكي؟", "كم نسبة السند المصرفي المطلوب؟"),
    ("من يصدر أوامر التغيير؟", "كيف تُعتمد الأعمال الإضافية؟"),
    ("ما شروط التسليم الابتدائي؟", "متى تُصدر شهادة الإنجاز؟"),
    # Written after the widening, scored once: 5 of 5, no new false matches.
    ("ما غرامة الإخلال بالعقد؟", "ما جزاء المخالفة؟"),
    ("كم قسط التأمين؟", "ما تكلفة بوليصة التغطية؟"),
    ("من هو المقاول من الباطن؟", "هل يُسمح بموردين ثانويين؟"),
    ("ما إجراءات فحص الأعمال؟", "كيف تتم معاينة واختبار التنفيذ؟"),
    ("متى تنتهي صلاحية العرض؟", "ما مدة سريان التسعير؟"),
]

#: Pairs that must not be carried into each other's answers. Several share a
#: word or a neighbouring idea on purpose: a contract question that mentions
#: "العقد" or "بند" is every contract question.
UNRELATED = [
    ("ما مدة الضمان؟", "من هو ممثل العميل؟"),
    ("ما غرامة التأخير؟", "ما حقوق الملكية الفكرية؟"),
    ("كيف أرفع عقداً جديداً؟", "ما سقف المسؤولية؟"),
    ("ما بنود السرية؟", "ما قيمة العقد الإجمالية؟"),
    ("اشرح بند التحكيم المؤسسي", "ما مدة الضمان؟"),
    ("ما مدة العقد؟", "ما جدول الدفعات؟"),
    ("من يوقع العقد؟", "ما نسبة الضريبة؟"),
    ("ما هي مواصفات التصميم؟", "متى ينتهي التأمين؟"),
    ("من يوقع العقد؟", "من يعتمد أوامر التغيير؟"),
    ("ما نسبة الضريبة؟", "ما عملة العقد؟"),
    ("ما مواصفات التصميم؟", "ما جدول التسليم؟"),
    ("هل يحق لنا التنازل عن العقد؟", "ما مدة الضمان؟"),
    ("ما سلامة الموقع؟", "ما قيمة الدفعة المقدمة؟"),
    ("من هو مدير المشروع؟", "ما معايير الجودة؟"),
    ("ما لغة العقد؟", "ما مدة الضمان؟"),
    ("من هو المستفيد؟", "ما جدول الدفعات؟"),
    ("ما عنوان المشروع؟", "ما سقف المسؤولية؟"),
    ("هل التصاميم معتمدة؟", "ما نسبة الضريبة؟"),
    ("كم عدد الموظفين؟", "ما القانون الواجب التطبيق؟"),
    ("ما رقم العقد؟", "هل هناك تأمين؟"),
    # Aimed at the concepts added when the table was widened -- a wider table
    # is a wider chance of joining two things that do not belong together.
    ("هل هناك إخلال جوهري؟", "ما عملة العقد؟"),
    ("من هو ممثل العميل؟", "ما مواصفات التصميم؟"),
    ("ما سلامة الموقع؟", "من يوقع العقد؟"),
    ("ما مطالبات المقاول؟", "ما لغة العقد؟"),
    ("متى يبدأ سريان العقد؟", "ما نسبة الضريبة؟"),
    ("ما خبرة الفريق المقترح؟", "ما جدول الدفعات؟"),
    ("ما موقع المشروع؟", "ما مدة الضمان؟"),
    ("من يملك التصاميم؟", "ما إجراءات الإشعار؟"),
    ("هل يوجد تأمين على الموقع؟", "كيف تُعتمد أوامر التغيير؟"),
    ("ما مؤشرات الأداء المطلوبة؟", "ما عملة الدفع؟"),
    ("من يصدر شهادة الإنجاز؟", "ما قانون التحكيم؟"),
    ("ما مهلة تقديم المطالبات؟", "من هم المهندسون المقترحون؟"),
]

#: Word weighting comes from the reader's own messages, so the fixture supplies
#: a corpus of its own rather than scoring each pair in a vacuum.
WEIGHTS = informativeness([text for pair in RELATED + UNRELATED for text in pair])


def score(question: str, earlier: str) -> float:
    return _score(_terms(question), earlier, WEIGHTS)


@pytest.mark.parametrize("question,earlier", RELATED,
                         ids=lambda v: str(v)[:34])
def test_the_same_question_in_other_words_is_found(question, earlier):
    found = score(question, earlier)
    assert found >= MIN_SCORE, (
        f"{found:.2f} < {MIN_SCORE}: asked {question!r}, would not recall "
        f"{earlier!r} -- a word or an idea is missing from apps/ai/concepts.py"
    )


@pytest.mark.parametrize("question,earlier", UNRELATED,
                         ids=lambda v: str(v)[:34])
def test_a_different_question_is_left_alone(question, earlier):
    """The expensive direction. A miss costs the reader a convenience; a wrong
    match puts another contract's exchange in front of the model."""
    found = score(question, earlier)
    assert found < MIN_SCORE, (
        f"{found:.2f} >= {MIN_SCORE}: asked {question!r}, would recall the "
        f"unrelated {earlier!r} -- a concept in apps/ai/concepts.py is too broad"
    )


def test_the_threshold_is_not_balanced_on_a_knife_edge():
    """A threshold that only works at one value is a number fitted to this
    fixture rather than a property of the scoring."""
    weakest = min(score(q, e) for q, e in RELATED)
    strongest = max(score(q, e) for q, e in UNRELATED)
    assert strongest < MIN_SCORE <= weakest, (
        f"related pairs bottom out at {weakest:.2f} and unrelated ones reach "
        f"{strongest:.2f}; the threshold {MIN_SCORE} has no room between them"
    )
    assert weakest - strongest >= 0.10, (
        f"only {weakest - strongest:.2f} separates the weakest real match from "
        f"the strongest false one -- too little to survive new vocabulary"
    )


class TestWhyItWorks:
    """The two halves of the score, each shown failing without the other."""

    def test_shared_wording_alone_finds_the_reworded_question(self):
        """Nothing in common but the subject: no word of "غرامات التأخر"
        appears in "تكاليف التأخير" except through the concept table."""
        assert score("من يتحمل تكاليف التأخير؟",
                     "من يدفع غرامات التأخر؟") >= MIN_SCORE

    def test_a_shared_subject_survives_a_long_question(self):
        """One topical word among four is still the topic. Coverage of the
        wording divides it away; coverage of the subject does not."""
        assert score("ما التزامات السرية بعد الانتهاء من الأعمال؟",
                     "هل يستمر بند عدم الإفشاء؟") >= MIN_SCORE

    def test_the_words_of_every_contract_question_are_discounted(self):
        """"عقد" and "بند" are in all of them and identify none of them."""
        from apps.ai.recall import _base_weight, _senses

        _forms, meanings = _senses("العقد")
        assert _base_weight(meanings) < 1.0
        _forms, meanings = _senses("التحكيم")
        assert _base_weight(meanings) == 1.0

    def test_a_word_it_has_never_seen_is_not_guessed_at(self):
        """The table is bounded and always will be, whatever is added to it.
        "مقايسة" is a bill of quantities and is not in it, so this does not
        match -- silence being the better failure, since a guess puts the
        wrong exchange in front of the model.

        When recall misses something it should have found, this is the shape
        of it, and apps/ai/concepts.py is where the fix goes.
        """
        from apps.ai.recall import _senses

        _forms, meanings = _senses("مقايسة")
        assert not meanings, "مقايسة is in the table now; pick another word"
        assert score("ما تفاصيل المقايسة؟",
                     "كيف تُحتسب كميات البنود؟") < MIN_SCORE
