"""Development users, one per role (§49).

Every account is created with `DEMO` in the name the site shows and a shared
development password, and the command refuses to run outside DEBUG. The prefix
is the point: these accounts must be recognisable as demo accounts on every
screen they appear on, which is why it sits in `username` rather than only in
a field nothing renders. These exist so the
separation-of-duties rules can be exercised by hand — you need at least two
people to approve anything.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Entity, Role, User

DEV_PASSWORD = "aji-dev-password-2026"

USERS = [
    ("admin@aji.demo", "DEMO Admin", Role.ADMIN, "Information Technology"),
    ("reviewer@aji.demo", "DEMO Contract Reviewer", Role.CONTRACT_REVIEWER,
     "Quantity Surveying & Contracts"),
    ("reviewer2@aji.demo", "DEMO Contract Reviewer Two", Role.CONTRACT_REVIEWER,
     "Quantity Surveying & Contracts"),
    ("legal@aji.demo", "DEMO Legal Reviewer", Role.LEGAL_REVIEWER, "Legal Affairs"),
    ("management@aji.demo", "DEMO Management", Role.MANAGEMENT, "Executive"),
    ("standards@aji.demo", "DEMO Standard Manager", Role.STANDARD_MANAGER, "Legal Affairs"),
    ("user@aji.demo", "DEMO Regular User", Role.USER, "Project Management"),
]


class Command(BaseCommand):
    help = "Create development users, one per role. DEBUG only."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Allow creation when DEBUG is False.")

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to create demo users with DEBUG=False. They share a "
                "well-known password and must never exist in production."
            )

        entity = Entity.objects.filter(code="aji-sa").first()
        if entity is None:
            raise CommandError("No entities found. Run `manage.py seed_reference` first.")

        created = named = 0
        for email, name, role, department in USERS:
            user, made = User.objects.get_or_create(
                email=email,
                defaults={
                    "username": name, "first_name": name, "role": role,
                    "entity": entity, "department": department,
                },
            )
            if made:
                user.set_password(DEV_PASSWORD)
                # The admin account needs the Django admin, but note that its
                # application role still does not permit approving a contract.
                if role == Role.ADMIN:
                    user.is_staff = True
                    user.is_superuser = True
                user.save()
                created += 1
            elif not user.username:
                # Seeded before the username field existed. Filled in here so
                # re-running the command is enough to repair an install that
                # already has these accounts -- and only when it is blank, so
                # a name somebody chose is never overwritten.
                user.username = name
                user.save(update_fields=["username"])
                named += 1

        self.stdout.write(self.style.WARNING(
            "\n  ***  DEMO USERS — DEVELOPMENT ONLY  ***\n"
        ))
        self.stdout.write(self.style.SUCCESS(f"Created {created} user(s)."))
        if named:
            self.stdout.write(self.style.SUCCESS(
                f"Filled in the display name on {named} existing account(s)."))
        self.stdout.write(f"  password for all: {DEV_PASSWORD}\n")
        for email, name, role, _ in USERS:
            self.stdout.write(f"  {email:28} {role}")
        self.stdout.write(
            "\nTwo contract reviewers exist on purpose: separation of duties means "
            "the person who uploads a contract cannot approve it, so you need a "
            "second account to walk the workflow through to approval."
        )
