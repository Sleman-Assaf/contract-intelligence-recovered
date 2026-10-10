"""Create or promote an administrator (§34).

    python manage.py create_admin you@ajigroup.com

The password is asked for interactively and never appears as an argument: a
command line lands in shell history, in the process list, and in whatever
terminal log is running. For an unattended install, put it in an environment
variable and name that variable with --password-env.

`--superuser` additionally grants Django's own admin site. It is off by
default on purpose: Role.ADMIN is the authority to administer *this*
application, while `is_superuser` is unrestricted access to every table in
the database, and §34 keeps the two apart.
"""
from __future__ import annotations

import getpass
import os

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Entity, Role, User
from apps.audit import services as audit
from apps.audit.models import AuditAction


class Command(BaseCommand):
    help = "Create an administrator, or promote an existing account to one."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument(
            "--entity",
            help="Entity code to attach, e.g. aji-jordan. Optional.",
        )
        parser.add_argument(
            "--password-env",
            metavar="VAR",
            help="Read the password from this environment variable instead of "
                 "asking for it. For unattended installs.",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also grant Django's admin site (is_superuser). Not needed to "
                 "manage users in this application.",
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        if "@" not in email:
            raise CommandError(f"{email!r} is not an email address.")

        entity = None
        if options["entity"]:
            try:
                entity = Entity.objects.get(code=options["entity"])
            except Entity.DoesNotExist:
                known = ", ".join(Entity.objects.values_list("code", flat=True)) or "none"
                raise CommandError(
                    f"No entity with code {options['entity']!r}. Known: {known}."
                )

        existing = User.objects.filter(email=email).first()
        password = self._password(options, required=existing is None)

        with transaction.atomic():
            if existing is None:
                user = User.objects.create_user(email=email, password=password)
                created = True
            else:
                user = existing
                created = False
                if password:
                    user.set_password(password)

            user.role = Role.ADMIN
            user.is_active = True
            if entity is not None:
                user.entity = entity
            if options["superuser"]:
                user.is_superuser = True
                user.is_staff = True
            user.save()

            audit.record(
                AuditAction.PERMISSION_CHANGED,
                actor=user,
                entity_type="user", entity_id=user.pk,
                new_state=Role.ADMIN,
                details={"email": email, "via": "create_admin",
                         "created": created, "superuser": bool(options["superuser"])},
            )

        what = "Created" if created else "Promoted"
        self.stdout.write(self.style.SUCCESS(
            f"{what} {email} as {Role.ADMIN}"
            + (f", entity {entity.code}" if entity else "")
            + (", Django superuser" if options["superuser"] else "")
        ))
        self.stdout.write(
            "They can sign in now, add users, and activate anyone who registers."
        )

    def _password(self, options, *, required):
        """Ask for the password, or read it from the named variable."""
        var = options["password_env"]
        if var:
            password = os.environ.get(var)
            if not password:
                raise CommandError(f"Environment variable {var!r} is empty or unset.")
            self._validate(password)
            return password

        while True:
            password = getpass.getpass("Password: ")
            if not password:
                if required:
                    self.stderr.write("A password is required for a new account.")
                    continue
                # Promoting somebody who already has one: leave it alone.
                return ""
            if password != getpass.getpass("Password (again): "):
                self.stderr.write("They did not match. Try again.")
                continue
            try:
                self._validate(password)
            except CommandError as exc:
                self.stderr.write(str(exc))
                continue
            return password

    @staticmethod
    def _validate(password):
        try:
            password_validation.validate_password(password)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages))
