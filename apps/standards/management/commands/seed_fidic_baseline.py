"""The FIDIC baseline: the default standard beneath AJi's own positions.

A category AJi has not (yet) set a position for is resolved against this
profile instead of being left without a standard (resolution.resolve_for_
category). An uploaded and approved AJi position always wins over it.

What these are, exactly: AJi's own short statements of the principles the
FIDIC Client/Consultant Model Services Agreement (the "White Book") is known
for -- reasonable skill and care, a liability cap, the consultant keeping
copyright with a licence to the client, amicable settlement then
arbitration. They paraphrase principles; they are not FIDIC's text, which is
copyrighted and belongs in AJi's licensed copy. Where the FIDIC form leaves a
figure to the Particular Conditions, so does this baseline: no percentages or
amounts are invented here. Categories FIDIC has no general position on (data
protection, tax, deliverable formats, acceptance) are left out, so they stay
"no standard found" until AJi uploads one.

Every record says this in its source note, and is labelled "FIDIC baseline"
wherever a finding cites it.

    manage.py seed_fidic_baseline                 load (idempotent)
    manage.py seed_fidic_baseline --retire-demo   also supersede the demo
                                                  fixtures, so FIDIC applies
                                                  wherever AJi has uploaded
                                                  nothing yet
"""
from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.standards.models import (
    ClauseCategory,
    Standard,
    StandardProfile,
    StandardScope,
    StandardStatus,
    StandardVersion,
)

SOURCE = ("FIDIC baseline: AJi's paraphrase of FIDIC Client/Consultant Model Services "
          "Agreement principles, not FIDIC text. Confirm against AJi's licensed edition.")

