# AI Course Creation Platform — MVP

Turns `TOC + audience + do's/don'ts + template` into `research + course content + images + editable Course JSON + PDF`,
with a four-screen Next.js front end over the top:

```
Create Course → Customize TOC → Generate → Canva-like Course Editor
→ select anything → ask AI → apply change → Export PDF
```

FastAPI backend + Next.js frontend, backed by PostgreSQL with cookie-based JWT auth and role-based admin
access. No queue, no object storage — those come in a later phase.

```
Course Input → Planner → Blueprint → Deep Research → Research Artifacts
            → Chapter Writer → Chapter Reviewer → Course Document JSON
            → Image Generation → PDF Renderer → Final PDF
```

**The Course Document JSON is the source of truth. The PDF is only an export.**

---

## Quick start

```bash
cp .env.example .env      # then put your OPENAI_API_KEY in it
docker compose up --build -d
docker compose exec backend alembic upgrade head   # first run only, see below
```

- App: <http://localhost:3000>
- API docs: <http://localhost:8000/docs>

No Python or Node installation needed on the host. Generated artifacts land in `backend/data/` on your machine
(bind-mounted), so you can inspect every blueprint, research file, chapter, document and PDF directly.

Useful follow-up commands:

```bash
docker compose ps                 # container status / health
docker compose logs -f backend    # tail one service's logs
docker compose logs -f frontend
docker compose down               # stop and remove containers (keeps the postgres volume)
docker compose down -v            # also wipe the postgres volume (destroys the database)
docker compose up --build -d      # rebuild after a Dockerfile/dependency change
```

### Database & migrations

Compose starts a `postgres:16-alpine` container and the backend connects to it via `DATABASE_URL`
(defaulted in `docker-compose.yml` to the `postgres` service). Schema changes are managed with
**Alembic** and are *not* applied automatically on container start, so after the first `up` — and after
pulling any change that adds a migration under `backend/alembic/versions/` — run:

```bash
docker compose exec backend alembic upgrade head    # apply pending migrations
docker compose exec backend alembic current          # check what's applied
docker compose exec backend alembic heads             # check the latest available migration
```

If `alembic current` doesn't match `alembic heads`, endpoints touching the missing tables/columns will
fail — run `upgrade head` again.

You also need two secrets set in `.env` before auth will work (empty values make login/register fail):

```bash
JWT_SECRET=<random 32+ byte string>
JWT_REFRESH_SECRET=<a different random 32+ byte string>
```

Generate them with `python -c "import secrets; print(secrets.token_urlsafe(48))"` (or any equivalent).
Optionally set `INITIAL_ADMIN_EMAIL` in `.env` before the *first* registration — the first account created
with that exact email is auto-promoted to `role=admin`; everyone else registers as `author`.

### Running it without an API key

If `OPENAI_API_KEY` is empty (or `MOCK_OPENAI=true`), the backend uses a built-in **offline mock client**
that returns schema-valid placeholder content. The entire pipeline — planning, research, writing, review,
document assembly, image generation, patch editing, PDF export — runs end to end with no network calls.
This is what the test suite uses, and it's the fastest way to see the shape of the output.

### Local development without Docker

Needs a Postgres instance reachable at the URL you set for `DATABASE_URL` (e.g. run just the `postgres`
service with `docker compose up postgres -d`, or point at any local Postgres 16).

```bash
# backend
cd backend
pip install -r requirements.txt
python -m playwright install chromium
alembic upgrade head    # apply migrations against DATABASE_URL from .env
uvicorn app.main:app --reload

# frontend (separate shell)
cd frontend
npm install
npm run dev            # http://localhost:3000
```

`NEXT_PUBLIC_API_URL` points the frontend at the API (default `http://localhost:8000`). It is read in the
browser, so under Docker it stays `localhost` rather than the internal `backend` service name.

---

## Try the whole flow

