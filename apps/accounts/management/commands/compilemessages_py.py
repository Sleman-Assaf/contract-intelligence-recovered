"""Compile .po catalogues to .mo without GNU gettext.

Django's own `compilemessages` shells out to `msgfmt`, which is part of GNU
gettext. That is not installed here and its installer needs administrator
rights, so translations could not be compiled at all — which is why every model
label rendered in English on an Arabic UI.

The .mo container is small and its layout is fixed, so it is written directly.
This is a drop-in for the one job the project needs; it deliberately does not
reimplement all of msgfmt.

Usage:
    manage.py compilemessages_py            # every locale under LOCALE_PATHS
    manage.py compilemessages_py --locale ar
"""
from __future__ import annotations

import array
import gettext
import re
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

MAGIC = 0x950412DE
NUL = bytes(1)

#: `msgid "…"` / `msgstr "…"`, plus the bare `"…"` continuation lines that
#: gettext uses to wrap long entries.
_DIRECTIVE = re.compile(
    r'^(msgid_plural|msgid|msgctxt)\s+"(.*)"\s*$'
)
#: `msgstr "…"` for a singular entry, `msgstr[0] "…"` for a plural one.
_MSGSTR = re.compile(r'^msgstr(?:\[(\d+)\])?\s+"(.*)"\s*$')
_CONTINUED = re.compile(r'^"(.*)"\s*$')

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r",
    '"': '"', "\\": "\\", "0": "\0",
}


def unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(_ESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_po(path: Path) -> dict[str, str]:
    """msgid -> msgstr. Entries with an empty translation are skipped.

    An empty msgstr means "not translated yet"; writing it into the catalogue
    would make gettext return an empty string instead of falling back to the
    original, so the label would vanish from the page entirely.

    Plural entries are stored the way gettext expects: the key is
    `singular \0 plural` and the value is the forms joined by NUL. Without
    this every `{% blocktrans count %}` block falls through untranslated --
    which is exactly how an English page keeps a handful of Arabic phrases.
    """
    catalog: dict[str, str] = {}
    key = plural = None
    forms: dict[int, str] = {}
    val = None
    field = None
    index = 0

    def flush():
        if key is None:
            return
        if plural is not None:
            ordered = [forms[i] for i in sorted(forms)]
            if all(ordered) and ordered:
                catalog[key + "\0" + plural] = "\0".join(ordered)
        elif val:
            catalog[key] = val

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        directive = _DIRECTIVE.match(line)
        if directive:
            name, text = directive.group(1), unescape(directive.group(2))
            if name == "msgid":
                flush()
                key, plural, val, forms, field = text, None, "", {}, "msgid"
            elif name == "msgid_plural":
                plural, field = text, "msgid_plural"
            else:                       # msgctxt: not used by this project
                field = "msgctxt"
            continue

        message = _MSGSTR.match(line)
        if message:
            slot, text = message.group(1), unescape(message.group(2))
            if slot is None:
                val, field = text, "msgstr"
            else:
                index = int(slot)
                forms[index] = text
                field = "msgstr[]"
            continue

        continued = _CONTINUED.match(line)
        if continued and field:
            piece = unescape(continued.group(1))
            if field == "msgid":
                key = (key or "") + piece
            elif field == "msgid_plural":
                plural = (plural or "") + piece
            elif field == "msgstr":
                val = (val or "") + piece
            elif field == "msgstr[]":
                forms[index] = forms.get(index, "") + piece

    flush()
    return catalog


def write_mo(catalog: dict[str, str], path: Path) -> None:
    header = (
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n"
        "Content-Transfer-Encoding: 8bit\n"
    )
    # The empty msgid carries the header and must sort first; gettext requires
    # the tables to be sorted by msgid.
    items = [("", catalog.get("", header))] + sorted(
        (k, v) for k, v in catalog.items() if k
    )

    ids = strs = b""
    offsets = []
    for key, value in items:
        kb, vb = key.encode("utf-8"), value.encode("utf-8")
        offsets.append((len(ids), len(kb), len(strs), len(vb)))
        ids += kb + NUL
        strs += vb + NUL

    keystart = 7 * 4 + 16 * len(items)
    valuestart = keystart + len(ids)
    koffsets: list[int] = []
    voffsets: list[int] = []
    for key_offset, key_len, val_offset, val_len in offsets:
        koffsets += [key_len, key_offset + keystart]
        voffsets += [val_len, val_offset + valuestart]

    out = struct.pack(
        "Iiiiiii",
        MAGIC,
        0,                          # revision
        len(items),
        7 * 4,                      # offset of the key table
        7 * 4 + len(items) * 8,     # offset of the value table
        0, 0,                       # hash table size / offset — unused
    )
    out += array.array("i", koffsets + voffsets).tobytes()
    out += ids + strs
    path.write_bytes(out)


def verify_mo(path: Path) -> int:
    """Load a compiled catalogue the way Django will, and report its size.

    Worth its own step because of how this fails. Django reads the catalogue
    during `django.setup()`, so a malformed .mo does not degrade the pages --
    it stops every management command, including this one. Recovering then
    means hand-editing a binary or finding a backup.

    The usual cause is a header gettext cannot read: it holds the charset, and
    without it the first non-ASCII entry raises and the whole catalogue is
    refused.
    """
    with path.open("rb") as handle:
        return len(gettext.GNUTranslations(handle)._catalog)


class Command(BaseCommand):
    help = "Compile .po files to .mo without requiring GNU gettext."

    def add_arguments(self, parser):
        parser.add_argument(
            "--locale", "-l", action="append", dest="locales", default=[],
            help="Locale to compile, e.g. ar. Repeatable. Default: all.",
        )

    def handle(self, *args, **options):
        roots = [Path(p) for p in settings.LOCALE_PATHS]
        if not roots:
            raise CommandError("LOCALE_PATHS is empty; nothing to compile.")

        wanted = set(options["locales"])
        compiled = 0

        for root in roots:
            if not root.is_dir():
                continue
            for po in sorted(root.glob("*/LC_MESSAGES/*.po")):
                locale = po.parent.parent.name
                if wanted and locale not in wanted:
                    continue
                catalog = parse_po(po)
                mo = po.with_suffix(".mo")
                # Built beside the real one and proved before it replaces it:
                # an unreadable .mo takes Django down with it.
                staged = mo.with_suffix(".mo.new")
                write_mo(catalog, staged)
                try:
                    entries = verify_mo(staged)
                except Exception as exc:
                    staged.unlink(missing_ok=True)
                    raise CommandError(
                        f"{locale}/{po.name} compiled to a catalogue that "
                        f"cannot be loaded ({exc}). The existing {mo.name} has "
                        f"been left alone."
                    ) from exc
                staged.replace(mo)
                compiled += 1
                self.stdout.write(
                    f"  {locale}/{po.name} -> {mo.name}  "
                    f"({len(catalog)} entries, {entries} readable)"
                )

        if not compiled:
            raise CommandError("No .po files matched.")
        self.stdout.write(self.style.SUCCESS(f"Compiled {compiled} catalogue(s)."))
        self.stdout.write(
            "Restart the server: compiled catalogues are read once per process."
        )
