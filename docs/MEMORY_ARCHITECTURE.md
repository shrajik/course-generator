# Memory architecture

The platform has two memories. They are deliberately separate: different storage, lifecycle,
permissions and trust. They meet in two controlled places only (see "How they connect").

| | Memory 1: Knowledge base | Memory 2: Artifact memory |
|---|---|---|
| What it holds | Reference knowledge admins upload or write | Chapters, visuals, question sets, activities and case studies AI generated for courses |
| Who decides what is trusted | An admin publishes it | An SME approves it |
| Lifecycle | draft / published / unpublished / archived (+ trash, purge) | pending review / approved (locked) / changes requested; artifact active / archived |
| Mutability | Editable; a new version replaces the live one only when an admin publishes it | **Approved versions are immutable**; the database enforces it |
| Citation in generated text | `[G12.5]` knowledge item 12, passage 5 | `[A7.2]` artifact 7, version 2 |
| Tables | `kb_*` (migration 0012) | `am_*` (migration 0013) |
| Code | `app/kb`, `app/services/knowledge_*` | `app/artifacts` |
| API | `/api/kb` (admin), `/api/knowledge` (legacy, kept) | `/api/artifacts` (reviewer / admin) |
| Screen | Admin > Knowledge | `/review/artifacts` (SMEs), Admin > Approved artifacts |

The existing `MemoryService` / `memory_embeddings` (template guidance and generation-history notes for the
reviewer stage) is a third, older thing and is **not** either memory. It is untouched.

## Memory 1: the knowledge base

* **Item** (what an admin manages) > **version** (immutable snapshot) > **passages** (immutable chunks). One
  version can draw on several files and on text typed in directly.
* **Lifecycle and processing are separate fields.** `lifecycle_status` is the admin's decision;
  `processing_status` is what the pipeline has done; a job's status is whether a worker is on it. A version
  that finished processing is **not** live: only an admin's publish makes it so, atomically (one transaction,
  row-locked, with a stale-view check that returns 409 instead of overwriting).
* **Citations never change meaning.** A passage's `ordinal` is its identity, never reused. Editing a document
  keeps the ordinals of unchanged passages (identical text, in any position, from any version of the file) and
  gives changed ones new numbers. Old citations resolve to the text they cited, even after replacement
  (`GET /api/kb/citations/G2.5`). Only a purge ends that.
* **Only live passages are searched.** `is_live` is true exactly for the live version of a published,
  non-deleted item; the vector (HNSW) and keyword indexes are partial on it. `KbService.check_invariants()`
  verifies it.
* **Ingestion is durable.** A job row per version; a worker (`app/kb/worker.py`) claims it with a lease and
  heartbeat (`FOR UPDATE SKIP LOCKED`, safe with several processes). Stages persist their progress: a retry
  re-embeds only the passages still missing a vector. Permanent errors fail at once with a message; transient
  ones retry with backoff; when embedding retries run out the version still finishes, searchable by keyword,
  with a warning. Identical text already embedded anywhere is never embedded again.
* **The admin supplies the file; the system proposes the rest.** Title, tags, category (nearest category
  centroid, no model call), section topics and, optionally, one small-model call for a title and description.
  Proposals fill in what the admin left blank on a draft and are always reviewable.
* **Retrieval** is BM25 + cosine fused (the same arithmetic the file library used, ported and tested equal),
  with the same relevance floors. Options (`KNOWLEDGE_*`): relationship rules (superseded and duplicate items),
  near-duplicate removal, extractive compression, related-item expansion, a result cache invalidated by the
  knowledge epoch, and category/tag/topic scopes. Those that change which passages courses see default off.
* **Migration.** `python -m app.scripts.kb_import` copies the file library in (numbers, citations, vectors
  bit-for-bit, no re-embedding) and `--verify`s it. `KB_SHADOW_COMPARE=true` compares every search against the
  files in the background. `KB_BACKEND=postgres` switches courses over. See `docs/KB_CUTOVER_RUNBOOK.md`.

## Memory 2: artifact memory

Lifecycle: AI generation > artifact created > **SME review** > approved / changes requested > **locked** >
indexed > reusable.

* **Capture follows the existing review workflow**, without changing it. Submitting a course for review
  extracts its artifacts (`app/artifacts/extract.py`): a chapter artifact per chapter, plus its visuals,
  question sets, activities and case studies as independently reusable artifacts. Approving the course approves
  and locks them with it (after re-capturing the document, so what is approved is what the reviewer last saw);
  sending it back returns them. Reviewers can also approve one artifact on its own (`/api/artifacts`).
  Failures here are logged, never block the review.
* **Identity is stable.** An artifact's key comes from the chapter or block id, so resubmitting finds the same
  artifacts. Content hashes ignore block ids: the same words are the same content.
