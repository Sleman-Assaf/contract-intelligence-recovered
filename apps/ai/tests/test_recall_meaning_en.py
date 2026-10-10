"""Does the memory understand English the way it understands Arabic?

The Arabic side of the recall engine folds spellings, strips the prepositions
Arabic glues onto nouns, strips regular endings, and maps ~600 words onto 47
ideas. The English side was lowercase-only with a thinner vocabulary, so a
reader who wrote in English got a worse memory than one who wrote in Arabic
-- from the same code, on the same page.

Same method as `test_recall_meaning.py`: pairs a reviewer would call the same
question, pairs they would not, and one threshold that has to separate them
with room to spare. The pairs were written before the English side was
improved and were not tuned to it -- a failure here means a word or an idea
is missing, not that the number should move.
"""
from __future__ import annotations

import pytest

from apps.ai.recall import MIN_SCORE, _score, _terms, informativeness

#: The same question, in other English words -- and some across the language
#: line, which is the whole point of a shared concept table.
RELATED = [
    ("What is the warranty period?", "How long is the defects liability period?"),
    ("Is there a cap on liability?", "What is the maximum the consultant can be liable for?"),
    ("When can the client terminate the contract?", "What are the termination conditions?"),
    ("What is the delay penalty?", "How are liquidated damages for late completion calculated?"),
    ("What are the payment terms?", "When are invoices due?"),
    ("Is there an arbitration clause?", "How are disputes resolved?"),
    ("Who owns the intellectual property?", "Who holds the copyright in the drawings?"),
    ("What insurance is required?", "What professional indemnity cover must we carry?"),
    ("What is the retention percentage?", "How much is withheld from each payment?"),
    ("What is the scope of work?", "Which services are included?"),
    ("Can we subcontract the work?", "Is subcontracting permitted?"),
    ("What happens under force majeure?", "Are we excused for events beyond our control?"),
    ("How are variations approved?", "What is the change order procedure?"),
    ("What is the governing law?", "Which jurisdiction applies to the agreement?"),
    ("What are the confidentiality obligations?", "Can we disclose project information?"),
    ("What is the advance payment?", "Is there a mobilisation payment up front?"),
    ("When does the contract start?", "What is the commencement date?"),
    ("What is the notice period?", "How much notice must be given?"),
    ("Who approves the deliverables?", "What is the acceptance procedure for the design?"),
    ("What is the performance bond amount?", "What bank guarantee do we have to provide?"),
    # Across languages: one idea, two scripts.
    ("What is the warranty period?", "ما مدة الضمان في العقد؟"),
    ("What is the liability cap?", "ما سقف المسؤولية؟"),
    ("What is the delay penalty?", "ما غرامة التأخير؟"),
    ("Is there an arbitration clause?", "هل يوجد بند تحكيم؟"),
    ("What are the payment terms?", "ما آلية الدفع؟"),
    ("Who owns the intellectual property?", "من يملك حقوق الملكية الفكرية؟"),
    ("Can the contract be terminated early?", "متى يمكن إنهاء العقد؟"),
    ("What insurance is required?", "ما التأمين المطلوب؟"),
]

#: Different questions that happen to share a word or a neighbouring idea.
UNRELATED = [
    ("What is the warranty period?", "Who is the client's representative?"),
    ("What is the delay penalty?", "Who owns the intellectual property?"),
    ("How do I upload a new contract?", "What is the liability cap?"),
    ("What are the confidentiality obligations?", "What is the total contract value?"),
    ("Explain the arbitration clause", "What is the warranty period?"),
    ("What is the contract duration?", "What is the payment schedule?"),
    ("Who signs the contract?", "What is the tax rate?"),
    ("What are the design specifications?", "When does the insurance expire?"),
    ("Who signs the contract?", "Who approves change orders?"),
    ("What is the tax rate?", "What is the contract currency?"),
    ("What are the design specifications?", "What is the delivery schedule?"),
    ("Can we assign the contract?", "What is the warranty period?"),
    ("What are the site safety rules?", "What is the advance payment?"),
    ("Who is the project manager?", "What are the quality standards?"),
    ("What language is the contract in?", "What is the warranty period?"),
    ("Who is the beneficiary?", "What is the payment schedule?"),
    ("What is the project address?", "What is the liability cap?"),
    ("Are the designs approved?", "What is the tax rate?"),
    ("How many staff are proposed?", "What is the governing law?"),
    ("What is the contract number?", "Is there insurance?"),
    ("Is there a material breach?", "What is the contract currency?"),
    ("Where is the site?", "What is the warranty period?"),
    ("Who owns the designs?", "What is the notice procedure?"),
    ("What are the performance indicators?", "What is the payment currency?"),
    ("Who issues the completion certificate?", "What is the arbitration law?"),
    # Across languages, still unrelated.
    ("What is the delay penalty?", "من هو ممثل العميل؟"),
    ("What is the tax rate?", "ما عملة العقد؟"),
    ("Who signs the contract?", "ما نسبة الضريبة؟"),
]

WEIGHTS = informativeness([text for pair in RELATED + UNRELATED for text in pair])


def score(question: str, earlier: str) -> float:
    return _score(_terms(question), earlier, WEIGHTS)


@pytest.mark.parametrize("question,earlier", RELATED, ids=lambda v: str(v)[:34])
def test_the_same_question_in_other_words_is_found(question, earlier):
    found = score(question, earlier)
    assert found >= MIN_SCORE, (
        f"{found:.2f} < {MIN_SCORE}: asked {question!r}, would not recall "
        f"{earlier!r} -- a word or an idea is missing from apps/ai/concepts.py"
    )


@pytest.mark.parametrize("question,earlier", UNRELATED, ids=lambda v: str(v)[:34])
def test_a_different_question_is_left_alone(question, earlier):
    found = score(question, earlier)
    assert found < MIN_SCORE, (
        f"{found:.2f} >= {MIN_SCORE}: asked {question!r}, would wrongly recall "
        f"{earlier!r}"
    )


def test_the_threshold_is_not_balanced_on_a_knife_edge():
    weakest = min(score(q, e) for q, e in RELATED)
    strongest = max(score(q, e) for q, e in UNRELATED)
    assert strongest < MIN_SCORE <= weakest, (
        f"related pairs bottom out at {weakest:.2f} and unrelated ones reach "
        f"{strongest:.2f}; the threshold {MIN_SCORE} has no room between them"
    )


class TestEnglishIsReadLikeArabic:
    """The specific mechanics the Arabic side has and the English side lacked."""

    def test_a_plural_finds_its_singular(self):
        assert score("What are the penalties for delay?", "What is the delay penalty?") >= MIN_SCORE

    def test_ies_plural(self):
        assert score("Are there any warranties?", "What is the warranty period?") >= MIN_SCORE

    def test_a_verb_form_finds_its_noun(self):
        assert score("Can they terminate early?", "What are the termination conditions?") >= MIN_SCORE

    def test_ing_form(self):
        assert score("Rules on subcontracting?", "Can we subcontract the work?") >= MIN_SCORE

    def test_english_stopwords_do_not_match(self):
        assert score("what is it about", "is that all there is") < MIN_SCORE