```bash
# 1. Ask for TOC improvements (advisory — nothing is saved)
curl -s localhost:8000/api/courses/improve-toc -H 'content-type: application/json' -d '{
  "course_title": "Introduction to RAG",
  "toc": ["What is RAG", "Chunking and Embeddings", "Evaluating a RAG System"],
  "audience": "Backend engineers new to LLMs",
  "template": "technical"
}' | jq

# 2. Create the course (runs the planner → blueprint)
COURSE=$(curl -s localhost:8000/api/courses -H 'content-type: application/json' -d '{
  "course_title": "Introduction to RAG",
  "toc": [
    {"title": "What is RAG", "sections": ["Motivation", "Architecture"]},
    {"title": "Chunking and Embeddings"},
    {"title": "Evaluating a RAG System"}
  ],
  "target_audience": "Backend engineers new to LLMs",
  "dos": ["Use concrete, runnable examples", "Define jargon on first use"],
  "donts": ["No marketing language", "Do not cite unverified benchmarks"],
  "template": "technical"
}')
CID=$(echo "$COURSE" | jq -r .course_id)
DID=$(echo "$COURSE" | jq -r .document_id)

# 3. Research + write + review + assemble + illustrate.
#    Backgrounded: this returns a job id straight away.
curl -s -X POST localhost:8000/api/courses/$CID/generate -H 'content-type: application/json' -d '{}' | jq

# 3b. Watch it work (progress, ETA, per-phase timings)
watch -n2 "curl -s localhost:8000/api/courses/$CID/run | jq '.run | {state, chapters_done, chapters_total, eta_seconds}'"

#     …or block until it finishes instead
curl -s -X POST localhost:8000/api/courses/$CID/generate -H 'content-type: application/json' \
  -d '{"mode": "sync"}' | jq '{status, chapters_generated, pages, timings}'

# 4. Inspect the course document (exists as soon as the first chapter is written)
curl -s localhost:8000/api/courses/$CID/document | jq '.pages | length'

# 5. Regenerate a single chapter only
curl -s -X POST localhost:8000/api/courses/$CID/generate -H 'content-type: application/json' \
  -d '{"chapter_ids": ["chapter_2"], "force": true, "mode": "sync"}' | jq

# 6. AI-edit one block (returns a PATCH, not a document)
BLOCK=$(curl -s localhost:8000/api/courses/$CID/document \
  | jq -r '[.pages[].blocks[] | select(.type=="paragraph")][0].id')
curl -s -X POST localhost:8000/api/documents/$DID/ai-edit -H 'content-type: application/json' \
  -d "{\"selected_block_ids\": [\"$BLOCK\"], \"instruction\": \"Make this easier to understand\"}" | jq

# 7. Export the PDF
curl -s -X POST "localhost:8000/api/documents/$DID/export/pdf" -o course.pdf
```

There is also `GET /api/documents/{document_id}/preview`, which serves the exact HTML the PDF is
rendered from — much faster than re-exporting while you iterate on styling.

---

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/courses` | Create a course; runs the planner (`"run_planner": false` to skip) |
| POST | `/api/courses/improve-toc` | Propose a better TOC + changes + reasoning (never applied automatically) |
| POST | `/api/courses/{course_id}/generate` | Research → write → review → assemble → illustrate. Backgrounded by default; `{"mode":"sync"}` blocks, `{"resume":true}` continues an interrupted run |
| GET | `/api/courses/{course_id}/run` | Progress, ETA and timings for the current or last run |
| GET | `/api/courses/{course_id}` | Status, progress and which artifacts exist |
| GET | `/api/courses/{course_id}/document` | The Course Document JSON |
| PUT | `/api/documents/{document_id}` | Persist manual editor changes (move/resize/type/insert/delete/style) |
| POST | `/api/documents/{document_id}/ai-edit` | Instruction on selected blocks → validated patch |
| POST | `/api/documents/{document_id}/export/pdf` | Render the current document to PDF |

Supporting endpoints: `GET /health`, `GET /api/courses`, `GET /api/courses/templates`,
`GET /api/courses/{id}/blueprint`, `GET /api/courses/{id}/chapters/{chapter_id}`,
`GET /api/courses/{id}/chapters/{chapter_id}/research`, `GET /api/courses/{id}/template`,
`GET /api/documents/{id}`, `GET /api/documents/{id}/preview`, `GET /api/documents/{id}/assets/{name}`.

---

## Auth & admin

Auth is JWT-based, issued as `HttpOnly` cookies (`access_token` 30 min, `refresh_token` 1 day by default —
tune with `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS`). Roles are `author` (default on
self-register), `editor_reviewer` and `admin`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/register` | Create an account, sets auth cookies |
| POST | `/auth/login` | Log in, sets auth cookies |
| POST | `/auth/refresh` | Rotate access/refresh tokens from the refresh cookie |
| POST | `/auth/logout` | Clear auth cookies, revoke the session |
| GET | `/auth/me` | Current user from the access cookie |

```bash
curl -s -c cookies.txt localhost:8000/auth/register -H 'content-type: application/json' \
  -d '{"email":"me@example.com","password":"a-strong-password","full_name":"Me"}' | jq
curl -s -b cookies.txt localhost:8000/auth/me | jq
```