# category, impact, preferred (en), preferred (ar), acceptable, red flag, rule
POSITIONS = [
    ("scope", 2,
     "The Services are defined in a scope schedule; the Consultant performs them with reasonable skill, care and diligence.",
     "تُحدَّد الخدمات في ملحق للنطاق، ويؤديها الاستشاري ببذل العناية والمهارة والحرص المعقول.",
     "Scope stated in the body of the agreement with defined stages.",
     "A fitness-for-purpose or guaranteed-result obligation instead of reasonable skill and care.", {}),
    ("financial", 3,
     "Remuneration is paid against the Consultant's invoices within the period stated in the Particular Conditions.",
     "تُدفع الأتعاب مقابل فواتير الاستشاري خلال المدة المحددة في الشروط الخاصة.",
     "A payment schedule tied to stages, with each payment due within a stated period.",
     "Payment at the Client's discretion, or with no due date.", {}),
    ("late_payment", 3,
     "Amounts not paid when due entitle the Consultant to financing charges from the due date.",
     "يحق للاستشاري رسوم تمويل على المبالغ غير المسددة في موعدها اعتباراً من تاريخ الاستحقاق.",
     "A remedy for late payment in a form enforceable in the jurisdiction.",
     "No remedy of any kind for late payment.", {}),
    ("variations", 3,
     "The Client may vary the Services by written instruction; the Consultant's time and remuneration are adjusted accordingly.",
     "للعميل تعديل الخدمات بتعليمات كتابية، ويُعدَّل وقت الاستشاري وأتعابه تبعاً لذلك.",
     "Oral instructions confirmed in writing, with the adjustment agreed promptly.",
     "Changes to scope with no adjustment to time or fee.",
     {"operator": "present", "markers": ["variation", "change order", "written instruction", "تغيير", "كتابي"]}),
    ("schedule", 3,
     "The Consultant is entitled to an extension of time for delay caused by the Client or by events beyond its control.",
     "يحق للاستشاري تمديد المدة عن التأخير الناتج عن العميل أو عن أحداث خارجة عن سيطرته.",
     "Extension by agreement, not to be unreasonably withheld.",
     "No entitlement to any extension of time.", {}),
    ("delayLiq", 5,
     "Delay damages are not part of the standard consultancy terms; where agreed, they are capped and count towards the overall liability limit.",
     "لا تُعدّ تعويضات التأخير جزءاً من الشروط الاستشارية القياسية؛ وإن اتُّفق عليها فهي مسقوفة وتدخل ضمن الحد الإجمالي للمسؤولية.",
     "A stated cap on delay damages, as the sole remedy for delay.",
     "Delay damages with no cap.", {}),
    ("liability", 5,
     "The Consultant's total liability is capped at the amount stated in the Particular Conditions, and excludes indirect and consequential loss.",
     "تُسقَف المسؤولية الإجمالية للاستشاري بالمبلغ المحدد في الشروط الخاصة، وتُستثنى منها الخسائر غير المباشرة والتبعية.",
     "A cap linked to the fee or to insurance cover, with the usual exceptions for fraud and wilful misconduct.",
     "Unlimited liability, or liability for indirect or consequential loss.", {}),
    ("insurance", 4,
     "The Consultant maintains professional indemnity insurance at the level stated in the Particular Conditions, where available on reasonable commercial terms.",
     "يحتفظ الاستشاري بتأمين المسؤولية المهنية بالمستوى المحدد في الشروط الخاصة، متى كان متاحاً بشروط تجارية معقولة.",
     "Cover maintained for a stated period after completion.",
     "Insurance required at a level or on terms not available in the market.", {}),
    ("bonds", 3,
     "A performance security is not a standard requirement; where agreed, its amount and form are stated and it is released on completion.",
     "ليس ضمان الأداء متطلباً قياسياً؛ وإن اتُّفق عليه تُحدَّد قيمته وشكله ويُفرج عنه عند الإنجاز.",
     "A conditional security of a stated amount with a fixed expiry.",
     "An on-demand security with no expiry or release date.", {}),
    ("suspension", 3,
     "The Client may suspend the Services by notice; the Consultant recovers its costs of suspension and resumption, and a prolonged suspension allows termination.",
     "للعميل تعليق الخدمات بإشعار، ويسترد الاستشاري تكاليف التعليق والاستئناف، ويُجيز التعليق المطوّل الإنهاء.",
     "Compensation for documented costs during suspension.",
     "Suspension without limit in time and without compensation.", {}),
    ("termination", 4,
     "Either party may terminate for the other's default after notice; on termination the Consultant is paid for services performed and its costs arising from termination.",
     "لكل طرف الإنهاء لإخلال الطرف الآخر بعد إشعار، ويُدفع للاستشاري عند الإنهاء مقابل الخدمات المؤداة والتكاليف الناشئة عن الإنهاء.",
     "Client termination for convenience with notice and payment for work done.",
     "Termination by the Client without payment for work performed.", {}),
    ("ip", 4,
     "The Consultant keeps copyright in its documents; the Client has a licence to use them for the Project.",
     "يحتفظ الاستشاري بحقوق النشر في مستنداته، ويُمنح العميل ترخيصاً باستخدامها لأغراض المشروع.",
     "Ownership passes to the Client on full payment, with the Consultant keeping a right to reuse its know-how.",
     "Ownership of all intellectual property passes on signature, before payment.", {}),
    ("confidentiality", 2,
     "Each party keeps the other's confidential information confidential and uses it only for the Project.",
     "يحافظ كل طرف على سرية المعلومات السرية للطرف الآخر ولا يستخدمها إلا لأغراض المشروع.",
     "A confidentiality period stated in years after completion.",
     "A one-sided confidentiality obligation on the Consultant only.", {}),
    ("force_majeure", 3,
     "Neither party is liable for failure caused by exceptional events beyond its control; affected obligations are suspended and a prolonged event allows termination.",
     "لا يُسأل أي طرف عن الإخفاق الناتج عن أحداث استثنائية خارجة عن سيطرته، وتُعلَّق الالتزامات المتأثرة، ويُجيز استمرار الحدث طويلاً الإنهاء.",
     "Relief from delay and liability, with a right to time.",
     "No relief for events beyond the Consultant's control.", {}),
    ("assignment", 2,
     "Neither party assigns the agreement without the other's consent; the Consultant subcontracts only with the Client's consent, and stays responsible for subconsultants.",
     "لا يتنازل أي طرف عن الاتفاقية دون موافقة الطرف الآخر، ولا يتعاقد الاستشاري من الباطن إلا بموافقة العميل ويبقى مسؤولاً عن مقاوليه.",
     "Consent not to be unreasonably withheld.",
     "The Client may assign freely while the Consultant may not.", {}),
    ("governing_law", 3,
     "The governing law and language are those stated in the Particular Conditions.",
     "القانون الحاكم ولغة العقد هما المحددان في الشروط الخاصة.",
     "The law of the country where the Services are performed.",
     "No governing law stated.", {}),
    ("dispute", 4,
     "Disputes go first to amicable settlement, then to adjudication where agreed, and finally to international arbitration.",
     "تُحال المنازعات أولاً إلى التسوية الودية، ثم إلى الفصل عبر مجلس أو خبير إن اتُّفق على ذلك، وأخيراً إلى التحكيم الدولي.",
     "Institutional arbitration, or the courts of the contracting jurisdiction.",
     "No dispute-resolution mechanism at all.",
     {"operator": "present", "markers": ["arbitration", "التحكيم"]}),
    ("obligations", 2,
     "The Client provides the information, data, approvals and decisions the Services need, at its own cost and without delaying the Consultant.",
     "يقدّم العميل المعلومات والبيانات والموافقات والقرارات التي تحتاجها الخدمات على نفقته ودون تأخير الاستشاري.",
     "Client responses within a stated number of days.",
     "No obligation on the Client to provide information or decisions.", {}),
]


