"""Finding a reader's earlier exchanges that bear on the question in front of
them (§38).

Retrieval, not a stored profile. Nothing is summarised and nothing new is
written: when a question arrives this searches what the reader has already
asked, and the few closest exchanges travel with the prompt. If nothing
matches, the prompt is exactly what it was before this module existed.

That choice is about cost as much as safety. The model service answers around
0.7 requests a second; a profile would need a second call on every exchange to
maintain, which is a second call the queue does not have.

── What is deliberately not done ────────────────────────────────────────────

*Nothing crosses between people.* Every query is filtered to the asker. A
thread can quote a contract the next reviewer is not cleared to see, so this
is a hard boundary and not a ranking preference.

*Nothing recalled is allowed to answer as the current contract.* An exchange
about one agreement surfacing while somebody asks about another is the one way
this feature could produce the failure the whole system exists to prevent: a
fluent, confident answer attributing one contract's terms to a different one.
The excerpts are labelled as belonging to other conversations, the prompt
forbids treating them as the attached document, and every recalled item is
recorded on the turn so a reader can see what was used.
"""
from __future__ import annotations

import math
import re

from . import concepts

#: Exchanges carried at most. Few, and short: the prompt already holds the
#: standards library and the current thread, and the service silently drops
#: the head of anything too long -- which is where the instructions live.
MAX_EXCHANGES = 4
MAX_CHARS = 420

#: How far back to look. Older than this and a match is usually coincidence.
CANDIDATE_LIMIT = 300

#: Words too common to say anything about what a question is about. Arabic and
#: English, since questions arrive in both and often mixed.
_STOPWORDS = {
    # English
    "the", "and", "for", "are", "was", "with", "this", "that", "from", "what",
    "which", "how", "does", "did", "can", "should", "would", "about", "into",
    "our", "their", "its", "has", "have", "had", "not", "you", "your", "any",
    "all", "but", "who", "when", "where", "why", "there", "here", "than",
    "will", "may", "must", "been", "being", "they", "them", "these", "those",
    "also", "just", "some", "much", "many", "each", "every", "please", "tell",
    "give", "need", "want", "know", "under", "over", "between", "within",
    "without", "more", "most", "very", "only", "such", "then", "were", "what's",
    "explain", "happens", "happen", "required", "require", "requires", "get",
    "use", "used", "using", "let", "say", "see", "one", "two", "still",
    # Arabic
    "من", "في", "على", "عن", "إلى", "الى", "هذا", "هذه", "ذلك", "التي", "الذي",
    "ما", "ماذا", "هل", "كيف", "متى", "أين", "لماذا", "أو", "او", "ثم", "كان",
    "يكون", "بين", "مع", "كل", "بعد", "قبل", "عند", "لا", "نعم", "هو", "هي",
    "هم", "قد", "لكن", "حتى", "إذا", "اذا", "أن", "ان", "إن", "لدى", "منها",
    "يمكن", "يوجد", "توجد", "هناك", "يجب", "هل", "أي", "اي", "نفس", "غير",
    "آلية", "اليه", "طريقة", "كيفية", "بين", "خلال", "وفق", "حسب", "بشأن",
    "يحق", "يجوز", "يتم", "تتم", "تعود", "لمن", "كم", "واجب", "الآخر", "آخر",
    "الذين", "التى", "عليه", "عليها", "بها", "به", "فيه", "فيها", "ذلك",
}

#: Letters, digits and the diacritics that sit inside a word. Deliberately
#: not the whole Arabic block: it holds "؟" and "،" as well as letters, and a
#: range that swallows punctuation glues it onto the word beside it, so
#: "الاستشارات؟" and "الاستشارات" stop being the same word.
_WORD = re.compile(r"[0-9A-Za-zء-ْ٠-٩ٰٱە]+")
#: Any Arabic letter: decides which set of endings a word is read with.
_ARABIC = re.compile(r"[ء-ي]")

