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
