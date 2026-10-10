"""Private contract storage (§4, §47).

Contract files never sit under a web-served directory and never get a permanent
public URL. Access goes through an authenticated view, optionally carrying a
short-lived signed token.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils.translation import gettext as _

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# PDF / OLE2 / ZIP(docx) magic numbers. The declared content type is a client
# claim; the first bytes are evidence.
_MAGIC = {
    b"%PDF-": ".pdf",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1": ".doc",
    b"PK\x03\x04": ".docx",
}

_signer = TimestampSigner(salt="aji.documents.source_file")


@dataclass(frozen=True)
class StoredFile:
    stored_path: str          # relative to PRIVATE_MEDIA_ROOT
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str


def safe_filename(name: str) -> str:
    """Strip anything that could escape the storage root or confuse the OS.

    Arabic filenames are common here, so this preserves Unicode letters rather
    than flattening to ASCII — it removes path syntax, not language.
    """
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("\\", "/").split("/")[-1]      # defeat traversal
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "", name)     # control + reserved
    name = name.strip(" .") or "contract"
    return name[:180]


def _sniff_extension(head: bytes) -> str | None:
    for magic, ext in _MAGIC.items():
        if head.startswith(magic):
            return ext
    return None


def validate_upload(uploaded_file) -> tuple[str, str]:
    """Validate an incoming file. Returns (extension, content_type).

    Raises ValidationError with a message safe to show a user.
    """
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise ValidationError(
            _("حجم الملف %(size)s ميغابايت، والحد المسموح %(limit)s ميغابايت.") % {
                "size": f"{uploaded_file.size / 1024 / 1024:.1f}", "limit": settings.MAX_UPLOAD_MB}
        )
    if uploaded_file.size == 0:
        raise ValidationError(_("الملف فارغ."))

    name = safe_filename(uploaded_file.name)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            _("نوع الملف غير مدعوم (%(ext)s). استخدم PDF أو DOC أو DOCX.") % {
                "ext": ext or _("غير معروف")}
        )

    uploaded_file.seek(0)
    head = uploaded_file.read(8)
    uploaded_file.seek(0)
    sniffed = _sniff_extension(head)
    if sniffed is None:
        raise ValidationError(_("محتوى الملف لا يبدو مستند PDF أو Word."))
    # .docx and .doc are both Word; only reject a genuine mismatch such as a
    # PDF renamed to .docx.
    if (sniffed == ".pdf") != (ext == ".pdf"):
        raise ValidationError(
            _("امتداد الملف %(ext)s لكن محتواه %(sniffed)s.") % {"ext": ext, "sniffed": sniffed}
        )

    content_type = (
        uploaded_file.content_type
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )
    return ext, content_type


def store(uploaded_file, *, contract_id) -> StoredFile:
    """Write the upload into private storage and return its descriptor."""
    ext, content_type = validate_upload(uploaded_file)
    original = safe_filename(uploaded_file.name)

    # Opaque on-disk name: the stored path never reveals client or project.
    relative = Path("contracts") / str(contract_id) / f"{uuid.uuid4().hex}{ext}"
    absolute = Path(settings.PRIVATE_MEDIA_ROOT) / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    size = 0
    uploaded_file.seek(0)
    with open(absolute, "wb") as handle:
        for chunk in uploaded_file.chunks():
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)

    return StoredFile(
        stored_path=str(relative).replace("\\", "/"),
        original_filename=original,
        content_type=content_type,
        size_bytes=size,
        sha256=digest.hexdigest(),
    )


def absolute_path(stored_path: str) -> Path:
    """Resolve a stored path, refusing anything outside the storage root."""
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    candidate = (root / stored_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValidationError("Invalid stored path.")
    return candidate


def make_access_token(source_file_id) -> str:
    """Short-lived token for one file.

    Still only usable by an authenticated user with access — the token limits
    the window, it does not replace the permission check.
    """
    return _signer.sign(str(source_file_id))


def read_access_token(token: str) -> str:
    try:
        return _signer.unsign(token, max_age=settings.SIGNED_URL_TTL_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError(_("انتهت صلاحية رابط التنزيل.")) from exc
    except BadSignature as exc:
        raise ValidationError(_("رابط التنزيل غير صالح.")) from exc


def delete(stored_path: str) -> None:
    """Remove bytes from disk. Used only when a record is hard-deleted."""
    try:
        absolute_path(stored_path).unlink(missing_ok=True)
    except ValidationError:
        pass
