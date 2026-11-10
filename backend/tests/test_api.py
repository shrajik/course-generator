"""API behaviour, exercised against the offline mock AI client."""

from __future__ import annotations


def _create(client, **overrides):
    payload = {
        "course_title": "Introduction to RAG",
        "toc": [{"title": "What is RAG"}, {"title": "Chunking and Embeddings"}],
        "target_audience": "Backend engineers new to LLMs",
        "dos": ["Use concrete examples"],
        "donts": ["No marketing language"],
        "template": "technical",
    }
    payload.update(overrides)
    response = client.post("/api/courses", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_reports_the_mock_client(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["ai_client"] == "mock"


def test_root_lists_the_required_endpoints(client):
    endpoints = client.get("/").json()["endpoints"]
    assert any("/api/courses/improve-toc" in e for e in endpoints)
    assert any("ai-edit" in e for e in endpoints)


def test_templates_endpoint_exposes_two_templates(client):
    templates = client.get("/api/courses/templates").json()["templates"]
    assert {t["kind"] for t in templates} == {"technical", "non_technical"}


def test_create_course_runs_the_planner(client):
    body = _create(client)
    assert body["course_id"].startswith("crs_")
    assert body["document_id"] == body["course_id"].replace("crs_", "doc_")
    assert body["status"] == "planned"
    assert body["has_blueprint"] is True
    assert [c["chapter_id"] for c in body["chapters"]] == ["chapter_1", "chapter_2"]


def test_create_course_can_skip_the_planner(client):
    body = _create(client, run_planner=False)
    assert body["status"] == "created"
    assert body["has_blueprint"] is False


def test_create_course_validates_input(client):
    response = client.post(
        "/api/courses",
        json={"course_title": "X", "toc": [], "target_audience": "Y", "template": "technical"},
    )
    assert response.status_code == 422


def test_blueprint_keeps_the_user_toc_and_reports_a_critique(client):
    course = _create(client)
    blueprint = client.get(f"/api/courses/{course['course_id']}/blueprint").json()
    assert [c["title"] for c in blueprint["chapters"]] == [
        "What is RAG",
        "Chunking and Embeddings",
    ]
    assert [c["order"] for c in blueprint["chapters"]] == [1, 2]
    assert "critique" in blueprint
    assert blueprint["learning_objectives"]


def test_improve_toc_returns_suggestions_without_saving(client):
    response = client.post(
        "/api/courses/improve-toc",
        json={
            "course_title": "Introduction to RAG",
            "toc": ["What is RAG", "Chunking"],
            "audience": "Engineers",
            "template": "technical",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["suggested_toc"]) >= 2
    assert body["changes"] and body["reasoning"]
    # Nothing was persisted.
    assert client.get("/api/courses").json()["count"] == 0


def test_generate_produces_chapters_document_and_images(client):
    course = _create(client)
    course_id = course["course_id"]
    result = client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"}).json()
    assert result["status"] == "ready"
    assert result["chapters_generated"] == ["chapter_1", "chapter_2"]
    assert result["chapters_failed"] == []
    assert result["pages"] > 2
    assert result["images_generated"] >= 1

    document = client.get(f"/api/courses/{course_id}/document").json()
    assert document["document_id"] == course["document_id"]
    assert document["pages"][0]["kind"] == "cover"
    types = {b["type"] for page in document["pages"] for b in page["blocks"]}
    assert {"heading", "paragraph", "quiz", "summary", "exercise"} <= types


def test_quality_endpoint_returns_a_report_after_generation(client):
    course = _create(client)
    course_id = course["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})

    body = client.get(f"/api/courses/{course_id}/quality").json()
    report = body["report"]
    assert report["course_id"] == course_id
    assert report["standards_status"] == "proposed"
    assert "findings" in report
    # TASK 2 (quality audit follow-up): a course that just finished
    # generating has a real historical snapshot from that same evaluation,
    # distinct from (but consistent with) the live report above.
    snapshot = body["historical_snapshot"]
    assert snapshot is not None
    assert snapshot["standards_version"] == report["standards_version"]
    assert snapshot["outcome"] in ("passed", "failed")


def test_quality_endpoint_before_generation_has_no_findings(client):
    course_id = _create(client)["course_id"]
    body = client.get(f"/api/courses/{course_id}/quality").json()
    assert body["report"]["findings"] == []
    # Never generated - no evaluation has ever run, so no snapshot exists yet.
    assert body["historical_snapshot"] is None


def test_get_course_reports_artifacts(client):
    course_id = _create(client)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    body = client.get(f"/api/courses/{course_id}").json()
    assert body["artifacts"]["blueprint"] is True
    assert body["artifacts"]["document"] is True
    assert len(body["artifacts"]["chapters"]) == 2
    assert body["artifacts"]["research"] == ["chapter_01.json", "chapter_02.json"]


def test_single_chapter_can_be_regenerated(client):
    course_id = _create(client)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    result = client.post(
        f"/api/courses/{course_id}/generate",
        json={"chapter_ids": ["chapter_2"], "force": True, "mode": "sync"},
    ).json()
    assert result["chapters_generated"] == ["chapter_2"]
    # Chapter 1 survived untouched.
    body = client.get(f"/api/courses/{course_id}").json()
    assert len(body["artifacts"]["chapters"]) == 2


def test_unknown_chapter_id_is_a_422(client):
    course_id = _create(client)["course_id"]
    response = client.post(
        f"/api/courses/{course_id}/generate", json={"chapter_ids": ["chapter_99"], "mode": "sync"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_unknown_course_is_a_404(client):
    response = client.get("/api/courses/crs_doesnotexist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_document_before_generation_is_a_404(client):
    course_id = _create(client)["course_id"]
    response = client.get(f"/api/courses/{course_id}/document")
    assert response.status_code == 404


def test_research_artifact_is_retrievable(client):
    course_id = _create(client)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    body = client.get(f"/api/courses/{course_id}/chapters/chapter_1/research").json()
    assert body["chapter_id"] == "chapter_1"
    assert body["payload"]["core_concepts"]
    assert body["payload"]["references"]


def test_ai_edit_returns_a_patch_and_applies_it(client):
    course_id = _create(client)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    document = client.get(f"/api/courses/{course_id}/document").json()
    document_id = document["document_id"]
    paragraph = next(
        b
        for page in document["pages"]
        for b in page["blocks"]
        if b["type"] == "paragraph" and b["meta"].get("origin") == "generated"
    )

    response = client.post(
        f"/api/documents/{document_id}/ai-edit",
        json={
            "selected_block_ids": [paragraph["id"]],
            "instruction": "Make this easier to understand",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["version"] == document["version"] + 1
    assert body["patch"]["operations"]
    assert all(op in SUPPORTED for op in [o["type"] for o in body["patch"]["operations"]])

    updated = client.get(f"/api/documents/{document_id}").json()
    edited = next(
        b for page in updated["pages"] for b in page["blocks"] if b["id"] == paragraph["id"]
    )
    assert edited["content"]["text"] != paragraph["content"]["text"]


SUPPORTED = {
    "update_block",
    "delete_block",
    "insert_block",
    "replace_block",
    "replace_image",
    "update_style",
}


def test_ai_edit_can_preview_without_applying(client):
    course_id = _create(client)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    document = client.get(f"/api/courses/{course_id}/document").json()
    block_id = document["pages"][2]["blocks"][0]["id"]
    body = client.post(
        f"/api/documents/{document['document_id']}/ai-edit",
        json={"selected_block_ids": [block_id], "instruction": "Shorten it", "apply": False},
    ).json()
    assert body["applied"] is False
    assert body["version"] == document["version"]


def test_ai_edit_rejects_unknown_block_ids(client):
    course_id = _create(client)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    document_id = client.get(f"/api/courses/{course_id}/document").json()["document_id"]
    response = client.post(
        f"/api/documents/{document_id}/ai-edit",
        json={"selected_block_ids": ["block_nope"], "instruction": "Fix it"},
    )
    assert response.status_code == 422
    assert "Unknown block ids" in response.json()["error"]["message"]


def test_ai_edit_restores_the_previous_image_when_regeneration_totally_fails(client, documents, monkeypatch):
    """A real, confirmed bug: `replace_image` on a `kind: "diagram"`/
    `"concept_experience"` block can fail every bounded regeneration
    attempt (the structured spec AND the whole raster-illustration
    fallback chain behind it - an ambitious replacement diagram failing
    the deterministic renderer's own geometry QA twice, then cascading
    through text-free/relevance retries and a final generic fallback, any
    single step of which can fail under real network conditions) - the
    block used to end up with NO image at all ("Image not generated yet")
    even though it had a perfectly good one a moment earlier, because
    `apply_patch` clears `path`/`generated` unconditionally before
    generation even starts. It must instead keep showing the working
    picture it had before the failed correction."""
    from app.schemas.patch import DocumentPatch

    course_id = _create(client)["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    document = client.get(f"/api/courses/{course_id}/document").json()
    document_id = document["document_id"]
    image_block = next(
        b for page in document["pages"] for b in page["blocks"]
        if b["type"] == "image" and b["content"].get("generated") and b["content"].get("path")
    )
    original_path = image_block["content"]["path"]

    async def fake_edit(*, document, selected_block_ids, instruction):
        return DocumentPatch.model_validate({
            "operations": [
                {"type": "replace_image", "block_id": image_block["id"], "prompt": "fix the overlap"}
            ],
            "reasoning": "test",
        })

    async def fake_generate_missing(*, document, template, only_block_ids=None, force=False, on_progress=None, job_id=""):
        # Simulate every bounded attempt failing, exactly like the real
        # chain's own final state on total failure - no block actually got
        # a working image.
        return 0

    monkeypatch.setattr(documents.editor, "edit", fake_edit)
    monkeypatch.setattr(documents.images, "generate_missing", fake_generate_missing)

    response = client.post(
        f"/api/documents/{document_id}/ai-edit",
        json={"selected_block_ids": [image_block["id"]], "instruction": "fix the overlap"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["images_failed"] == [image_block["id"]]

    updated = client.get(f"/api/documents/{document_id}").json()
    restored = next(b for page in updated["pages"] for b in page["blocks"] if b["id"] == image_block["id"])
    assert restored["content"]["path"] == original_path
    assert restored["content"]["generated"] is True


def test_replace_image_regenerated_to_nothing_detects_a_total_failure():
    from app.services.document_service import _replace_image_regenerated_to_nothing

    had_image = {"path": "assets/image_001.svg", "generated": True}
    no_image = {"path": None, "generated": False}
    still_has_image = {"path": "assets/image_002.svg", "generated": True}

    assert _replace_image_regenerated_to_nothing(had_image, no_image) is True
    assert _replace_image_regenerated_to_nothing(had_image, still_has_image) is False
    # Nothing to fall back to in the first place - restoring would just
    # swap one blank state for another, identical one.
    assert _replace_image_regenerated_to_nothing(no_image, no_image) is False


def test_non_technical_course_uses_its_own_template(client):
    course = _create(
        client,
        course_title="Practical Negotiation for Managers",
        toc=[{"title": "Preparing to Negotiate"}],
        template="non_technical",
        target_audience="First-time managers",
    )
    course_id = course["course_id"]
    client.post(f"/api/courses/{course_id}/generate", json={"mode": "sync"})
    document = client.get(f"/api/courses/{course_id}/document").json()
    assert document["template_id"] == "non_technical_v1"
    types = {b["type"] for page in document["pages"] for b in page["blocks"]}
    assert "story" in types and "case_study" in types
    assert "code" not in types
