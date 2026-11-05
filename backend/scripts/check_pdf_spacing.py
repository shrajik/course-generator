#!/usr/bin/env python
"""CLI: check an already-generated course's PDF pagination/spacing.

    python scripts/check_pdf_spacing.py <document_id> [--export]

Loads the stored CourseDocument, runs the same QA gate the generation
pipeline runs automatically after images are inserted (see
app.services.pdf_spacing_qa and its own wiring in app.services.course_service
CourseService._finalise), and prints a pass/fail report. Exits 1 on failure
so it can be used as a CI/manual gate on top of whatever the pipeline already
checked.

--export additionally exports a real PDF (if one isn't already on disk) and
reads its real page count back with pypdf, cross-checking it against the
document's own footer total - the one check that needs the actual rendered
file rather than just the stored layout data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.document_service import get_document_service  # noqa: E402
from app.services.pdf_spacing_qa import check_pdf_spacing  # noqa: E402
from app.services.storage_service import get_storage  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document_id")
    parser.add_argument(
        "--export", action="store_true",
        help="export a fresh PDF (or use the one already on disk) and cross-check its real page count",
    )
    args = parser.parse_args()

    documents = get_document_service()
    document = await documents.load(args.document_id)

    pdf_path: Path | None = None
    if args.export:
        pdf_path, document = await documents.export_pdf(args.document_id)
    else:
        storage = get_storage()
        candidate = storage.pdf_path(document.course_id)
        if candidate.exists():
            pdf_path = candidate

    report = check_pdf_spacing(document, pdf_path=pdf_path)
    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
