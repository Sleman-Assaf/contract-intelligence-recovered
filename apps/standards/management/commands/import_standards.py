"""Import approved AJi standards from a file (§49, §50).

Governance rule enforced here: **imported standards arrive as DRAFT.** Loading
a file is not the same as Legal approving its contents, so nothing becomes
active until a Standard Manager approves it in the admin — an act that records
who approved it and when.

Accepts JSON, CSV or the Excel template itself (.xlsx, from
`manage.py standards_template`). The column names match the template
circulated to the country legal teams, so a completed spreadsheet loads
without rekeying. Template rows Legal left untouched -- a category row with
nothing filled in -- are skipped, not failed.

    manage.py import_standards positions.json
    manage.py import_standards ksa.csv --country SA --dry-run
    manage.py import_standards AJi_Standards_Template.xlsx --dry-run
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_date

from apps.accounts.models import Country
from apps.audit import services as audit
from apps.audit.models import AuditAction
from apps.contracts.models import ContractType
from apps.standards.models import (
    ClauseCategory,
    Standard,
    StandardScope,
    StandardStatus,
    StandardVersion,
)

#: CSV header -> model field. Mirrors the Legal template.
CSV_FIELDS = {
    "category": "category",
    "country": "country",
    "contract_type": "contract_type",
    "preferred_position": "preferred_position",
    # The Arabic copies. Without them in this map an Arabic position or
    # approved wording in the file was dropped without a word.
    "preferred_position_ar": "preferred_position_ar",
    "approved_fallback_wording_ar": "approved_fallback_wording_ar",
    "acceptable_fallback": "acceptable_range",
    "acceptable_range": "acceptable_range",
    "acceptable_range_ar": "acceptable_range_ar",
    "walk_away": "red_flags",
    "red_flags": "red_flags",
    "red_flags_ar": "red_flags_ar",
    "approved_fallback_wording": "approved_fallback_wording",
    "exceptions": "exceptions",
    "approval_authority": "approval_authority",
    "who_approves_the_fallback": "approval_authority",
    "responsible_department": "responsible_department",
    "impact": "impact",
    "impact_1_5": "impact",
    "legal_basis": "source_reference",
    "source_reference": "source_reference",
    "effective_date": "effective_date",
    "notes": "change_note",
    # AJi's side for the standard to apply (Standard.perspective). Without
    # this entry the template's column was dropped on reading and every
    # row imported as applying to every side.
    "aji_role": "aji_role",
    "perspective": "aji_role",
}


#: What makes a row more than an untouched template line (which carries only
#: its category and the pre-filled department).
_SUBSTANCE = ("preferred_position", "preferred_position_ar", "acceptable_range", "acceptable_range_ar",
              "red_flags", "red_flags_ar",
              "approved_fallback_wording", "approved_fallback_wording_ar", "exceptions")


def _filled_in(row: dict) -> bool:
    return any(str(row.get(k) or "").strip() for k in _SUBSTANCE)


def _cell_text(value) -> str:
    """A spreadsheet cell as text: a date as YYYY-MM-DD, 4.0 as 4."""
    from datetime import datetime

    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _mapped(pairs) -> dict:
    row: dict = {}
    for header, value in pairs:
        if header is None:
            continue
        key = CSV_FIELDS.get(str(header).strip().lower().replace(" ", "_"))
        if key and (value or "").strip():
            row[key] = value.strip()
    return row



#: AJi's role column: the stored values, and the words Legal may type.
ROLE_VALUES = {"consultant", "client", "sub_consultant", "jv_partner", "other"}
ROLE_ALIASES = {
    "الاستشاري": "consultant", "استشاري": "consultant",
    "صاحب العمل": "client", "العميل": "client", "employer": "client", "hiring": "client",
    "sub-consultant": "sub_consultant", "subconsultant": "sub_consultant", "استشاري فرعي": "sub_consultant",
    "jv": "jv_partner", "jv partner": "jv_partner", "شريك ائتلاف": "jv_partner", "ائتلاف": "jv_partner",
}

class Command(BaseCommand):
    help = "Import AJi standards from JSON, CSV or the .xlsx template. Imported rows arrive as DRAFT."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to a .json or .csv file")
        parser.add_argument("--country", default="",
                            help="Default jurisdiction when a row omits one (e.g. SA)")
        parser.add_argument("--template", default="aji_standard",
                            help="Reference template these belong to")
        parser.add_argument("--dry-run", action="store_true",
                            help="Validate and report without writing anything")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        if path.suffix.lower() == ".json":
            rows = self._read_json(path)
        elif path.suffix.lower() in {".csv", ".tsv"}:
            rows = self._read_csv(path)
        elif path.suffix.lower() == ".xlsx":
            rows = self._read_xlsx(path)
        else:
            raise CommandError("Unsupported file type. Use .json, .csv or .xlsx.")

        before = len(rows)
        rows = [r for r in rows if _filled_in(r)]
        if before != len(rows):
            self.stdout.write(f"  Skipped {before - len(rows)} template row(s) left blank.")

        if not rows:
            raise CommandError("No rows found in the file.")

        parsed, errors = self._validate(rows, options)
        for message in errors:
            self.stdout.write(self.style.ERROR(f"  {message}"))

        if errors:
            raise CommandError(
                f"{len(errors)} row(s) failed validation. Nothing was imported — "
                "fix the file and run again."
            )

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"Dry run: {len(parsed)} row(s) would be imported as DRAFT."
            ))
            for row in parsed[:10]:
                self.stdout.write(
                    f"  {row['category'].code} / {row['scope']}"
                    f"{'/' + row['country'] if row['country'] else ''}"
                )
            return

        created = self._write(parsed, options)
        self.stdout.write(self.style.SUCCESS(
            f"Imported {created} standard version(s) as DRAFT."
        ))
        self.stdout.write(self.style.WARNING(
            "None of them is active yet. A Standard Manager must approve each "
            "version in the admin — approval records who and when."
        ))

    # ── reading ──────────────────────────────────────────────────────────
    def _read_json(self, path: Path) -> list[dict]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("standards", [])
        return data

    def _read_csv(self, path: Path) -> list[dict]:
        rows: list[dict] = []
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = _mapped(raw.items())
                if row:
                    rows.append(row)
        return rows

    def _read_xlsx(self, path: Path) -> list[dict]:
        """The template's "Standards" sheet (else the first). The header is the
        row whose first cell is `category`: the template puts a title and a
        row of bilingual labels above it."""
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=True)
        sheet = book["Standards"] if "Standards" in book.sheetnames else book.worksheets[0]
        header: list[str] | None = None
        rows: list[dict] = []
        for values in sheet.iter_rows(values_only=True):
            cells = [_cell_text(v) for v in values]
            if header is None:
                if cells and cells[0].strip().lower() == "category":
                    header = cells
                continue
            row = _mapped(zip(header, cells))
            if row:
                rows.append(row)
        book.close()
        if header is None:
            raise CommandError("No header row found: the first column's header must be 'category'.")
        return rows

    # ── validation ───────────────────────────────────────────────────────
    def _validate(self, rows: list[dict], options) -> tuple[list[dict], list[str]]:
        parsed: list[dict] = []
        errors: list[str] = []
        valid_countries = {c for c, _ in Country.choices}

        for index, row in enumerate(rows, start=1):
            code = (row.get("category") or "").strip()
            if not code:
                errors.append(f"row {index}: missing 'category'")
                continue

            category = ClauseCategory.objects.filter(code=code).first()
            if category is None:
                errors.append(
                    f"row {index}: unknown clause category '{code}'. "
                    "Run seed_reference, or add it in the admin first."
                )
                continue

            if not (row.get("preferred_position") or "").strip():
                # Either language will do: a position written only in Arabic
                # is AJi's position all the same.
                if (row.get("preferred_position_ar") or "").strip():
                    row["preferred_position"] = row["preferred_position_ar"]
                else:
                    errors.append(f"row {index} ({code}): 'preferred_position' (or its Arabic "
                                  "'preferred_position_ar') is required")
                    continue

            country = (row.get("country") or options["country"] or "").strip().upper()
            if country and country not in valid_countries:
                errors.append(
                    f"row {index} ({code}): unknown country '{country}'. "
                    f"Expected one of {', '.join(sorted(valid_countries))}."
                )
                continue

            contract_type = None
            type_code = (row.get("contract_type") or "").strip()
            if type_code:
                contract_type = ContractType.objects.filter(code=type_code).first()
                if contract_type is None:
                    errors.append(f"row {index} ({code}): unknown contract type '{type_code}'")
                    continue

            # AJi's side for the standard to apply; blank = every side.
            raw_role = (row.get("aji_role") or row.get("perspective") or "").strip().lower()
            perspective = ROLE_ALIASES.get(raw_role, raw_role)
            if perspective and perspective not in ROLE_VALUES:
                errors.append(f"row {index} ({code}): unknown AJi role '{raw_role}'. "
                              f"Expected one of {', '.join(sorted(ROLE_VALUES))} or blank.")
                continue

            impact = None
            raw_impact = str(row.get("impact") or "").strip()
            if raw_impact:
                try:
                    impact = int(float(raw_impact))
                except ValueError:
                    errors.append(f"row {index} ({code}): impact '{raw_impact}' is not a number")
                    continue
                if not 1 <= impact <= 5:
                    errors.append(f"row {index} ({code}): impact must be 1-5, got {impact}")
                    continue

            effective = None
            raw_date = (row.get("effective_date") or "").strip()
            if raw_date:
                effective = parse_date(raw_date)
                if effective is None:
                    errors.append(
                        f"row {index} ({code}): effective_date '{raw_date}' is not YYYY-MM-DD"
                    )
                    continue

            rule = row.get("rule_definition") or {}
            if isinstance(rule, str):
                try:
                    rule = json.loads(rule) if rule.strip() else {}
                except json.JSONDecodeError as exc:
                    errors.append(f"row {index} ({code}): rule_definition is not valid JSON ({exc})")
                    continue

            if contract_type is not None:
                scope = StandardScope.CONTRACT_TYPE
            elif country:
                scope = StandardScope.COUNTRY
            else:
                scope = StandardScope.GLOBAL

            parsed.append({
                "category": category, "country": country, "perspective": perspective,
                "contract_type": contract_type, "scope": scope,
                "impact": impact, "effective_date": effective, "rule": rule,
                "row": row,
            })

        return parsed, errors

    # ── writing ──────────────────────────────────────────────────────────
    @transaction.atomic
    def _write(self, parsed: list[dict], options) -> int:
        created = 0
        for item in parsed:
            row = item["row"]
            standard, _ = Standard.objects.get_or_create(
                category=item["category"],
                scope=item["scope"],
                country=item["country"],
                contract_type=item["contract_type"],
                reference_template=options["template"],
                perspective=item.get("perspective", ""),
                defaults={"title": f"{item['category'].name} — {item['scope']}"
                                   + (f" — {item['perspective']}" if item.get("perspective") else "")},
            )

            # Never overwrite: a new import is a new version, so the analysis
            # that used the previous one stays explainable (§33).
            latest = standard.versions.order_by("-version").first()
            next_version = (latest.version + 1) if latest else 1

            version = StandardVersion.objects.create(
                standard=standard,
                version=next_version,
                preferred_position=row.get("preferred_position", ""),
                preferred_position_ar=row.get("preferred_position_ar", ""),
                acceptable_range=row.get("acceptable_range", ""),
                acceptable_range_ar=row.get("acceptable_range_ar", ""),
                red_flags=row.get("red_flags", ""),
                red_flags_ar=row.get("red_flags_ar", ""),
                approved_fallback_wording=row.get("approved_fallback_wording", ""),
                approved_fallback_wording_ar=row.get("approved_fallback_wording_ar", ""),
                exceptions=row.get("exceptions", ""),
                approval_authority=row.get("approval_authority", ""),
                responsible_department=(
                    row.get("responsible_department")
                    or item["category"].default_department
                ),
                rule_definition=item["rule"],
                impact=item["impact"],
                source_reference=row.get("source_reference", ""),
                effective_date=item["effective_date"] or date.today(),
                # DRAFT on purpose. Importing is not approving.
                status=StandardStatus.DRAFT,
                is_demo_fixture=False,
                change_note=row.get("change_note", "") or "Imported from file.",
            )
            audit.record(
                AuditAction.STANDARD_CREATED,
                entity_type="StandardVersion",
                entity_id=version.pk,
                new_state=StandardStatus.DRAFT,
                details={
                    "standard": str(standard),
                    "version": next_version,
                    "imported": True,
                },
            )
            created += 1
        return created
