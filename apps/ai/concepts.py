"""What contract words mean, for matching one question against another.

"ما مدة الضمان؟" and "كم فترة الكفالة؟" are the same question. To a string
compare they share nothing at all. This table is what closes that gap: each
entry gathers the words -- Arabic and English, since questions arrive in both
and often mixed -- that a reviewer would use for one idea.

── Why a table and not a model ──────────────────────────────────────────────

Meaning is a model's job, and there is no model here to do it. The on-premises
service exposes one chat route and no embeddings, Ollama is not reachable
directly, and a local embedder would mean torch and a model download on every
machine that installs the site. An embedding call per question would also cost
a round trip against a service that answers about 0.7 requests a second.

So the vocabulary of one domain is written down instead. It is bounded where a
model would generalise, and in exchange it costs nothing, runs offline, and
behaves the same way twice -- which also means a wrong match can be found and
fixed here rather than argued with.

── Adding to it ─────────────────────────────────────────────────────────────

Add the word to the concept a reviewer would say it belongs to. A word may sit
in several: "ضمان" is both a warranty and a retained sum, and both readings
are worth matching. Spelling variants are unnecessary -- hamza, ta-marbuta,
tashkeel and the definite article are folded before lookup -- but plurals are
worth adding where they are irregular, since light stemming only catches the
regular ones.

Two cautions, both learned by measuring. A word in no concept costs a miss; a
word in the *wrong* concept costs a false match, and the false match is the
expensive one -- it puts another contract's exchange in front of the model.
And a concept wide enough to hold two ideas will join them: "مدة" and "جدول"
sat in one entry until a question about a contract's term began recalling one
about the payment schedule. When in doubt, split.
"""
from __future__ import annotations

