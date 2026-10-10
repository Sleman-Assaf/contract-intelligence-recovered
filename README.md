# Contract Intelligence — recovered source snapshot

**AI-assisted contract review with document extraction, clause assessment and a human approval workflow.**

The recovered Django applications ingest contract documents, extract text/OCR evidence, identify clauses, compare them with configured standards and record findings for review and reporting.

**بالعربية:** نظام لتحليل العقود، استخراج النصوص والبنود، مقارنتها بالمعايير، وإعداد ملاحظات وتوصيات مع مراجعة بشرية.

> **This snapshot is incomplete and cannot currently be started as a Django project.** The uploaded inner archive `aji-contract-intelligence-v1.zip` ends before its ZIP directory and before essential project files. Only complete entries with validated checksums were recovered.

## Features visible in the recovered code

- Contract/document records, processing tasks and extracted evidence.
- OCR preprocessing, rotation/skew handling, line recovery and coordinates.
- Configurable AI orchestration, clause classification and assessment.
- Standards, findings, human review workflow and notifications.
- Reporting, audit records, account management and application tests.

These descriptions come from the recovered implementation. Full application startup and external services could not be verified.

## Recovered modules

| Folder | Purpose |
| --- | --- |
| `apps/accounts/` | Account management and permissions |
| `apps/contracts/`, `apps/documents/` | Contracts, documents, extraction and OCR |
| `apps/ai/`, `apps/analysis/` | AI orchestration, classification and findings |
| `apps/standards/` | Review standards |
| `apps/workflow/`, `apps/notifications/` | Review state and notifications |
| `apps/reports/`, `apps/audit/`, `apps/common/` | Reports, audit and shared components |

289 Python files and two supplied metadata files were recovered. The virtual environment, Celery state, credentials and unrelated video-production assets were excluded.

## Missing files and startup

At least the following were not present in the recoverable archive:

- `manage.py` and the dependency manifest (`requirements.txt` or equivalent).
- Project settings, root URLs, WSGI/ASGI and Celery configuration.
- `templates/` and `static/`.

Guessing dependencies or writing new settings would not restore the original application. No working installation command is claimed for this snapshot.

To supply the complete project, create a new ZIP from the original folder, excluding `.venv`, `venv`, `__pycache__`, `.celery`, `.pytest_cache`, `.env`, private documents/databases and unrelated `bolt_kling` video files. Keep source, migrations, configuration templates, `manage.py`, the dependency manifest, templates and static assets.

Wait for archive creation to finish, verify that the ZIP opens and includes those files, then upload it. The complete project's exact migration, account-creation, worker and startup commands can then be documented and tested.

## Verification and publication

291 complete files passed ZIP CRC validation. Python syntax can be checked, but Django startup, UI rendering, migrations and tests require the missing project files. This recovered folder is preserved for review and should not be published as a complete, runnable application.