`/admin/*` requires the `admin` role (see `INITIAL_ADMIN_EMAIL` above for bootstrapping the first one):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/health` | Admin-scoped health check |
| GET | `/admin/dashboard` | Aggregate counts across users/courses |
| GET | `/admin/users` | List users |
| GET | `/admin/users/{user_id}` | User detail |
| GET | `/admin/courses` | List courses across all owners |
| GET | `/admin/courses/{course_id}` | Course detail with owner/review info |

Course ownership and the review workflow (`draft → in_review → approved`, or `in_review →
changes_requested → in_review` on rejection) are enforced per-request in `app/api/course_authorization.py`:
an `author` (or `admin`) owns submit/resubmit actions on their own courses; an `editor_reviewer` can
view/edit any course once it leaves `draft`, and approve/request-changes on any course regardless of
ownership.

---

## The two templates

There is **one** AI pipeline (planner → research → writer → reviewer → editor). The template only changes
content structure and default presentation rules, and is passed to the agents as *data*:
`backend/app/course/templates/technical_v1.json` and `non_technical_v1.json`.

| | Technical | Non-Technical |
| --- | --- | --- |
| For | programming, AI/ML, cloud, DevOps, security | business, marketing, leadership, sales, finance |
| Chapter shape | Introduction · Objectives · Concept · How It Works · Visual · Technical Example · Code · Step-by-Step · Common Mistakes · Best Practices · Exercise · Challenge · Quiz · Summary | Introduction · Objectives · Story · Core Concept · Real-World Example · Case Study · Framework · Practical Application · Common Mistakes · Pro Tips · Reflection · Exercise · Quiz · Summary |
| Reviewer emphasis | technical correctness, code quality | clarity, practical relevance |

Editing a template's JSON changes structure and styling with no code changes.

---

## Course Document model

Each block keeps **content**, **style** and **layout** separate, so the future editor can mutate any of
the three independently:

```json
{
  "id": "block_1f2e3d4c5b6a",
  "type": "heading",
  "content": { "text": "Understanding RAG", "level": 2 },
  "style":   { "font_size": 26, "font_weight": 700, "color": "#0f172a" },
  "layout":  { "x": 64, "y": 100, "width": 666, "height": 46, "z_index": 0 },
  "meta":    { "chapter_id": "chapter_1", "section_key": "concept", "origin": "generated" }
}
```

Block types: `heading, paragraph, image, quote, callout, code, table, quiz, exercise, case_study, story,
tip, warning, summary, divider` plus `learning_objectives, challenge, reflection`.

The system is extensible: add an enum member in `app/schemas/blocks.py`, a content model, one registry
entry, and one branch in `app/render/templates/course.html.j2`. Nothing else changes.

`layout` coordinates are produced by a deterministic layout engine (`app/course/document/layout.py`) that
estimates block heights and flows blocks into A4 pages (794×1123 px @ 96 DPI), splitting long paragraphs,
code and lists across page boundaries and never orphaning a heading.

---

## AI editing

`POST /api/documents/{document_id}/ai-edit`

```json
{ "selected_block_ids": ["block_102"], "instruction": "Make this easier to understand" }
```

The model must return operations, never a document:

```json
{ "operations": [ { "type": "update_block", "block_id": "block_102",
                    "content": { "text": "Simplified explanation..." } } ] }
```

Supported: `update_block`, `delete_block`, `insert_block`, `replace_block`, `replace_image`,
`update_style`. Every patch is Pydantic-validated, then sanitised — operations targeting blocks outside
the selection, unknown block ids, or non-presentation style keys are dropped and reported in
`rejected_operations`. Valid operations still apply if one is bad. After applying, the document is
re-paginated, its `version` is bumped, and it is written back to disk.

Pass `"apply": false` to preview the patch without changing anything.

---

## Storage layout

```
backend/data/courses/{course_id}/
  course.json                 status, input, per-chapter progress
  blueprint.json              planner output + critique
  research/chapter_01.json    one research artifact per chapter
  chapters/chapter_01.json    generated blocks + summary + review
  document.json               the source of truth
  assets/image_001.png        generated images
  exports/course.pdf          export (course.html is written alongside it)
