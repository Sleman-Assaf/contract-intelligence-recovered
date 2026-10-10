"""Development fixtures for the standards library (§49).

Every version written here has `is_demo_fixture=True`, which makes it render
as **TEST — NOT APPROVED POLICY** in the admin, on the analysis screen, in the
comparison view and in the exported report. Nothing produced from these can be
mistaken for a position AJi has signed off.

Provenance of the twelve positions: they are transcribed from the application
being replaced — `src/lib/demoData.js`, findings F-01 to F-12 — where they are
themselves labelled fictional. They are plausible and specific, which makes
them useful for exercising the pipeline, but AJi Legal has not confirmed any
of them. Until they do, they are test data.

Refuses to run with --check in a non-debug environment.
"""
from __future__ import annotations

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.standards.models import (
    ClauseCategory,
    Standard,
    StandardScope,
    StandardStatus,
    StandardVersion,
)

SOURCE = "DEMO FIXTURE — transcribed from demoData.js F-01..F-12. Not approved AJi policy."

# category, preferred position, acceptable, red flag, impact(1-5), department,
# machine-checkable rule, approved replacement clause wording
#
# The last field is contract language, not policy prose: it is what the
# redline export offers to a counterparty, so it has to read as a clause.
POSITIONS: list[tuple] = [
    ("delayLiq",
     "Liquidated damages for delay shall not exceed 10% of the total contract value.",
     "Up to 15% with written approval from Legal Affairs.",
     "No cap on delay damages — do not sign.",
     5, "Legal Affairs",
     {"key": "ld_cap_percent", "extract": "percentage", "operator": "lte",
      "value": 10, "fallback_value": 15},
     "Liquidated damages for delay shall not in aggregate exceed ten per cent (10%) of the total Contract Price, and shall be the Client's sole and exclusive remedy for delay."),

    ("insurance",
     "Professional indemnity insurance of at least SAR 25,000,000.",
     "Down to SAR 20,000,000 for contracts under SAR 5,000,000.",
     "Below SAR 15,000,000 — do not sign.",
     4, "Risk Management",
     {"key": "pi_insurance", "extract": "money", "operator": "gte",
      "value": 25000000, "fallback_value": 20000000, "currency": "SAR"},
     "The Consultant shall effect and maintain professional indemnity insurance with a limit of indemnity of not less than SAR 25,000,000 for each and every claim, and shall maintain such cover for six (6) years following completion of the Services."),

    ("dispute",
     "Institutional arbitration clause included as mandatory language.",
     "Courts of the contracting jurisdiction where arbitration is refused.",
     "No dispute-resolution mechanism at all.",
     4, "Legal Affairs",
     {"operator": "present", "markers": ["arbitration", "التحكيم"]},
     "Any dispute arising out of or in connection with this Agreement shall be finally settled by arbitration administered by the Saudi Center for Commercial Arbitration in accordance with its Arbitration Rules, seated in Riyadh."),

    ("financial",
     "At least 60% of payments tied to approved delivery milestones.",
     "40% milestone-linked for contracts under 6 months.",
     "Payment wholly at the client's discretion.",
     3, "Finance",
     {"key": "milestone_percent", "extract": "percentage", "operator": "gte",
      "value": 60, "fallback_value": 40},
     "Not less than sixty per cent (60%) of the Contract Price shall be payable against delivery milestones approved by the Client in writing, and the balance in equal periodic instalments."),

    ("variations",
     "Every variation requires a written change order stating cost and time impact.",
     "Verbal instruction confirmed in writing within 5 business days.",
     "Client may vary scope without any cost or time adjustment.",
     3, "Project Management",
     {"operator": "present", "markers": ["change order", "written", "أمر تغيير", "كتابي"]},
     "No variation to the Services shall take effect unless recorded in a written change order signed by both parties and stating its effect on the Contract Price and the Programme."),

    ("suspension",
     "Documented fixed costs are compensated during any suspension.",
     "Compensation after 30 days of suspension.",
     "Unlimited suspension with no compensation.",
     3, "Finance", {},
     "Where the Client suspends the Services, the Client shall reimburse the Consultant's documented fixed costs properly incurred during the period of suspension."),

    ("ip",
     "Transfer of design ownership is deferred until payment is received in full.",
     "Transfer on 90% payment with retention of moral rights.",
     "Ownership transfers on signature.",
     4, "Legal Affairs", {},
     "Ownership of the intellectual property in the deliverables shall transfer to the Client upon payment in full of all sums due to the Consultant, and not before."),

    ("confidentiality",
     "Confidentiality term of 5 years for sensitive engineering information.",
     "3 years for non-technical commercial information.",
     "No confidentiality obligation on the client.",
     2, "Risk Management",
     {"key": "confidentiality_years", "extract": "duration_days",
      "operator": "gte", "value": 1825},
     "Each party shall keep confidential all information disclosed by the other party in connection with this Agreement for a period of five (5) years from the date of disclosure."),

    ("scope",
     "A detailed scope annex with clearly defined phases is attached.",
     "Scope defined in the body with phase milestones.",
     "Scope described only in general terms.",
     2, "Project Management", {},
     "The Services are as set out in the Scope Annex attached to and forming part of this Agreement, which defines the phases and deliverables in detail."),

    ("deliverables",
     "Approved digital formats are specified (DWG, PDF, BIM).",
     "Formats agreed in writing before the first delivery.",
     "Formats left to the client's later discretion.",
     1, "Project Management", {},
     "Deliverables shall be issued in the formats specified in the Deliverables Schedule (DWG, PDF and BIM), agreed in writing before the first delivery."),

    ("schedule",
     "Entitlement to an extension of time for client-caused delay.",
     "Extension by agreement, not to be unreasonably withheld.",
     "No entitlement to any extension.",
     3, "Project Management", {},
     "The Consultant shall be entitled to an extension of time for any delay caused by the Client or by any matter beyond the Consultant's reasonable control."),

    ("obligations",
     "The client provides required information within 5 business days.",
     "Within 10 business days.",
     "No time limit on client obligations.",
     2, "Project Management",
     {"key": "client_response_days", "extract": "duration_days",
      "operator": "lte", "value": 5},
     "The Client shall provide such information, approvals and decisions as the Consultant may reasonably require within five (5) business days of a written request."),]

