"""Reference data — real configuration, not test policy.

Clause categories, contract types and AJi entities are structural: the system
cannot resolve a standard without them. They carry no company position, so
nothing here needs the demo-fixture warning that `seed_demo_standards` does.

Idempotent: safe to run repeatedly, and safe to run against production.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Country, Entity
from apps.contracts.models import ContractType
from apps.standards.models import ClauseCategory

#: The eight AJi operating companies, mapped to the jurisdiction each
#: contracts under. Standards resolve on the jurisdiction, not the entity —
#: several entities share one legal system.
ENTITIES = [
    ("AJi - Saudi Arabia", "أجي - السعودية", "aji-sa", Country.SAUDI_ARABIA),
    ("AJi - Jordan", "أجي - الأردن", "aji-jo", Country.JORDAN),
    ("AJi - Oman", "أجي - عُمان", "aji-om", Country.OMAN),
    ("HDP - UAE", "إتش دي بي - الإمارات", "hdp-ae", Country.UAE),
    ("AJi Built Environment", "أجي للبيئة المبنية", "aji-be", Country.SAUDI_ARABIA),
    ("AJi Water & Environment", "أجي للمياه والبيئة", "aji-we", Country.SAUDI_ARABIA),
    ("Limana", "ليمانا", "limana", Country.SAUDI_ARABIA),
    ("Raseel", "رسيل", "raseel", Country.SAUDI_ARABIA),
]

CONTRACT_TYPES = [
    ("arch", "Architectural Design Consultancy", "استشارات تصميم معماري", 10),
    ("supervision", "Engineering Supervision", "إشراف هندسي", 20),
    ("design", "Design Services", "خدمات تصميم", 30),
    ("pm", "Project Management", "إدارة مشروع", 40),
    ("interior", "Interior Design", "تصميم داخلي", 50),
    ("other", "Other", "أخرى", 90),
]

#: 21 clause categories. Twelve mirror the areas the existing application
#: already reviews; nine were identified as gaps against its own stated review
#: priorities (termination, liability, bonds, tax, governing law, data
#: protection, late payment, force majeure, assignment).
CLAUSE_CATEGORIES = [
    ("scope", "Scope of work", "نطاق العمل", "Project Management", 10),
    ("deliverables", "Deliverables and formats", "المخرجات وصيغها", "Project Management", 20),
    ("acceptance", "Acceptance", "الاعتماد والتسليم", "Project Management", 30),
    ("financial", "Payment schedule", "جدول الدفعات", "Finance", 40),
    ("late_payment", "Late payment: interest or delay compensation",
     "التأخر في السداد: فائدة أو تعويض", "Finance", 45),
    ("tax", "Taxes, VAT and withholding", "الضرائب والاستقطاع", "Finance", 50),
    ("variations", "Change orders and variations", "أوامر التغيير", "Project Management", 60),
    ("schedule", "Programme and extension of time", "البرنامج الزمني والتمديد", "Project Management", 70),
    ("delayLiq", "Liquidated damages for delay", "تعويضات التأخير", "Legal Affairs", 80),
    ("liability", "Liability cap and indemnities", "سقف المسؤولية والتعويضات", "Legal Affairs", 90),
    ("insurance", "Professional indemnity insurance", "التأمين المهني", "Risk Management", 100),
    ("bonds", "Performance bonds and guarantees", "الضمانات وسندات الأداء", "Finance", 110),
    ("suspension", "Suspension", "تعليق الأعمال", "Finance", 120),
    ("termination", "Termination rights and compensation", "الإنهاء والتعويض", "Legal Affairs", 130),
    ("ip", "Intellectual property and design ownership",
     "الملكية الفكرية وملكية التصاميم", "Legal Affairs", 140),
    ("confidentiality", "Confidentiality", "السرية", "Risk Management", 150),
    ("data_protection", "Data protection", "حماية البيانات", "Legal Affairs", 160),
    ("force_majeure", "Force majeure", "القوة القاهرة", "Legal Affairs", 170),
    ("assignment", "Assignment and subcontracting", "التنازل والتعاقد من الباطن", "Legal Affairs", 180),
    ("governing_law", "Governing law and jurisdiction", "القانون الحاكم والاختصاص", "Legal Affairs", 190),
    ("dispute", "Dispute resolution and arbitration", "تسوية المنازعات والتحكيم", "Legal Affairs", 200),
    ("obligations", "Client obligations", "التزامات العميل", "Project Management", 210),
]


class Command(BaseCommand):
    help = "Create or update reference data: entities, contract types, clause categories."

    @transaction.atomic
    def handle(self, *args, **options):
        created = updated = 0

        for name, name_ar, code, country in ENTITIES:
            _, made = Entity.objects.update_or_create(
                code=code,
                defaults={"name": name, "name_ar": name_ar, "country": country},
            )
            created += made
            updated += not made

        for code, name, name_ar, order in CONTRACT_TYPES:
            _, made = ContractType.objects.update_or_create(
                code=code,
                defaults={"name": name, "name_ar": name_ar, "sort_order": order},
            )
            created += made
            updated += not made

        for code, name, name_ar, department, order in CLAUSE_CATEGORIES:
            _, made = ClauseCategory.objects.update_or_create(
                code=code,
                defaults={
                    "name": name, "name_ar": name_ar,
                    "default_department": department, "sort_order": order,
                },
            )
            created += made
            updated += not made

        self.stdout.write(self.style.SUCCESS(
            f"Reference data ready: {created} created, {updated} updated."
        ))
        self.stdout.write(
            f"  {Entity.objects.count()} entities across "
            f"{len({e.country for e in Entity.objects.all()})} jurisdictions\n"
            f"  {ContractType.objects.count()} contract types\n"
            f"  {ClauseCategory.objects.count()} clause categories"
        )