#: Tashkeel and tatweel. Optional in writing and inconsistently typed, so
#: "تُحتسب" and "تحتسب" must fold together.
_MARKS = re.compile(r"[ً-ْـٰ]")

#: A preposition or conjunction written onto the front of the noun, longest
#: first. Arabic glues these on where English keeps them separate, so
#: "لغرامة" is "for a penalty" -- one word to the writer, and the same noun
#: to the reader.
_PROCLITICS = ("وال", "بال", "كال", "فال", "لل", "ال", "و", "ف", "ب", "ل", "ك")

#: English said about oneself. Arabic carries "my" inside the word -- "اسمي",
#: "وظيفتي" -- and those words sit in the "self" concept; English keeps the
#: possessive separate, so "my name" and "I work as" have no single word to
#: put there. "name" and "work" alone cannot go in: "the name of the client"
#: and "scope of work" are contract questions. So the phrase is folded to one
#: token before the words are read, and the plain word stays what it was.
_SELF_PHRASES = re.compile(
    r"\b(?:my (?:name|job|role|profession|occupation|title|position|work"
    # Mixed script: "my مهنة" -- the English possessive with the Arabic noun,
    # which carries no suffix to say whose it is.
    r"|مهنة|وظيفة|اسم|تخصص|منصب|خبرة|دور|شغل|عمل)"
    r"|i (?:work|am working) (?:as|in|at|for)|i am an?|i'm an?|who am i|about me"
    r"|introduce me|remember me|my name's"
    # And the other way round: an Arabic question word with the English noun.
    r"|(?:ما|شو|ايش|وش|وين) (?:هي |هو )?(?:my )?(?:job|profession|occupation|name|role))\b",
    re.IGNORECASE,
)

#: An introduction by name, in either language. "I am Suleiman, contract
#: advisor" and "أنا سليمان من قسم العقود" say who the reader is without
#: one word from the "self" concept: the signal is the first person followed
#: by a name. English names are capitalised; Arabic ones are not, so the
#: Arabic form asks for what follows the name instead -- a comma, "من", or a
#: title -- which keeps "أنا بدي أعرف الغرامة" (I want to know the penalty)
#: what it is: a question, not an introduction.
_INTRO_PHRASES = re.compile(
    r"(?:\b(?:[Ii] am|[Ii]'m|[Tt]his is|[Ii]t's|[Hh]ere is) [A-Z][a-z]+\b"
    r"|(?:^|\s)(?:أنا|انا|معك|معكم|هنا) \S+(?: \S+)? ?(?:،|,|-|—|من قسم|من فريق|من شركة|مستشار|مهندس|مدير|محامي|مراجع|رئيس|مسؤول)"
    r"|(?:^|\s)(?:من|مين) (?:انا|أنا)\b"
    r"|\b(?:عني|بنفسي)\b)",
)


def _fold_phrases(text: str) -> str:
    text = _SELF_PHRASES.sub(" myself ", text)
    return _INTRO_PHRASES.sub(" myself ", text)


def _fold(raw: str) -> str:
    """One spelling of a word, whatever was typed.

    Hamza and ta-marbuta are written inconsistently even by careful writers.
    """
    word = _MARKS.sub("", raw.lower())
    return (word.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
                .replace("ة", "ه").replace("ى", "ي"))


#: Folded like everything else, so the "ى" of "على" does not hide the entry.
_STOPWORDS = {_fold(word) for word in _STOPWORDS}


def _forms(raw: str) -> frozenset[str]:
    """A word and, if it carries a prefix, the noun underneath it.

    Both are kept rather than the stripped form alone. Stripping is a guess --
    the "ل" of "لجنة" is part of the word, not a preposition -- and keeping
    both means a wrong guess adds a spurious form instead of destroying the
    real one. A stray form can only cost a little precision; a destroyed one
    costs the match entirely.
    """
    word = _fold(raw)
    if len(word) < 3:
        return frozenset()
    forms = {word}
    for prefix in _PROCLITICS:
        rest = word[len(prefix):]
        if word.startswith(prefix) and (len(rest) >= 3 or rest in _STOPWORDS):
            forms.add(rest)
            break
    return frozenset(forms)


