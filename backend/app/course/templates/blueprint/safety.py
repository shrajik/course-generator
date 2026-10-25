"""Upload safety for template files.

Two separate concerns:

* `check_docx_archive` - a .docx is a ZIP archive. It is inspected *before*
  python-docx or mammoth open it, so a corrupted file gets a clear message and
  a file built to expand enormously (a "zip bomb") is refused before anything
  decompresses it.
* `is_prompt_injection` - text in an uploaded template is data. Lines that
  try to talk to the AI about its own instructions are dropped from the
  blueprint and listed for the uploader, instead of reaching a prompt.
"""

from __future__ import annotations

import io
import re
import zipfile

from app.core.errors import ValidationFailedError

MAX_ARCHIVE_MEMBERS = 2000
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
# Real DOCX parts compress at well under 100:1; a crafted bomb is far above it.
MAX_COMPRESSION_RATIO = 200
_RATIO_MIN_BYTES = 1024 * 1024  # tiny parts can have silly ratios harmlessly
_REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")


def check_docx_archive(data: bytes) -> None:
    """Raise ValidationFailedError unless `data` is a sane, readable DOCX."""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValidationFailedError(
            "This file is not a valid .docx document (it is not a Word archive). "
            "It may be corrupted, or it may be an older .doc file - save it as .docx and try again."
        )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValidationFailedError("This .docx contains too many internal parts to be a normal document.")
            total = 0
            for member in members:
                if member.flag_bits & 0x1:
                    raise ValidationFailedError("Password-protected .docx files are not supported.")
                total += member.file_size
                if (
                    member.file_size > _RATIO_MIN_BYTES
                    and member.compress_size > 0
                    and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ValidationFailedError(
                        "This .docx expands to an unreasonable size when opened and was refused."
                    )
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValidationFailedError(
                    "This .docx expands to more than 150 MB when opened and was refused."
                )
            names = {member.filename for member in members}
            missing = [part for part in _REQUIRED_PARTS if part not in names]
            if missing:
                raise ValidationFailedError(
                    "This file is not a Word document (word/document.xml is missing). It may be corrupted."
                )
            # Sizes are bounded above, so a full CRC pass is now safe.
            bad = archive.testzip()
            if bad is not None:
                raise ValidationFailedError(f"This .docx is corrupted (damaged part: {bad}).")
    except ValidationFailedError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, ValueError) as exc:
        raise ValidationFailedError(f"This .docx is corrupted and could not be opened ({exc}).") from exc


_INJECTION_RES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|above|prior|earlier|all|system|your)\b.{0,20}\b(instructions?|rules?|prompts?|guidelines?)\b",
        r"\bsystem prompt\b",
        r"\byou are (now|no longer)\b",
        r"\b(act|behave) as (an?|the) (?!learner|student|instructor|teacher)",
        r"\b(reveal|print|show|output|repeat)\b.{0,30}\b(prompt|instructions|api[ _-]?key|secret|password|token)s?\b",
        r"<\s*/?\s*(script|iframe|system|assistant)\b",
        r"\bjailbreak\b",
        r"\bdeveloper mode\b",
        r"\bdo not (follow|obey)\b.{0,30}\b(rules|instructions|guidelines)\b",
    )
)


def is_prompt_injection(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _INJECTION_RES)
