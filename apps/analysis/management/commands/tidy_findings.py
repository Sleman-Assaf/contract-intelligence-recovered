"""Apply apps/analysis/tidy.py to analyses that ran before it existed.

    python manage.py tidy_findings            # every contract's current analysis
    python manage.py tidy_findings --all      # every analysis run
"""
from django.core.management.base import BaseCommand

from apps.analysis.models import AnalysisRun
from apps.analysis.tidy import recount, tidy_findings


class Command(BaseCommand):
    help = "Merge repeated findings and mark findings without evidence as not determined."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="every run, not only current ones")

    def handle(self, *args, **options):
        import sys

        # Arabic contract names on a Windows console (cp1252) crashed the
        # command half way; a name it cannot show is shown as "?".
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass
        runs = AnalysisRun.objects.filter(status__in=["succeeded", "degraded"])
        if not options["all"]:
            from apps.contracts.models import Contract

            runs = runs.filter(pk__in=Contract.objects.exclude(current_analysis=None)
                               .values_list("current_analysis_id", flat=True))
        for run in runs.select_related("contract"):
            before = run.findings.count()
            result = tidy_findings(run)
            recount(run)
            contract = run.contract
            if contract.current_analysis_id == run.pk:
                contract.risk_level = run.overall_risk
                contract.save(update_fields=["risk_level", "updated_at"])
            self.stdout.write(f"{contract.reference or contract.project_name[:30]} v{run.version}: "
                              f"{before} -> {result['left']} findings ({result['merged']} merged, "
                              f"{result['undetermined']} not determined, {result.get('restored', 0)} restored)")