#: Regular Arabic endings. Stripping them catches "تعويضات" against
#: "تعويض" without an entry for every plural in the concept table. The
#: stem is added beside the word, never instead of it, so a wrong strip costs
#: a spare form rather than the real one.
_SUFFIXES = ("ات", "ية", "ين", "ون", "ان", "ها", "هم", "هن", "كم", "نا", "ه")


#: Regular English endings, longest first. "warranties" and "warranty",
#: "subcontracting" and "subcontract", "terminated" and "terminate" are one
#: word to a reader; without this each spelling needed its own table entry
#: and the English memory was measurably worse than the Arabic one.
_LATIN_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies",
                   "ied", "ness", "ers", "ing", "ed", "es", "s", "e")


def _latin_stems(word: str) -> set[str]:
    """Candidate stems for a Latin word. Several, never one: "termination"
    and "terminate" only meet at "terminat", which neither reaches by a single
    rule, so each spelling contributes every stem it plausibly has."""
    out = set()
    for suffix in _LATIN_SUFFIXES:
        if not word.endswith(suffix):
            continue
        base = word[: -len(suffix)]
        if len(base) < 4:
            continue
        if suffix in ("ies", "ied"):
            out.add(base + "y")
        elif suffix in ("ation", "ations"):
            out.add(base + "ate")
            out.add(base + "at")
        else:
            out.add(base)
            if suffix in ("ing", "ings", "ed", "ers", "es"):
                out.add(base + "e")            # terminating -> terminate
        # A doubled consonant before -ing/-ed: "planning" -> "plan".
        if suffix in ("ing", "ed") and len(base) >= 4 and base[-1] == base[-2]:
            out.add(base[:-1])
    # Reach the shared stem from the other side too: "terminate" -> "terminat".
    out |= {s[:-1] for s in set(out) | {word} if s.endswith("e") and len(s) > 5}
    return out


def _stems(word: str) -> set[str]:
    """`word` and, if it ends in a regular suffix, the stem under it."""
    out = {word}
    if _ARABIC.search(word) is None:
        return out | _latin_stems(word)
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            out.add(word[: -len(suffix)])
            break
    return out


#: word -> concepts, built once from the table using this module's folding.
_CONCEPT_OF = concepts.reverse_index(_fold)


def _senses(raw: str) -> tuple[frozenset[str], frozenset[str]]:
    """One word as (spellings, concepts).

    Two sets rather than one because they are not worth the same: sharing a
    word is stronger evidence than sharing an idea, and a scorer that cannot
    tell them apart has to treat "الضمان" and "الكفالة" as either identical or
    unrelated. Neither is true.
    """
    forms = set()
    for form in _forms(raw):
        forms |= _stems(form)
    if not forms:
        return frozenset(), frozenset()
    meanings: set[str] = set()
    for form in forms:
        meanings |= _CONCEPT_OF.get(form, frozenset())
    return frozenset(forms), frozenset(meanings)


def _canonical(forms: frozenset[str]) -> str:
    """One name for a word, whatever prefix or ending it was written with.

    The shortest form: stripping only ever removes an affix, so the shortest
    is the stem every spelling of the word reduces to.
    """
    return min(forms, key=lambda form: (len(form), form))


def _terms(text: str) -> set[tuple[frozenset[str], frozenset[str]]]:
    """What the text is about: one entry per word, with its spellings and
    the ideas it belongs to.

    One entry per word, not per spelling, so a word that happens to carry a
    prefix does not weigh twice as much as one that does not.
    """
    out = set()
    for raw in _WORD.findall(_fold_phrases(text)):
        forms, meanings = _senses(raw)
        # "وكيف" is "كيف" with a conjunction on it, and says as little.
        if forms and not (forms & _STOPWORDS):
            out.add((forms, meanings))
    return out