#: Where the twelve are Saudi-specific, and what changes elsewhere. Only the
#: rows that genuinely differ get a country override — most positions are
#: jurisdiction-neutral.
COUNTRY_OVERRIDES = [
    ("dispute", "SA",
     "Institutional arbitration at the Saudi Center for Commercial Arbitration.",
     "Riyadh courts where arbitration is refused.",
     "No dispute-resolution mechanism.", 4),
    ("dispute", "JO",
     "Arbitration under the Jordanian Arbitration Law.",
     "Amman courts where arbitration is refused.",
     "No dispute-resolution mechanism.", 4),
    ("insurance", "JO",
     "Professional indemnity insurance of at least JOD 7,000,000.",
     "Down to JOD 5,000,000 for contracts under JOD 1,000,000.",
     "Below JOD 3,000,000 — do not sign.", 4),
    ("late_payment", "SA",
     "Delay compensation for late payment, expressed as documented cost — "
     "not interest, which is not enforceable in the Kingdom.",
     "Compensation capped at 5% of the overdue amount.",
     "No remedy for late payment.", 3),
    ("late_payment", "JO",
     "Interest on late payment at the statutory rate.",
     "Agreed commercial rate.",
     "No remedy for late payment.", 3),
]


class Command(BaseCommand):
    help = (
        "Load clearly-labelled DEMO standards for development. "
        "These are NOT approved AJi policy."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Allow loading when DEBUG is False (not recommended).",
        )
        parser.add_argument(
            "--remove", action="store_true",
            help="Delete every demo fixture version instead of creating them.",
        )
        parser.add_argument(
            "--refresh", action="store_true",
            help=(
                "Update existing demo fixtures in place. Only valid for demo "
                "data — approved policy versions are immutable by design."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to load demo standards with DEBUG=False. These are test "
                "data, not AJi policy, and must not reach a production library. "
                "Use --force only if you understand the consequence."
            )

        if options["remove"]:
            return self._remove()

        if options["refresh"]:
            return self._refresh()

        missing = [
            code for code, *_ in POSITIONS
            if not ClauseCategory.objects.filter(code=code).exists()
        ]
        if missing:
            raise CommandError(
                "Missing clause categories: " + ", ".join(missing)
                + ". Run `manage.py seed_reference` first."
            )

        created = self._load_global()
        created += self._load_overrides()

        self.stdout.write(self.style.WARNING(
            "\n  ***  DEMO / TEST STANDARDS  —  NOT APPROVED AJi POLICY  ***\n"
        ))
        self.stdout.write(self.style.SUCCESS(f"Loaded {created} demo standard version(s)."))
        self.stdout.write(
            "Every one is flagged is_demo_fixture=True and will be labelled as "
            "test data wherever it appears.\n"
            "Replace them with approved positions via "
            "`manage.py import_standards <file.json>`."
        )

    # ── loading ──────────────────────────────────────────────────────────
    def _version(self, standard, *, preferred, acceptable, red_flags, impact,
                 department="", rule=None, wording="") -> bool:
        if standard.versions.filter(is_demo_fixture=True).exists():
            return False
        StandardVersion.objects.create(
            standard=standard, version=1,
            preferred_position=preferred,
            acceptable_range=acceptable,
            red_flags=red_flags,
            impact=impact,
            responsible_department=department,
            rule_definition=rule or {},
            approved_fallback_wording=wording,
            source_reference=SOURCE,
            status=StandardStatus.APPROVED,
            effective_date=date(2020, 1, 1),
            is_demo_fixture=True,
            change_note="Seeded by seed_demo_standards. Test data.",
        )
        return True

    def _load_global(self) -> int:
        count = 0
        for (code, preferred, acceptable, red_flags, impact, department, rule,
             wording) in POSITIONS:
            category = ClauseCategory.objects.get(code=code)
            standard, _ = Standard.objects.get_or_create(
                category=category, scope=StandardScope.GLOBAL,
                country="", contract_type=None, reference_template="aji_standard",
                defaults={"title": f"{category.name} — AJi global position"},
            )
            count += self._version(
                standard, preferred=preferred, acceptable=acceptable,
                red_flags=red_flags, impact=impact, department=department, rule=rule,
                wording=wording,
            )
        return count

    def _load_overrides(self) -> int:
        count = 0
        for code, country, preferred, acceptable, red_flags, impact in COUNTRY_OVERRIDES:
            category = ClauseCategory.objects.filter(code=code).first()
            if category is None:
                continue
            standard, _ = Standard.objects.get_or_create(
                category=category, scope=StandardScope.COUNTRY,
                country=country, contract_type=None, reference_template="aji_standard",
                defaults={"title": f"{category.name} — {country} override"},
            )
            count += self._version(
                standard, preferred=preferred, acceptable=acceptable,
                red_flags=red_flags, impact=impact,
                department=category.default_department,
            )
        return count

    # ── refresh ──────────────────────────────────────────────────────────
    def _refresh(self):
        """Update demo fixtures in place.

        `StandardVersion.save()` refuses to modify an approved version, which
        is the right rule for policy: an analysis cites the version it ran
        against, and rewriting it underneath would falsify the record. A
        queryset update bypasses that guard, which is acceptable here *only*
        because these rows are flagged `is_demo_fixture=True` and are not
        policy. Real positions are revised by publishing a new version.
        """
        updated = 0
        for code, preferred, acceptable, red_flags, impact, department, rule, wording in POSITIONS:
            rows = StandardVersion.objects.filter(
                is_demo_fixture=True, standard__category__code=code,
                standard__scope=StandardScope.GLOBAL,
            )
            updated += rows.update(
                preferred_position=preferred,
                acceptable_range=acceptable,
                red_flags=red_flags,
                impact=impact,
                responsible_department=department,
                rule_definition=rule or {},
                approved_fallback_wording=wording,
            )
        self.stdout.write(self.style.WARNING(
            "\n  ***  DEMO / TEST STANDARDS  —  NOT APPROVED AJi POLICY  ***\n"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"Refreshed {updated} demo standard version(s) in place."
        ))

    # ── removal ──────────────────────────────────────────────────────────
    def _remove(self):
        from django.db.models import ProtectedError

        versions = StandardVersion.objects.filter(is_demo_fixture=True)
        count = versions.count()
        try:
            # Bypasses the immutability guard deliberately: demo fixtures are
            # not policy, so removing them destroys no governance record.
            versions.delete()
        except ProtectedError:
            # `on_delete=PROTECT` is doing its job: an analysis cites the
            # version it ran against, and deleting it would leave findings
            # pointing at nothing. Say so instead of surfacing a traceback.
            from apps.analysis.models import Finding, MissingClause

            findings = Finding.objects.filter(
                standard_version__is_demo_fixture=True
            ).count()
            absences = MissingClause.objects.filter(
                standard_version__is_demo_fixture=True
            ).count()
            raise CommandError(
                "Cannot remove the demo standards: "
                f"{findings} finding(s) and {absences} missing-clause record(s) "
                "cite them, and deleting a version an analysis ran against "
                "would orphan the audit trail.\n"
                "Use `--refresh` to update them in place, or delete the "
                "contracts that were analysed against them first."
            )

        Standard.objects.filter(versions__isnull=True).delete()
        self.stdout.write(self.style.SUCCESS(f"Removed {count} demo standard version(s)."))
