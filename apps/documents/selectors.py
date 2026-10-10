"""Read-side helpers for documents."""
from __future__ import annotations

#: Problems in the file a reader of the contract page or the report must
#: see -- in the automatic flow no one opens the confirmation screen, so a
#: contract stating two delay-damage rates would otherwise go unmentioned.
_NOTE_KINDS = ("figure_conflict", "figure_unsettled", "words_vs_figure", "iban_checksum", "placeholder")


def file_notes(document) -> list[dict]:
    """The file's own problems worth reporting, conflicts first, then the
    summary of the OCR + vision figure check."""
    if document is None:
        return []
    warnings = document.warnings or []
    order = {kind: i for i, kind in enumerate(_NOTE_KINDS)}
    notes = sorted((w for w in warnings if w.get("kind") in order),
                   key=lambda w: (order[w["kind"]], w.get("page") or 0))
    notes += [w for w in warnings if w.get("kind") == "figure_check"][:1]
    return notes