* **Approved is locked, in the database.** A trigger refuses any change to an approved version's content,
  provenance or review record, and refuses deleting it. What may still change: which approved version is
  active, and a missing vector filled in once. Editing approved work is `new_version`: a new pending version
  that needs its own approval; the approved one keeps being offered meanwhile. Old versions stay available by
  citation.
* **Only approved, active versions are retrieved.** Pending, changes-requested, superseded and archived
  versions are not merely filtered out: they are not in the index.
* **Provenance** per version: course and chapter, generation run id, model info, the knowledge and source
  citations it was written from, who approved it and when, plus an activity history and a log of which
  chapters were offered which artifact.
* **Graph.** `am_links`: `belongs_to_course`, `contains_chapter`, `contains_visual`, `contains_questions`,
  `references_knowledge`, `derived_from`, `related_to`, `supersedes` (no cycles). Captured automatically on
  submit where derivable; admins can add more.
* **Reuse in generation.** While a chapter is written, approved artifacts of *other* courses are offered to the
  writer ("APPROVED PRIOR MATERIAL ... cite [A7.2]"), cut extractively to `ARTIFACT_EXCERPT_CHARS` each, and
  recorded in the chapter's sources used. Duplicates are removed. A course never sees its own artifacts.

## How they connect

1. **A link names a knowledge passage; it never joins the stores.** `am_links.target_kb_number/ordinal` holds
   `G12.5` as numbers. There is no foreign key between `kb_*` and `am_*`, and neither side writes to the other.
2. **Retrieval offers both side by side, each under its own rules.** When an artifact is offered, the knowledge
   passages it was written from are fetched through Memory 1's own retrieval with `live_only`: if an admin has
   unpublished or replaced that knowledge, it is **not** offered, however old the artifact is. Such passages
   keep their `[G..]` citation, so the fact-check audit still applies to them, and they are kept within the
   knowledge character budget.

## Configuration

Knowledge base: `KB_BACKEND`, `KB_WORKER_ENABLED`, `KB_JOB_MAX_ATTEMPTS`, `KB_AI_METADATA`,
`KB_SHADOW_COMPARE`, `KB_PURGE_ENABLED` (off), `KNOWLEDGE_GRAPH_RULES`, `KNOWLEDGE_DEDUPE`,
`KNOWLEDGE_COMPRESS`, `KNOWLEDGE_RELATED_EXPANSION`, `KNOWLEDGE_CACHE_SIZE`, `EMBEDDING_QUERY_CACHE_SIZE`.
Artifact memory: `ENABLE_ARTIFACT_MEMORY` (capture), `ENABLE_ARTIFACT_REUSE`, `ARTIFACT_EXCERPTS_PER_CHAPTER`,
`ARTIFACT_EXCERPT_CHARS`, `ARTIFACT_TOTAL_CHARS`, `ARTIFACT_MIN_SEMANTIC`, `ARTIFACT_MIN_COVERAGE`. Both memories
need `USE_DATABASE=true` and pgvector 0.5+ (0.8+ recommended for filtered search; the image is pinned).

## Operating notes

* Apply migrations with `alembic upgrade head` (0012 knowledge base, 0013 artifact memory). Both are additive.
  The 0013 downgrade drops approved versions on purpose (the lock is lifted for it); do not downgrade on a
  database holding approvals you want to keep.
* Approvals made while the embedding provider was down are approved but not indexed (keyword search only); run
  `POST /api/artifacts/index-missing` (admin) once it is back.
* Back up before cutover (`docs/KB_CUTOVER_RUNBOOK.md`); approved artifacts are immutable, so a restore is the
  only way back from a mistaken approval of the *content*: approving is a decision, not a draft.

## Tests

`backend/tests/test_kb_*.py` and `test_artifact_*.py` (database tests skip without `TEST_DATABASE_URL`; start
`docker compose --profile test up -d postgres-test`). The lock, the live invariants, atomic publishing,
concurrent publishing, crash recovery and retry have dedicated tests, including randomised sequences of admin
actions that check the invariants after every step. Frontend: `npm test` (`src/lib/kb`, `src/lib/artifacts`).

## Known limits

* Artifact keyword search and excerpts cover up to 60,000 characters of an artifact; its embedding is made from a
  4,000-character sample spread across the whole text (`embedding_text`), not just its opening.
* The fact-check audit checks `[G..]`/`[S..]` citations only; claims taken from an approved artifact are not
  re-audited (the content was approved by an SME).
* Artifact identity rests on block ids: if a chapter is regenerated with new block ids, its blocks become new
  artifacts (the chapter itself keeps its identity).
* Artifacts are reviewed at `/review/artifacts` (reviewers and admins; archiving stays admin-only) and
  managed at Admin > Approved artifacts. SMEs can still approve a whole course in the existing review screen.
* Classifier thresholds (category assignment, section topics) are uncalibrated starting points.
* Permanent purge of knowledge is implemented but disabled (`KB_PURGE_ENABLED`) while the cutover rollback
  window is open.
