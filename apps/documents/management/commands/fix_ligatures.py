"""Write ligature characters out as letters in text already stored.

    manage.py fix_ligatures --dry-run
    manage.py fix_ligatures

Extraction now does this as it reads a PDF (documents/text.py); this brings
contracts extracted before up to the same text -- the blocks, the clauses,
the findings' quoted wording -- and rebuilds the chat index of every analysis
whose clauses changed. The uploaded file itself is not touched: extracting it
again gives the same text this writes.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.documents.text import LIGATURES, plain_letters


def _with_ligatures(model, field):
    from django.db.models import Q

    query = Q()
    for code in LIGATURES:
        query |= Q(**{f"{field}__contains": chr(code)})
    return model.objects.filter(query)


class Command(BaseCommand):
    help = "Write ligature characters out as letters in stored contract text."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report, change nothing")

    def handle(self, *args, **options):
        from apps.analysis import rag
        from apps.analysis.models import AnalysisRun, Clause, Finding
        from apps.documents.models import Block

        targets = [
            (Block, ("original_source_text", "final_text", "vision_text")),
            (Clause, ("text",)),
            (Finding, ("original_wording",)),
        ]
        changed_documents, changed = set(), 0
        with transaction.atomic():
            for model, fields in targets:
                names = {f.name for f in model._meta.fields}
                for field in (f for f in fields if f in names):
                    rows = list(_with_ligatures(model, field))
                    for row in rows:
                        setattr(row, field, plain_letters(getattr(row, field)))
                        if not options["dry_run"]:
                            row.save(update_fields=[field])
                        document_id = getattr(row, "document_id", None)
                        if document_id:
                            changed_documents.add(document_id)
                    changed += len(rows)
                    self.stdout.write(f"  {model.__name__}.{field}: {len(rows)} row(s)")
            if options["dry_run"]:
                transaction.set_rollback(True)

        runs = AnalysisRun.objects.filter(document_id__in=changed_documents, rag_index__isnull=False)
        self.stdout.write(f"  chat indexes to rebuild: {runs.count()}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"Dry run: {changed} row(s) would change. Nothing written."))
            return
        for run in runs:
            rag.build(run)
        self.stdout.write(self.style.SUCCESS(f"Wrote out ligatures in {changed} row(s); rebuilt {runs.count()} chat index(es)."))