class Command(BaseCommand):
    help = "Load the FIDIC baseline standards (the default beneath AJi's own positions)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--retire-demo", action="store_true",
            help="Supersede the demo fixtures, so FIDIC applies wherever AJi has uploaded nothing.")

    @transaction.atomic
    def handle(self, *args, **options):
        created = 0
        for code, impact, preferred, preferred_ar, acceptable, red_flags, rule in POSITIONS:
            category = ClauseCategory.objects.filter(code=code).first()
            if category is None:
                self.stdout.write(self.style.WARNING(f"skipped {code}: no such category"))
                continue
            standard, _ = Standard.objects.get_or_create(
                category=category, scope=StandardScope.GLOBAL, country="", contract_type=None,
                reference_template=StandardProfile.FIDIC,
                defaults={"title": f"{category.name} — FIDIC baseline"},
            )
            if standard.versions.exists():
                continue
            StandardVersion.objects.create(
                standard=standard, version=1, preferred_position=preferred,
                preferred_position_ar=preferred_ar, acceptable_range=acceptable,
                red_flags=red_flags, rule_definition=rule, impact=impact,
                responsible_department=category.default_department,
                source_reference=SOURCE, status=StandardStatus.APPROVED,
                effective_date=date(2020, 1, 1), is_demo_fixture=False,
                change_note="Seeded by seed_fidic_baseline.",
            )
            created += 1
        self.stdout.write(self.style.SUCCESS(f"FIDIC baseline: {created} standard(s) created."))

        if options["retire_demo"]:
            retired = 0
            for version in StandardVersion.objects.filter(is_demo_fixture=True,
                                                           status=StandardStatus.APPROVED):
                version.status = StandardStatus.SUPERSEDED
                version.expiry_date = timezone.localdate()
                version.save(update_fields=["status", "expiry_date", "updated_at"])
                retired += 1
            self.stdout.write(self.style.SUCCESS(
                f"Demo fixtures superseded: {retired}. Findings that cited them keep their record."))