```

Chapters are independent: if chapter 8 fails, regenerate only chapter 8 with
`{"chapter_ids": ["chapter_8"], "force": true}`. Research is cached per chapter and reused unless
`force` is set.

---

## Configuration

See `.env.example`. The ones that matter:

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | *(empty)* | Empty ⇒ offline mock client |
| `RESEARCH_MODE` | `fast` | `fast` = model + `web_search` per chapter; `deep` = Deep Research API (much slower and pricier) |
| `MOCK_OPENAI` | `false` | Force the offline client even with a key |
| `ENABLE_IMAGE_GENERATION` | `true` | Turn off to skip image cost |
| `MAX_CONCURRENCY` | `3` | Concurrent chapter research / image jobs |
| `MAX_REVIEW_REVISIONS` | `1` | Rewrite passes when the reviewer finds blockers |
| `PLANNER_MODEL` etc. | `gpt-5` | Per-agent model overrides |
| `DATABASE_URL` | *(compose-provided)* | asyncpg URL; Compose points it at the `postgres` service, override for local/host Postgres |
| `JWT_SECRET` / `JWT_REFRESH_SECRET` | *(empty)* | Required — auth endpoints fail without them |
| `INITIAL_ADMIN_EMAIL` | *(empty)* | First registration matching this email becomes `role=admin` |
| `FRONTEND_ORIGIN` | `http://localhost:3000` | CORS allow-origin for the backend |

---

## Project structure

```
backend/app/
  api/          courses.py, documents.py, health.py, auth.py, admin.py,
                course_authorization.py, dependencies.py               — HTTP only
  agents/       planner, research, writer, reviewer, editor, prompts
  course/
    templates/  technical_v1.json, non_technical_v1.json, registry.py
    blocks/     normalizer.py   (writer draft → validated blocks)
    document/   builder.py, layout.py, patcher.py
  render/       html_renderer.py + templates/course.html.j2
  schemas/      blocks, course, blueprint, research, draft, review, document, patch, template, auth
  services/     openai_service, mock_ai, research_service, image_service, pdf_service,
                storage_service, course_service, document_service, auth_service
  db/           models.py, service.py, session.py, repositories/       — SQLAlchemy + asyncpg
  core/         config, errors, ids, logging, roles, security
alembic/        versions/ — schema migrations, run with `alembic upgrade head`
```

AI logic, PDF logic and document models are kept in separate layers. `openai_service.AIClient` is the
only place that talks to OpenAI, and it's an interface with two implementations (real + mock), which is
what makes the pipeline testable offline.

---

## Tests

```bash
cd backend && python -m pytest        # or: docker compose run --rm backend pytest
```

116 tests, no API key required: Pydantic schemas, template validation, layout/pagination/splitting,
patch validation and application, course creation, TOC improvement, document assembly, image generation,
HTML rendering (including escaping), PDF export, and two full end-to-end runs — one per template —
that walk create → improve-toc → blueprint → research → generate → review → document → image →
AI edit → PDF and assert on the real PDF bytes.

The Playwright-dependent tests skip automatically if Chromium isn't available.

---

## Generation speed

Chapters are written **concurrently**. Each one receives every other chapter's planned summary from the
blueprint plus explicit "this is reserved for chapter K" boundaries, which removes the dependency that used to
force chapters to be written one after another — historically ~83% of the wall clock. Measured with a
latency-injected mock at 8 chapters: **write+review 6.3× faster, end-to-end 2.4× faster**
(`docs/PERFORMANCE_PLAN.md` has the numbers and the trade-offs).

Also in place:

- **Background runs.** `POST /generate` returns a job id immediately; progress, ETA and timings live on the
  course record (`GET /api/courses/{id}/run`). `{"mode": "sync"}` still blocks. `{"resume": true}` skips
  chapters that already have artifacts.
- **The document is saved after every chapter**, so the editor opens on the finished chapters while the rest
  are still being written — the progress screen offers "Start editing" as soon as one exists.
- **Fewer calls**: research is one call per chapter instead of two, the reviewer receives a text-only
  projection instead of full block JSON, and a review failure rewrites only the flagged blocks.
- **Adaptive concurrency**: per-phase limits that halve on a 429 and recover, so raising them degrades
  instead of failing.
- **Measurement built in**: every call and phase is timed onto `GenerateResponse.timings` and logged as a
  waterfall. `parallel_speedup` > 1 proves calls actually overlapped.

`WRITING_MODE=sequential` restores the original one-chapter-at-a-time behaviour for A/B comparison of
continuity quality.

## Frontend

The core four-screen course flow, plus login/register and an admin section.

| # | Screen | Route | What it does |
| --- | --- | --- | --- |
| 1 | Create Course | `/` | Title, audience, multi-entry Do's/Don'ts, the two templates. Continue asks the backend to draft a starting outline (`POST /improve-toc`). |
| 2 | Customize Table of Contents | `/toc` | Chapters + sections: expand, add, rename (double-click), delete, drag-and-drop reorder. Summary counts and estimates come from the outline being edited. **Improve with AI** shows the backend's suggestions with Apply / Cancel — your TOC is never overwritten silently. **Generate Course** creates the course and starts the pipeline. |
| 3 | Generating Your Course | `/generate/[courseId]` | Progress ring and stage list driven by the backend's real persisted state (see below). |
| 4 | Course Editor | `/editor/[documentId]` | Toolbar · left rail + page thumbnails · canvas · properties panel · AI Assistant. Plus `/preview/[documentId]` for the reader's view. |

