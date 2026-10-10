"""Build the chat's RAG index for contracts analysed before it existed.

New analyses build theirs when they finish (analysis.tasks.analyse_contract);
this is for the ones already in the system, and for a rebuild after the
passage format changes (--rebuild).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.analysis import rag
from apps.analysis.models import AnalysisStatus, RagIndex
from apps.contracts.models import Contract


class Command(BaseCommand):
    help = "Build the chat's RAG index of every contract's current analysis."

    def add_arguments(self, parser):
        parser.add_argument("--rebuild", action="store_true",
                            help="Rebuild indexes that already exist.")

    def handle(self, *args, **options):
        built = skipped = failed = 0
        contracts = (Contract.objects.exclude(current_analysis=None)
                     .exclude(current_analysis__status=AnalysisStatus.FAILED)
                     .select_related("current_analysis"))
        for contract in contracts:
            run = contract.current_analysis
            if not options["rebuild"] and RagIndex.objects.filter(analysis=run).exists():
                skipped += 1
                continue
            index = rag.build_quietly(run)
            if index is None:
                failed += 1
                self.stderr.write(f"  failed: {contract.short_ref} {contract.project_name}")
            else:
                built += 1
                self.stdout.write(f"  {contract.short_ref}: {index.passages_count} passages")
        self.stdout.write(self.style.SUCCESS(
            f"RAG indexes: {built} built, {skipped} already there, {failed} failed."))
