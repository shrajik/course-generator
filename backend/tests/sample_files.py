"""Tiny real files for the ingestion tests: a PDF, a DOCX and some text."""

from __future__ import annotations

import io


def make_pdf(pages: list[list[str]]) -> bytes:
    """A valid PDF with one text line per list item on each page. An empty page
    has no text layer at all, like a scanned page."""
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body if isinstance(body, bytes) else body.encode("latin-1"))
        return len(objects)

    catalog = add("")  # placeholders, filled in below
    page_tree = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    for lines in pages:
        text = "BT /F1 12 Tf 72 720 Td 16 TL "
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            text += f"({escaped}) Tj T* "
        text += "ET" if lines else ""
        stream = text.encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {page_tree} 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
            )
        )
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {page_tree} 0 R >>".encode("latin-1")
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[page_tree - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def make_docx(*, with_table: bool = True) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading("Refund Policy", level=1)
    document.add_paragraph("Refunds are issued within fourteen days of a valid request.")
    document.add_heading("Eligibility", level=2)
    document.add_paragraph("A customer is eligible when the product is unused and returned.")
    document.add_paragraph("Digital goods are never refundable.", style="List Bullet")
    if with_table:
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "Region"
        table.rows[0].cells[1].text = "Deadline"
        table.rows[1].cells[0].text = "EU"
        table.rows[1].cells[1].text = "30 days"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


POLICY_TEXT = (
    "REFUND POLICY\n\n"
    "Refunds are issued within fourteen days of a valid request.\n\n"
    "1.1 Eligibility\n\n"
    "A customer is eligible for a refund when the product is unused and returned in its "
    "original packaging. Digital goods are never refundable.\n\n"
    "1.2 Escalation\n\n"
    "Disputed refunds are escalated to the billing team, who respond within two business days.\n"
).encode("utf-8")