Supporting pages: `/login`, `/register`, `/courses` (list, filtered by ownership/review status),
`/history`, `/templates`, and `/admin`, `/admin/users`, `/admin/courses` (admin-role only, backed by the
`/admin/*` API).

### Generation progress is real

The backend POC generates synchronously, so the screen fires `POST /generate` once and polls
`GET /api/courses/{id}` for the progress the backend genuinely persists as it works: `course.status`, the
per-chapter `researched/written/reviewed` flags, and the research files already on disk. Percentages are a
weighted roll-up of those numbers — nothing is simulated on a timer. All of that mapping lives in one
function (`lib/generation/stages.ts`), so a richer progress endpoint later only changes that file.

### The editor

The canvas renders the Course Document JSON returned by the backend — never hardcoded content. Each
block's `content`, `style` and `layout` stay separate, and `layout.x/y/width/height` drives absolute
positioning on an A4 page (794×1123 px @ 96 DPI, identical to the backend layout engine), so the canvas
previews what the PDF will look like.

- **Blocks**: a registry (`components/blocks/registry.tsx`) maps each of the 18 block types to a renderer.
  Adding a type is one entry plus one component.
- **Selection**: click selects (violet frame + handles, properties panel, AI target); double-click edits
  text inline; drag moves; handles resize; `Delete` removes; `Esc` deselects.
- **Undo/redo**: whole-document snapshots (`⌘/Ctrl+Z`, `⌘/Ctrl+Shift+Z`), so **AI changes are undoable too**.
- **AI Assistant**: select a block, pick a suggestion or type an instruction →
  `POST /api/documents/{id}/ai-edit` → the returned **patch** is applied to editor state (all six
  operations: `update_block`, `delete_block`, `insert_block`, `replace_block`, `replace_image`,
  `update_style`), then the backend's persisted copy is adopted so re-pagination and regenerated images
  come through. The whole course is never regenerated for a single edit.
- **Replace Image** in the properties panel routes through the same `ai-edit` endpoint (images are
  generated by the backend; there is no upload endpoint in this POC).
- **Export PDF** calls the backend endpoint and downloads the result. The frontend never renders the PDF.

### API layer

All HTTP lives in `lib/api/` (`client.ts`, `courses.ts`, `documents.ts`, `generation.ts`) with TypeScript
types in `lib/types/` mirroring the Pydantic schemas. No component calls `fetch` directly.

```
frontend/src/
  app/            route entries for the four screens + preview
  components/
    course/       CreateCourseForm, TemplateSelector, RuleList, TocEditor, ChapterItem, SectionItem,
                  TocSummary, ImproveTocDialog
    generation/   GenerationProgress, ProgressRing, StageList
    editor/       CourseEditor, EditorToolbar, LeftRail, PageSidebar, BlocksPanel, UploadsPanel,
                  CanvasToolbar, Canvas, SelectionBox, PropertiesPanel, AiAssistant, CoursePreview
    blocks/       one renderer per block type + registry + EditableText
    ui/           Button, IconButton, Modal, StepHeader
  lib/
    api/          typed backend client
    editor/       store (state + history), patch application, block helpers, style resolution
    generation/   backend status -> stage list
    state/        course draft shared by screens 1 and 2
```

## Deliberate POC limitations

- Generation runs **in-process** (an asyncio task per course, state on disk). That covers timeouts, resume
  and progress without new infrastructure, but not parallelism across machines or cancellation — that is what
  a real queue is for, and it is deferred until there are concurrent users.
- **No rate limiting.** Auth and generation endpoints have no throttling — fine for local/dev use, not for
  a public deployment.
- Desktop-first, as specified: screens 1-2 adapt down, the editor assumes a laptop-sized window.
- Underline is present in the toolbar but disabled: the backend's PDF renderer has no underline support, and
  a control that silently vanishes on export would be worse than a disabled one.
- Layout heights are *estimated*, not measured by a text-shaping engine. Estimates deliberately err high;
  boxes use `min-height` so content grows rather than clipping.
- Concurrent writes to the same course are not locked.

## Next step

PostgreSQL, auth, admin and manual-edit persistence are in. Next: Redis, background workers (move
generation off the in-process asyncio task) and object storage for generated assets.