def _keys(text: str) -> tuple[set[str], set[str]]:
    """Everything `text` could be matched on: spellings, and concepts."""
    forms: set[str] = set()
    meanings: set[str] = set()
    for raw in _WORD.findall(_fold_phrases(text)):
        word_forms, word_meanings = _senses(raw)
        forms |= word_forms
        meanings |= word_meanings
    return forms, meanings


#: What a shared idea is worth against a shared word. Below 1 because the
#: table is deliberately broad -- "مدة" and "جدول" are both about time without
#: being the same question -- so a match built only from concepts needs more
#: of them to clear the threshold than a match built from the words.
CONCEPT_WEIGHT = 0.6

#: What sharing a subject is worth against repeating the words. Just under 1:
#: two questions on the same subject are usually related and occasionally only
#: adjacent, so a subject match should clear the threshold without outranking
#: an exchange that asked the very same thing.
TOPIC_WEIGHT = 0.85


#: Words that only ever say "this is about a contract" -- "عقد", "بند",
#: "مادة", "شرط". Every question a contract reviewer asks contains one, so
#: they identify nothing, and counting them the same as "تحكيم" lets the
#: scaffolding of a short question outvote the word carrying the topic.
#: Taken from the table rather than listed again: a word whose only meanings
#: are `contract` and `clause` is, by definition, one of these.
_SCAFFOLDING = frozenset({"contract", "clause", "process", "quantity"})
SCAFFOLDING_WEIGHT = 0.35


def _base_weight(meanings: frozenset[str]) -> float:
    return SCAFFOLDING_WEIGHT if meanings and meanings <= _SCAFFOLDING else 1.0


def informativeness(texts: list[str]) -> dict[str, float]:
    """How much each word narrows things down, from the texts to hand.

    "عقد" and "بند" are in half the questions a contract reviewer asks and
    say almost nothing about which one this is; "تحكيم" says nearly
    everything. Counting every word the same lets the scaffolding of a short
    question -- "هل يوجد بند ..." -- outvote the one word that carries the
    topic.

    Measured against this reader's own questions rather than a fixed list, so
    it follows whatever they actually write about. Smoothed, because with a
    handful of messages the counts are noisy: every weight stays near 1 until
    there is enough text to say otherwise.
    """
    seen: dict[str, tuple[int, float]] = {}
    for text in texts:
        for forms, meanings in _terms(text):
            word = _canonical(forms)
            count, _ = seen.get(word, (0, 1.0))
            seen[word] = (count + 1, _base_weight(meanings))
    total = len(texts)
    return {
        word: (math.log((total + 1) / (count + 1)) + 1.0) * base
        for word, (count, base) in seen.items()
    }


def _score(question_terms, text: str, weights: dict[str, float] | None = None) -> float:
    """How much of the question this earlier exchange covers, 0 to 1.

    A fraction of the question rather than a raw count: counting alone rewards
    a long answer for containing many words, while dividing by the question's
    own size asks the thing that matters -- how much of what was asked does
    this exchange actually speak to.
    """
    if not question_terms:
        return 0.0
    forms, meanings = _keys(text)
    weights = weights or {}

    # How much of the question's wording this exchange repeats.
    matched = possible = 0.0
    asked_about: set[str] = set()
    for word_forms, word_meanings in question_terms:
        weight = weights.get(
            _canonical(word_forms), _base_weight(word_meanings))
        possible += weight
        if word_forms & forms:
            matched += weight
        elif word_meanings & meanings:
            matched += weight * CONCEPT_WEIGHT
        asked_about |= word_meanings
    words = matched / possible if possible else 0.0

    # How many of the things the question is *about* this exchange is also
    # about. The two are scored separately and the better one wins, because
    # wording and subject fail in opposite directions: "من يتحمل تكاليف
    # التأخير؟" and "من يدفع غرامات التأخر؟" are one question asked twice
    # and share almost no words, so coverage of the wording divides the one
    # real match away. Subjects are the thing two phrasings have in common.
    asked_about -= _SCAFFOLDING
    about = meanings - _SCAFFOLDING
    # Against the shorter of the two lists of subjects, not against the
    # question's. A question that raises four subjects has not become less
    # related to an earlier one about a single subject they share -- dividing
    # by the question's own breadth would penalise it for asking a fuller
    # question. Measured: this separates the related pairs from the unrelated
    # ones by 0.42 where dividing by the question's subjects manages 0.28.
    topics = (
        len(asked_about & about) / min(len(asked_about), len(about))
        if asked_about and about else 0.0
    )

    return max(words, topics * TOPIC_WEIGHT)


