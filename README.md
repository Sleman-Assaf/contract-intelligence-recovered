# Contract Intelligence — Incomplete Source Recovery

**بالعربية:** هذه نسخة توثيق لاسترجاع مشروع Contract Intelligence من ملف ZIP ناقص. المشروع يحتاج الملفات المفقودة قبل تشغيله.

> **Incomplete archive and pending source upload.** The provided nested archive was truncated. We recovered 289 Python source files plus configuration examples locally, but the workspace disconnected before those files were committed to this repository. This checkout cannot run the application.

## What was recovered

The prepared recovery package contains Python application modules under `apps/`, a sanitized `.env.example`, `.gitignore` and documentation. Recovered Python files passed syntax parsing. This does not establish that the complete application runs.

## Missing startup files

- `manage.py`.
- Root settings and URL configuration.
- WSGI/ASGI and Celery startup configuration.
- Dependency manifest such as `requirements.txt` or `pyproject.toml`.
- Templates and static assets.

## How to make it runnable

Provide a new ZIP of the actual project folder containing the complete source and dependency manifest. Exclude `.venv/`, `__pycache__/`, production secrets, databases and the unrelated `bolt_kling/` folder.

Once the missing configuration and dependencies are available, the installation and startup commands can be checked against the actual project. No unverified startup command is presented for this incomplete archive.

## Data handling

The recovery excludes virtual environments, caches, private environment files and unrelated media. Environment examples contain variable names with blank values. No open-source license is granted unless the rights holder adds one.