#: concept -> the words that mean it. Keys are internal identifiers and never
#: shown to anyone; the words are what a reviewer actually types.
CONCEPTS: dict[str, tuple[str, ...]] = {
    # ── the words every contract question contains ──────────────────────
    # These identify nothing on their own. `_SCAFFOLDING` in recall.py names
    # them, so they weigh less than the word carrying the topic.
    "contract": (
        "عقد", "عقود", "اتفاقية", "اتفاقيات", "اتفاق", "تعاقد", "تعاقدية",
        "مقاولة", "وثيقة",
        "contract", "contracts", "contractual", "agreement", "deal",
    ),
    "clause": (
        "بند", "بنود", "مادة", "مواد", "شرط", "شروط", "أحكام", "فقرة", "نص",
        "clause", "clauses", "provision", "provisions", "article", "terms",
        "conditions", "wording",
    ),
    "process": (
        "إجراء", "إجراءات", "عملية", "خطوات", "آلية", "تطبيق", "حالة",
        "نموذج", "استمارة",
        "procedure", "procedures", "process", "steps", "mechanism", "form",
    ),
    "quantity": (
        "نسبة", "نسب", "عدد", "كمية", "مقدار", "إجمالي", "مجموع", "حجم",
        "percentage", "percent", "number", "quantity", "total", "amount",
    ),

    # ── money ───────────────────────────────────────────────────────────
    "payment": (
        "دفع", "دفعات", "سداد", "تسديد", "تسدد", "مستحق", "مستحقات",
        "فاتورة", "فواتير", "استحقاق", "صرف", "مستخلص", "مستخلصات",
        "تحويل", "شيك",
        "payment", "payments", "invoice", "invoices", "disbursement",
        "invoicing", "due", "paid", "pay",
    ),
    "advance": (
        "مقدمة", "مقدّمة", "مقدم", "دفعة", "سلفة", "سلف", "عربون",
        "تحريك", "تعبئة",
        "advance", "upfront", "deposit", "prepayment",
        "mobilisation", "mobilization",
    ),
    "fees": (
        "أتعاب", "رسوم", "بدل", "بدلات", "مقابل", "أجر", "أجور", "مكافأة",
        "عمولة",
        "fee", "fees", "remuneration", "commission",
        "رسم",
    ),
    "price": (
        "قيمة", "قيم", "سعر", "أسعار", "تسعير", "مبلغ", "مبالغ", "تكلفة",
        "تكاليف", "كلفة", "ميزانية",
        "price", "pricing", "value", "cost", "costs", "budget",
    ),
    "retention": (
        "احتجاز", "محتجز", "محتجزة", "محتجزات", "حجز",
        "retention", "retained", "withheld",
        "withhold",
        "retain",
    ),
    "bond": (
        "سند", "خطاب", "خطابات", "مصرفية", "بنكية", "بنكي",
        "bond", "bonds", "surety",
        "bank",
    ),
    "tax": (
        "ضريبة", "ضرائب", "ضريبي", "زكاة", "استقطاع", "قيمة مضافة",
        "tax", "taxes", "vat", "withholding",
    ),
    "currency": (
        "عملة", "عملات", "ريال", "دولار", "دينار", "درهم", "يورو",
        "currency", "sar", "usd", "eur", "aed", "jod", "omr",
    ),
    "claim": ("مطالبة", "مطالبات", "ادعاء", "دعوى", "claim", "claims"),

    # ── risk and consequence ────────────────────────────────────────────
    "warranty": (
        "ضمان", "ضمانات", "ضمانة", "كفالة", "كفالات", "صيانة", "خفية",
        "warranty", "warranties", "guarantee", "maintenance", "latent",
    ),
    "delay_penalty": (
        "غرامة", "غرامات", "تأخير", "تأخيرات", "تأخر", "جزائية", "جزائي",
        "تعطيل", "عقوبة", "عقوبات", "جزاء",
        "penalty", "penalties", "liquidated", "delay", "delays",
        "late", "lateness", "overdue",
    ),
    "liability": (
        "مسؤولية", "مسئولية", "مساءلة", "التزام", "التزامات", "تبعة",
        "تبعات", "ذمة",
        "liability", "liabilities", "obligation", "obligations",
        "responsibility", "responsible",
        "liable",
    ),
    "cap": (
        "سقف", "سقوف", "حد", "حدود", "أقصى", "أعلى",
        "cap", "ceiling", "limit", "limits", "maximum", "threshold",
        "capped", "caps",
    ),
    "indemnity": (
        "تعويض", "تعويضات", "تعويضية", "جبر", "ضرر", "أضرار", "خسائر",
        "indemnity", "indemnification", "compensation", "damages", "losses",
        "خسارة", "indemnify", "indemnified",
    ),
    "breach": (
        "إخلال", "مخالفة", "مخالفات", "تقصير", "إهمال", "تعثر", "خرق",
        "breach", "default", "violation", "negligence",
    ),
    "insurance": (
        "تأمين", "تأمينات", "مؤمن", "تغطية", "بوليصة", "قسط",
        "insurance", "insured", "insurer", "coverage", "policy", "premium",
        "cover", "covered",
        "insure",
    ),
    "force_majeure": (
        "قاهرة", "قوة", "استثنائية", "طارئة", "طارئ", "جائحة", "كوارث",
        "force", "majeure", "pandemic", "unforeseen",
        "excused", "excuse", "unforeseeable", "epidemic", "catastrophe",
    ),
    "health_safety": (
        "سلامة", "صحة", "أمان", "بيئة", "حوادث",
        "safety", "health", "hse", "environment", "accident",
    ),

    # ── the shape of the engagement ─────────────────────────────────────
    "scope": (
        "نطاق", "أعمال", "خدمات", "مخرجات", "تسليمات", "مهام", "نشاطات",
        "scope", "works", "services", "deliverables", "tasks", "activities",
    ),
    "duration": (
        "مدة", "فترة", "أجل", "مهلة", "سريان", "صلاحية", "انتهاء", "أمد",
        "duration", "period", "term", "validity", "expiry", "expiration",
        "start", "starts", "commence", "commencement", "effective",
        "مهل", "آجال", "مدد",
    ),
    "schedule": (
        "جدول", "زمني", "مواعيد", "تواريخ", "برنامج", "تسلسل", "خطة",
        "schedule", "timeline", "programme", "program", "deadline",
        "موعد", "جداول",
    ),
    "milestone": (
        "مرحلة", "مراحل", "مرحلية", "milestone", "milestones", "phase",
    ),
    "renewal": (
        "تجديد", "تمديد", "تمديدات", "استمرار",
        "renewal", "renewals", "renew", "extension", "extend",
    ),
    "termination": (
        "إنهاء", "فسخ", "إلغاء", "إبطال", "انسحاب",
        "termination", "terminate", "terminated", "cancellation", "cancel",
        "rescission",
    ),
    "suspension": (
        "تعليق", "إيقاف", "توقف", "تأجيل", "تجميد",
        "suspension", "suspend", "postpone",
    ),
    "variation": (
        "تغيير", "تغييرات", "تغييري", "تغييرية", "تعديل", "تعديلات",
        "ملحق", "إضافي", "إضافية", "أوامر",
        "variation", "variations", "change", "amendment", "addendum", "order",
        "changes", "orders",
        "ملاحق", "vary", "varied",
    ),
    "acceptance": (
        "تسليم", "استلام", "تسلم", "قبول", "إنجاز", "اكتمال", "ابتدائي",
        "نهائي",
        "delivery", "acceptance", "handover", "completion", "completed",
        "accept", "accepted", "approve", "approves", "approved", "sign-off", "signoff",
        "شهادة", "شهادات", "deliver", "delivered",
    ),
    "defects": (
        "عيب", "عيوب", "خلل", "قصور", "أخطاء", "عطل", "إصلاح", "معالجة",
        "defect", "defects", "remedial", "rectification", "snag",
    ),

    # ── people and paperwork ────────────────────────────────────────────
    # Who the reader is, as opposed to who the parties to a contract are.
    # Only the possessive forms: bare "اسم" is the name of a project or a
    # party far more often than it is the reader's own, and folding the two
    # together would answer "ما اسم المشروع؟" out of a conversation about
    # somebody's job title.
    "self": (
        "اسمي", "مهنتي", "وظيفتي", "تخصصي", "منصبي", "خبرتي", "دوري",
        "profession", "occupation", "job", "career", "myself",
        # Not "name" or "role": "the name of the client" and "the role of the
        # engineer" are contract questions, as "ما اسم المشروع" is in Arabic.
    ),
    "parties": (
        "عميل", "عملاء", "صاحب", "طرف", "أطراف", "مستفيد", "جهة", "ممثل",
        "client", "employer", "party", "parties", "representative",
    ),
    "subcontract": (
        "باطن", "مقاول", "مقاولين", "استشاري", "استشاريين", "مورد", "موردين",
        "subcontract", "subcontractor", "subconsultant", "supplier",
        "vendors",
    ),
    "staffing": (
        "كوادر", "كادر", "فريق", "موظفين", "طاقم", "خبرة", "خبراء",
        "مهندسين",
        "staff", "personnel", "team", "resources", "engineers", "experts",
        "مهندس", "مهندسون", "عامل", "عمال",
    ),
    "signature": (
        "توقيع", "توقيعات", "يوقع", "موقعة",
        "signature", "sign", "signed", "signatory",
    ),
    "notice": (
        "إشعار", "إشعارات", "إخطار", "إخطارات", "تبليغ", "تنبيه", "مراسلات",
        "notice", "notices", "notification", "notify", "correspondence",
    ),
    "approval": (
        "اعتماد", "موافقة", "مصادقة", "إقرار", "تصديق", "تفويض", "إذن",
        "approval", "approve", "consent", "authorisation", "authorization",
        "permit",
        "رخصة", "رخص", "ترخيص",
    ),
    "inspection": (
        "فحص", "تفتيش", "معاينة", "اختبار", "اختبارات", "مراجعة", "تدقيق",
        "رقابة",
        "inspection", "review", "audit", "monitoring", "test", "testing",
        "survey",
    ),
    "assignment": (
        "تنازل", "تنازلات", "حوالة", "إحالة", "نقل",
        "assignment", "assign", "novation", "transfer",
    ),

    # ── rights, law, and the work itself ────────────────────────────────
    "arbitration": (
        "تحكيم", "نزاع", "نزاعات", "خلاف", "خلافات", "قضاء", "محكمة",
        "تقاضي", "وساطة", "تسوية", "صلح",
        "arbitration", "dispute", "disputes", "litigation", "court",
        "mediation", "settlement",
        "resolved", "resolution",
        "محاكم", "arbitrate", "arbitrator", "arbitrators",
    ),
    "governing_law": (
        "قانون", "قوانين", "أنظمة", "لوائح", "تشريع", "تشريعات", "اختصاص",
        "ولاية", "قضائي",
        "law", "laws", "governing", "jurisdiction", "statute", "regulation",
        "regulations", "legal",
        "court", "courts",
    ),
    "confidentiality": (
        "سرية", "سري", "إفشاء", "كتمان", "خصوصية",
        "confidentiality", "confidential", "disclosure", "privacy", "nda",
        "disclose", "disclosed", "secret", "secrecy", "non-disclosure",
    ),
    "intellectual_property": (
        "ملكية", "فكرية", "تأليف", "براءة", "براءات", "حقوق", "علامة",
        "تجارية", "ترخيص", "يملك", "مالك",
        "intellectual", "property", "copyright", "patent", "trademark",
        "licence", "license", "ownership", "own", "owns", "owner", "owned",
    ),
    "design": (
        "تصميم", "تصاميم", "تصميمات", "مخطط", "مخططات", "رسومات", "لوحات",
        "design", "designs", "drawing", "drawings", "plans", "blueprint",
    ),
    "site": (
        "موقع", "مواقع", "أرض", "ميدان",
        "site", "sites", "location", "premises",
    ),
    "performance": (
        "أداء", "جودة", "معايير", "مواصفات", "كفاءة", "مؤشرات", "إنتاجية",
        "performance", "quality", "standards", "specifications", "kpi",
    ),
}


def reverse_index(fold) -> dict[str, frozenset[str]]:
    """word -> the concepts it belongs to, with `fold` applied to each word.

    Folding is passed in rather than imported so this file stays a table and
    the spelling rules live in one place.
    """
    index: dict[str, set[str]] = {}
    for concept, words in CONCEPTS.items():
        for word in words:
            for part in word.split():          # "قيمة مضافة" is two tokens
                index.setdefault(fold(part), set()).add(concept)
    return {word: frozenset(concepts) for word, concepts in index.items()}