#: Below this, the overlap is a word or two in common and nothing more.
MIN_SCORE = 0.34


def recall_for(user, question: str, *, exclude_conversation=None, contract=None) -> list[dict]:
    """Earlier exchanges of this reader's that bear on `question`.

    With `contract`, only exchanges about that same contract file: the chat
    answers from one file at a time, and an answer about another contract
    must not travel into this one.

    Returns `[{"question", "answer", "when", "conversation", "contract"}]`,
    most relevant first, ready to be shown as well as sent.
    """
    from django.db.models import Case, IntegerField, Value, When

    from .models import AdvisorConversation, AdvisorMessage

    terms = _terms(question or "")
    if not terms:
        return []

    threads = AdvisorConversation.objects.recallable_for(user)
    if contract is not None:
        threads = threads.filter(contract=contract)
    if exclude_conversation is not None:
        threads = threads.exclude(pk=exclude_conversation)

    # Ordered oldest-first within each thread so an answer can be paired with
    # the question above it.
    #
    # A question and its answer are normally seconds apart, but nothing
    # guarantees it -- and the primary key is a random UUID, so a tie in
    # `created_at` has no stable order to fall back on. When the answer sorted
    # first the pair was simply not found, and the exchange went unrecalled
    # with nothing to show why. Asking for the reviewer's turn first on a tie
    # costs one CASE in the query and removes the whole class of problem.
    messages = list(
        AdvisorMessage.objects
        .filter(conversation__in=threads)
        .select_related("conversation")
        .annotate(
            speaker_order=Case(
                When(role=AdvisorMessage.Role.USER, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("conversation_id", "created_at", "speaker_order")
        [:CANDIDATE_LIMIT]
    )

    # Weighted against the reader's own earlier questions: which words are
    # worth matching on depends on what this person keeps asking about.
    weights = informativeness(
        [m.body for m in messages
         if m.role == AdvisorMessage.Role.USER and m.body]
    )

    scored: list[tuple[float, dict]] = []
    for index, message in enumerate(messages):
        if message.role != AdvisorMessage.Role.USER or not message.body:
            continue
        # A file stays in its own chat. An exchange that was about an
        # attached file -- a translation, "what does page 3 say" -- would
        # carry that file's content into every other conversation that asks
        # something similar.
        if (message.used_attachment or message.document_path
                or message.image_path or message.document_pages):
            continue
        answer = None
        for candidate in messages[index + 1:index + 3]:
            if (candidate.conversation_id == message.conversation_id
                    and candidate.role == AdvisorMessage.Role.ASSISTANT
                    and candidate.body):
                answer = candidate
                break
        # A question whose answer failed carries nothing worth recalling.
        if answer is None:
            continue

        score = _score(terms, message.body, weights)
        if score < MIN_SCORE:
            continue
        scored.append((score, {
            "question": message.body.strip()[:MAX_CHARS],
            "answer": answer.body.strip()[:MAX_CHARS],
            "when": answer.created_at.strftime("%Y-%m-%d %H:%M"),
            "conversation": str(message.conversation_id),
            "contract": answer.contract_name or "",
        }))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    # One per thread: three follow-ups from the same conversation say one
    # thing three times and crowd out a different question that also matched.
    seen: set[str] = set()
    out: list[dict] = []
    for _score_value, item in scored:
        if item["conversation"] in seen:
            continue
        seen.add(item["conversation"])
        out.append(item)
        if len(out) >= MAX_EXCHANGES:
            break
    return out
