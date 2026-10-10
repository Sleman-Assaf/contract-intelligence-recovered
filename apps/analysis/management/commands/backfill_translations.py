"""Translate findings that predate bilingual output.

Findings written before the analysis produced both languages carry prose in
one language only, so a reader on the other language sees the original. A full
re-analysis would fix that, but it re-reads the whole contract and spends the
quota of a fresh run; this only translates words that already exist.

It never touches `original_wording` or `fallback_wording`. Those are the
contract's own text and the wording Legal approved -- the redline export writes
them into a document that goes to a counterparty, so a translation must never
reach them.

    manage.py backfill_translations --dry-run
    manage.py backfill_translations --limit 20
    manage.py backfill_translations --run <uuid>
    manage.py backfill_translations --current     # only each contract's current analysis

Also completes a finding translated before recommended actions were: its
other-language prose exists, but the action in it is still the original.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.analysis.models import Finding


class Command(BaseCommand):
    help = "Fill in the missing-language prose on findings already analysed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--run", help="Only findings of this analysis run."
        )
        parser.add_argument(
            "--current", action="store_true",
            help="Only findings of each contract's current analysis -- what the pages show.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after this many findings. Useful against a daily quota.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be translated and change nothing.",
        )

    def handle(self, *args, **options):
        from apps.ai.prompts import OUTPUT_LANGUAGES

        from apps.documents.language import mostly

        findings = Finding.objects.all().select_related("category")
        if options["current"]:
            findings = Finding.objects.current().select_related("category")
        if options["run"]:
            findings = findings.filter(analysis_id=options["run"])

        def action_untranslated(f):
            """Prose in a language whose recommended action is still another --
            unless the action is approved wording, which is never translated."""
            action = " ".join((f.recommended_action or "").split())
            if not action or action == " ".join((f.fallback_wording or "").split()):
                return False
            return any(
                mostly((f.prose.get(code) or {}).get("recommended_action") or "") not in ("", code)
                for code in OUTPUT_LANGUAGES if (f.prose.get(code) or {}).get("title")
            )

        def incomplete(f):
            missing_prose = any(
                not (f.prose.get(code) or {}).get("title")
                for code in OUTPUT_LANGUAGES
            )
            missing_clause = bool((f.original_wording or "").strip()) and any(
                code not in (f.wording_translations or {})
                for code in OUTPUT_LANGUAGES
            )
            return missing_prose or missing_clause or action_untranslated(f)

        pending = [f for f in findings if incomplete(f)]
        if options["limit"]:
            pending = pending[: options["limit"]]

        if not pending:
            self.stdout.write("Every finding already carries all languages.")
            return

        self.stdout.write(
            f"{len(pending)} finding(s) missing prose or a clause translation "
            f"in at least one of {', '.join(OUTPUT_LANGUAGES)}."
        )
        if options["dry_run"]:
            for f in pending[:10]:
                prose = sorted(k for k in f.prose if f.prose[k])
                clause = sorted(f.wording_translations or {})
                self.stdout.write(
                    f"  {str(f.pk)[:8]}  prose={prose or 'none'} "
                    f"clause={clause or 'none'}  {f.title[:50]}"
                )
            self.stdout.write("Dry run: nothing was changed.")
            return

        from apps.ai.providers.base import AIProviderError
        from apps.ai.services import ai_service

        if not hasattr(ai_service, "translate_finding"):
            raise CommandError(
                "The provider has no `translate_finding` call yet. Run\n"
                "  manage.py backfill_translations --dry-run\n"
                "to see the scope, and re-analyse the contracts instead until "
                "that call exists."
            )

        done = failed = 0
        for finding in pending:
            try:
                ai_service.translate_finding(finding=finding)
                done += 1
            except AIProviderError as exc:
                failed += 1
                self.stderr.write(f"  {str(finding.pk)[:8]} failed: {exc}")
        self.stdout.write(
            self.style.SUCCESS(f"Translated {done}; {failed} failed.")
        )
