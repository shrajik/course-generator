# Knowledge base: PostgreSQL cutover runbook (draft)

Applies to the move of the admin knowledge base (the global library) from files
under `data/sources/global/` to PostgreSQL + pgvector. Status: **draft, written in
Phase 0**; every step marked TODO is filled in as its phase lands.

## Rollback policy

* **Before the first authoritative PostgreSQL write:** clean rollback. Set
  `KB_BACKEND=files` (TODO: setting introduced in Phase 2b) and restart. The file data
  is never modified by the migration or the import.
* **After the first authoritative write** (the first admin change or accepted upload
  while PostgreSQL is the write path): **forward recovery only**. No write-back to
  files. See "Forward recovery".
* Purge (permanent deletion of an item's content) stays disabled by feature flag until
  the rollback window has closed, because legacy documents' original files are used in
  place and purging one would destroy the rollback path.

## Prerequisites (Phase 0)

1. **pgvector pinned and running.** `docker-compose.yml` pins `pgvector/pgvector:0.8.0-pg16`.
   Verify the tag exists before relying on it: `docker pull pgvector/pgvector:0.8.0-pg16`.
   Recreate the container to apply it (the named volume keeps the data):
   `docker compose up -d --force-recreate postgres`.
   Then confirm the backend log shows `pgvector 0.8.x (iterative scan: yes)` at startup.
2. **Scheduled PostgreSQL backups exist** (TODO: confirm with the platform owner; this
   is a hard gate for cutover).
3. **Baseline recorded:** run `python -m app.scripts.kb_benchmark --passages <corpus size>`
   and keep the output. The golden retrieval results live in `backend/tests/golden_knowledge.py`.
4. **Corpus size known** (TODO: number of documents and passages in production).

## Cutover gate: every item must be checked before the flag flips

| # | Check | How |
|---|---|---|
| 1 | Verified database backup | `pg_dump -Fc` (below), restored into a scratch database, import parity report run against the restored copy |
| 2 | Verified filesystem snapshot | `python -m app.scripts.kb_snapshot create` then `verify` (below) |
| 3 | Migration validated on a copy of production data | `alembic upgrade head`, then rehearse `alembic downgrade -1` (TODO: revision 0012, Phase 1) |
| 4 | Import parity report clean | `python -m app.scripts.kb_import --dry-run`, then the real run, then `--verify --report parity.json` (exit 0 = numbers, lifecycle, passage order and text, embed hashes, vectors bit-identical, every `[G..]` unchanged, number sequence above every number issued) |
| 5 | Shadow mode within tolerance | Set `KB_SHADOW_COMPARE=true` (needs the database and an import first); generate real courses; every search is compared in the background. Read the counts (`match` / `reordered` / `partial` / `different` / `error`) from the log lines `KB shadow ...` or `knowledge_shadow.get_shadow_stats()`. Agreed tolerance: TODO (suggest: no `different`, no `error`, `partial`/`reordered` explained) |
| 6 | This runbook reviewed and the recovery steps rehearsed once | |

## Backup procedures

**Database** (run from the repo root; adjust names from `docker-compose.yml`/`.env`):

```sh
docker exec course_creator_postgres pg_dump -U course_app -Fc course_generator > kb-pre-cutover.dump
# prove it restores:
docker exec -i course_creator_postgres createdb -U course_app kb_restore_check
docker exec -i course_creator_postgres pg_restore -U course_app -d kb_restore_check < kb-pre-cutover.dump
docker exec course_creator_postgres psql -U course_app -d kb_restore_check -c "\dt"
docker exec course_creator_postgres dropdb -U course_app kb_restore_check
```

**Filesystem** (the data directory holds the library files, originals, courses, templates):

```sh
cd backend
python -m app.scripts.kb_snapshot create --out ../backups/data-<date>.tar.gz
python -m app.scripts.kb_snapshot verify ../backups/data-<date>.tar.gz
python -m app.scripts.kb_snapshot verify ../backups/data-<date>.tar.gz --against ./data   # unchanged since?
```

Keep both artifacts until the rollback window closes (TODO: decide the window length).

## Rollback (only before the first authoritative write)

1. Set the backend to the file backend and restart (TODO: `KB_BACKEND=files`).
2. Verify `GET /api/knowledge` lists the same documents and numbers as before.
3. Run `kb_snapshot verify <archive> --against ./data`: it must report no changes to the
   library files.
4. Optionally drop the new tables with `alembic downgrade` to the pre-0012 revision.

## Forward recovery (after the first authoritative write)

* Prefer a fix-forward release.
* If data is damaged: restore the latest `pg_dump` into a **side** database, copy the
  affected `kb_*` tables selectively into the live one, and re-run the reconciliation
  check (TODO: Phase 3: `is_live` invariant; chunk/version integrity).
* Original uploaded files are still on disk; a snapshot plus the database backup covers
  all knowledge-base state.
* After any restore, boot once before accepting writes: the startup guard raises the
  item-number sequence above the highest number known (TODO: Phase 1), so a number is
  never issued twice.
