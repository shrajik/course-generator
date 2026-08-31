"""The complete backend flow, in the order the spec requires.

    create course -> improve TOC -> blueprint -> research -> generate chapter
    -> review -> course document JSON -> image -> AI edit -> PDF

Runs entirely offline against the mock AI client.
"""

from __future__ import annotations

import pytest

from tests.test_pdf import needs_browser

TECHNICAL_PAYLOAD = {
    "course_title": "Introduction to RAG",
    "toc": [
        {"title": "What is RAG", "sections": ["Motivation", "Architecture"]},
        {"title": "Chunking and Embeddings"},
        {"title": "Evaluating a RAG System"},
    ],
    "target_audience": "Backend engineers new to LLMs",
    "dos": ["Use concrete, runnable examples", "Define jargon on first use"],
    "donts": ["No marketing language", "Do not cite unverified benchmarks"],
    "template": "technical",
}


@needs_browser
@pytest.mark.parametrize(
    "template,title",
    [("technical", "Introduction to RAG"), ("non_technical", "Practical Negotiation")],
)
def test_full_backend_flow(client, template, title):
    # 1. Improve the TOC (advisory only).
    improved = client.post(
        "/api/courses/improve-toc",
        json={
            "course_title": title,
            "toc": [item["title"] for item in TECHNICAL_PAYLOAD["toc"]],
            "audience": TECHNICAL_PAYLOAD["target_audience"],
            "template": template,
        },
    )
    assert improved.status_code == 200
    suggested = improved.json()["suggested_toc"]
    assert suggested

    # 2. Create the course with the approved TOC -> planner -> blueprint.
    payload = {
        **TECHNICAL_PAYLOAD,
        "course_title": title,
        "template": template,
        "toc": [{"title": item["title"]} for item in suggested],
    }
    created = client.post("/api/courses", json=payload)
    assert created.status_code == 201, created.text
    course = created.json()
    course_id, document_id = course["course_id"], course["document_id"]
    assert course["status"] == "planned"

    blueprint = client.get(f"/api/courses/{course_id}/blueprint").json()
    assert len(blueprint["chapters"]) == len(suggested)

    # 3-6. Research, write, review, assemble, illustrate.
    result = client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"}).json()
    assert result["status"] == "ready", result
    assert result["chapters_failed"] == []
    assert len(result["chapters_generated"]) == len(suggested)
    assert result["images_generated"] >= 1
    assert result["pages"] >= 4

    # Research artifacts exist per chapter.
    for chapter_id in result["chapters_generated"]:
        research = client.get(
            f"/api/courses/{course_id}/chapters/{chapter_id}/research"
        ).json()
        assert research["payload"]["core_concepts"]
        chapter = client.get(f"/api/courses/{course_id}/chapters/{chapter_id}").json()
        assert chapter["chapter"]["summary"]
        assert chapter["chapter"]["review"]["approved"] is True

    # 7. Course Document JSON is the source of truth.
    document = client.get(f"/api/courses/{course_id}/document").json()
    assert document["document_id"] == document_id
    assert document["pages"][0]["kind"] == "cover"
    blocks = [b for page in document["pages"] for b in page["blocks"]]
    assert all({"content", "style", "layout"} <= set(b) for b in blocks)

    # 8. Images are on disk and referenced from the document.
    images = [b for b in blocks if b["type"] == "image"]
    assert images and all(b["content"]["path"].startswith("assets/") for b in images)

    # 9. AI edit returns a patch, not a document, and applies it.
    target = next(
        b for b in blocks if b["type"] == "paragraph" and b["meta"]["origin"] == "generated"
    )
    edit = client.post(
        f"/api/documents/{document_id}/ai-edit",
        json={
            "selected_block_ids": [target["id"]],
            "instruction": "Make this easier to understand",
        },
    ).json()
    assert edit["applied"] is True
    assert edit["version"] == document["version"] + 1
    assert edit["patch"]["operations"]
    assert "pages" not in edit["patch"]  # a patch, never a whole document

    edited_document = client.get(f"/api/documents/{document_id}").json()
    edited_block = next(
        b
        for page in edited_document["pages"]
        for b in page["blocks"]
        if b["id"] == target["id"]
    )
    assert edited_block["content"]["text"] != target["content"]["text"]

    # 10. PDF export reflects the current document.
    exported = client.post(
        f"/api/documents/{document_id}/export/pdf", params={"download": False}
    ).json()
    assert exported["pages"] == len(edited_document["pages"])
    assert exported["size_bytes"] > 10_000
    assert exported["pdf_path"].endswith("exports/course.pdf")

    streamed = client.post(f"/api/documents/{document_id}/export/pdf")
    assert streamed.status_code == 200
    assert streamed.headers["content-type"] == "application/pdf"
    assert streamed.content.startswith(b"%PDF-")

    # The HTML preview renders from the same document.
    preview = client.get(f"/api/documents/{document_id}/preview")
    assert preview.status_code == 200
    assert "<html" in preview.text
