# Build Status — AI Course Creation Platform

Snapshot of what exists in the codebase right now (branch `feature/auth-database-integration`).
Two layers: the original AI course-generation pipeline, and a newer auth + admin + PostgreSQL layer
built on top of it.

---

## 1. High-level flow

```
Create Course → Customize TOC → Generate → Course Editor → Export PDF
     (login required to reach any of these — AuthGate on every route)
```

Backend pipeline (unchanged from the original POC):

```
Course Input → Planner → Blueprint → Deep Research → Research Artifacts
            → Chapter Writer → Chapter Reviewer → Course Document JSON
            → Image Generation → PDF Renderer → Final PDF
```

The **Course Document JSON** is the source of truth; the PDF is only an export.

---

## 2. Stack & how it runs

- **Backend**: FastAPI (Python 3.12), async SQLAlchemy + `asyncpg`, Alembic migrations, Playwright for PDF rendering.
- **Frontend**: Next.js 15, four core screens + login + admin panel.
- **Database**: PostgreSQL 16 (Docker container locally, or an external instance via `DATABASE_URL`).
- **Docker**: `docker-compose.yml` runs `postgres` + `backend` (port 8000) + `frontend` (port 3000).
  `docker compose up --build`, then apply migrations once: `docker compose exec backend alembic upgrade head`.
- **Offline mode**: empty `OPENAI_API_KEY` (or `MOCK_OPENAI=true`) swaps in a built-in mock AI client so the
  whole pipeline runs with no network calls — used by the test suite.

---

## 3. Database layer (`backend/app/db/`)

Two Alembic migrations so far:

| Migration | Adds |
| --- | --- |
| `20260904_0001_initial_phase1` | `courses`, `documents`, `blueprints`, `generation_runs` |
| `20260904_0002_auth_tables` | `users`, `auth_sessions` |

**Models** (`db/models.py`):

- **User** — `id` (UUID), `email` (unique), `password_hash`, `role` (`user`/`admin`), `is_active`,
  `is_verified`, timestamps.
- **AuthSession** — `id`, `user_id` (FK, cascade delete), `refresh_token_hash` (unique), `expires_at`,
  `revoked_at` (nullable), `created_at`. One row per issued refresh token; rotated on every refresh.
- **Course** — `course_id`, `document_id`, `title`, `status`, `template_id`, `input_json` (JSONB),
  `metadata_json` (JSONB); related 1:1 to `Document`, 1:1 to `Blueprint`, 1:many to `GenerationRun`.
- **Document** — `document_id`, `course_pk` (FK, unique), `version`, `document_json` (JSONB) — the actual
  Course Document.
- **Blueprint** — `course_pk` (FK, unique), `blueprint_json` (JSONB) — planner output + critique.
- **GenerationRun** — `course_pk` (FK), `job_id` (unique), `state`, `payload_json` (JSONB), `error` — tracks
  a background `/generate` job's progress.

Repositories (`db/repositories/`) wrap each table with typed CRUD + filtered list/count: `users.py`,
`courses.py`, `documents.py`, `blueprints.py`.

Engine: a single cached `AsyncEngine` (`db/engine.py`, `get_engine()`), session factory in `db/session.py`.

---

## 4. Authentication (`backend/app/api/auth.py`, `services/auth_service.py`)

**Endpoints** (prefix `/auth`):

| Method | Path | Does |
| --- | --- | --- |
| POST | `/auth/register` | Create user, auto-`admin` if email matches `INITIAL_ADMIN_EMAIL`, sets cookies |
| POST | `/auth/login` | Verify credentials, sets cookies |
| POST | `/auth/refresh` | Rotate access + refresh token pair |
| POST | `/auth/logout` | Revoke the current session, clear cookies |
| GET | `/auth/me` | Current user (requires auth) |

**Mechanics**:

- Passwords hashed with `pwdlib` (`PasswordHash.recommended()`).
- JWTs (`HS256`) — separate secrets for access vs. refresh tokens (`JWT_SECRET`, `JWT_REFRESH_SECRET`),
  configurable TTLs (`ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`).
- Tokens are delivered as **HttpOnly cookies** (`access_token`, `refresh_token`), not returned in the JSON body.
- Refresh tokens are never stored raw — only their hash, per session row (`auth_sessions`). Refreshing
  finds the matching active session, revokes it, and issues a brand-new pair (rotation on every use).
- Route protection: `get_current_user` (cookie or `Authorization: Bearer` header) and `get_current_admin`
  (adds a `role == "admin"` check) — both in `api/dependencies.py`.

---

## 5. Admin panel (`backend/app/api/admin.py`)

All under `/admin`, all requiring `get_current_admin`:

| Method | Path | Does |
| --- | --- | --- |
| GET | `/admin/health` | Liveness check |
| GET | `/admin/dashboard` | Totals: users, courses, system status |
| GET | `/admin/users` | Paginated/filterable user list (search, role, active, verified) |
| GET | `/admin/users/{id}` | Single user detail |
| PATCH | `/admin/users/{id}/role` | Change a user's role (can't change your own) |
| PATCH | `/admin/users/{id}/status` | Activate/deactivate a user (can't deactivate yourself) |
| GET | `/admin/courses` | Paginated/filterable course list (search, status, owner) |
| GET | `/admin/courses/{id}` | Single course detail |

---

## 6. Frontend

Four original product screens, plus new auth/admin screens:

| Route | Purpose |
| --- | --- |
| `/` | Create Course (title, audience, do's/don'ts, template) |
| `/toc` | Customize Table of Contents, AI TOC suggestions |
| `/generate/[courseId]` | Live generation progress |
| `/editor/[documentId]` | Canvas-based Course Editor + AI block editing |
| `/preview/[documentId]` | Reader view |
| `/login` | Login / register form |
| `/admin` | Admin dashboard (totals) |
| `/admin/users` | User management (role/status changes) |
| `/admin/courses` | Course oversight |

**Auth wiring**:

- `AuthProvider` / `useAuth` (`lib/auth/auth-provider.tsx`) — holds the current user, auto-calls
  `/auth/me` on load, retries once via `/auth/refresh` on a 401, exposes `login`/`register`/`logout`.
- `AuthGate` (`components/auth/AuthGate.tsx`) — wraps every route: redirects signed-out users to
  `/login`, redirects signed-in users away from public-only pages, shows a logout bar on normal pages.
  Admin-only enforcement is server-side (`get_current_admin`); the frontend doesn't independently gate
  by role beyond styling.
- `lib/api/auth.ts`, `lib/api/admin.ts` — typed API clients for the endpoints above.

---

## 7. What's NOT built yet

- No document-save endpoint — manual editor edits (drag/resize/typing) still live only in the browser
  session; only AI-driven edits (`/ai-edit`) persist server-side.
- No password reset / email verification flow (`is_verified` field exists but nothing sets it yet).
- No per-user course ownership enforcement visible in the course-generation endpoints themselves (only
  the admin course list supports filtering by owner).
- No rate limiting; CORS is open.
- No object storage / queue — generation still runs in-process (asyncio task per course), state on disk
  plus the new `generation_runs` table.
- Alembic migrations are **not** run automatically on container start — must be applied manually
  (`docker compose exec backend alembic upgrade head`).
