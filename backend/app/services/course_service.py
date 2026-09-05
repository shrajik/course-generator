"""Pipeline orchestration.

    Course Input -> Planner -> Blueprint -> Research -> Writer -> Reviewer
                 -> Course Document -> Images -> (PDF export)

Performance shape of this module:

* **Chapters are written concurrently** (`WRITING_MODE=parallel`, the default).
  Each chapter is given the *planned* summaries of its neighbours from the
  blueprint plus explicit topic boundaries, which removes the dependency that
  previously forced chapters to be written one after another - historically ~80%
  of the wall clock. `WRITING_MODE=sequential` restores the old behaviour for
  A/B comparison of continuity quality.
* **The document is saved after every chapter**, so the editor can be opened on
  chapter 1 while later chapters are still being written.
* **Runs are backgrounded and resumable**: the request returns a job id, progress
  is persisted on the course record, and a re-run with `resume=true` skips
  chapters that already have artifacts.
"""

from __future__ import annotations

import asyncio
import time

from app.agents.planner import PlannerAgent
from app.agents.prompts import ContinuityContext
from app.agents.reviewer import ReviewerAgent
from app.agents.writer import WriterAgent, to_generated_chapter
from app.core.concurrency import get_limiter
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.ids import course_id as new_course_id
from app.core.ids import document_id_for_course, new_id, utc_now_iso
from app.core.logging import get_logger
from app.core.metrics import current_metrics, phase as metrics_phase, run_metrics
from app.course.document.builder import build_document, reflow_document
from app.course.templates.registry import load_template
from app.schemas.blueprint import BlueprintChapter, CourseBlueprint
from app.schemas.course import (
    ChapterProgress,
    CourseInput,
    CourseRecord,
    GenerateRequest,
    GenerateResponse,
    ImproveTocRequest,
    ImproveTocResponse,
    RunInfo,
)
from app.schemas.document import CourseDocument
from app.schemas.draft import GeneratedChapter
from app.schemas.review import ChapterReview
from app.schemas.template import CourseTemplate
from app.db.service import get_database_service
from app.services.image_service import ImageService
from app.services.openai_service import AIClient, get_ai_client
from app.services.research_service import ResearchService
from app.services.storage_service import StorageService, get_storage

log = get_logger(__name__)


class CourseService:
    def __init__(
        self,
        ai: AIClient | None = None,
        storage: StorageService | None = None,
        settings: Settings | None = None,
        use_db: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.storage = storage or get_storage()
        self.ai = ai or get_ai_client()
        # PostgreSQL is the primary store for course/blueprint/document records;
        # tests without a live database flip this off (see conftest.py).
        self.use_db = self.settings.use_database if use_db is None else use_db
        self.planner = PlannerAgent(self.ai, self.settings)
        self.writer = WriterAgent(self.ai, self.settings)
        self.reviewer = ReviewerAgent(self.ai, self.settings)
        self.research = ResearchService(self.ai, self.storage, self.settings)
        self.images = ImageService(self.ai, self.storage, self.settings)
        # Serialises the incremental document rebuilds triggered by parallel
        # chapter completions.
        self._document_locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, asyncio.Task] = {}

    # --- persistence: course/blueprint/document live in Postgres when enabled,
    # research/chapters/assets/exports always stay on the filesystem (below). Each
    # call opens its own short-lived session - this service is a long-lived
    # singleton and generation runs as a detached background task, so it cannot
    # hold one request-scoped session across its whole lifetime. ------------
    async def _save_course(self, record: CourseRecord) -> CourseRecord:
        if self.use_db:
            async with get_database_service() as db:
                return await db.save_course_record(record)
        return self.storage.save_course(record)

    async def _load_course(self, course_id: str) -> CourseRecord:
        if self.use_db:
            async with get_database_service() as db:
                return await db.load_course_record(course_id)
        return self.storage.load_course(course_id)

    async def _list_courses(self, limit: int = 100, offset: int = 0) -> list[CourseRecord]:
        if self.use_db:
            async with get_database_service() as db:
                return await db.list_course_records(limit, offset)
        return [self.storage.load_course(cid) for cid in self.storage.list_course_ids()]

    async def _save_blueprint(self, course_id: str, blueprint: CourseBlueprint) -> None:
        if self.use_db:
            async with get_database_service() as db:
                await db.save_blueprint(course_id, blueprint)
            return
        self.storage.save_blueprint(course_id, blueprint)

    async def _load_blueprint(self, course_id: str) -> CourseBlueprint:
        if self.use_db:
            async with get_database_service() as db:
                return await db.load_blueprint(course_id)
        return self.storage.load_blueprint(course_id)

    async def _has_blueprint(self, course_id: str) -> bool:
        if self.use_db:
            async with get_database_service() as db:
                return await db.has_blueprint(course_id)
        return self.storage.has_blueprint(course_id)

    async def _save_document(self, document: CourseDocument) -> None:
        if self.use_db:
            async with get_database_service() as db:
                await db.save_document(document)
            return
        self.storage.save_document(document)

    async def _load_document(self, course_id: str) -> CourseDocument:
        if self.use_db:
            async with get_database_service() as db:
                return await db.load_document_for_course(course_id)
        return self.storage.load_document(course_id)

    async def _has_document(self, course_id: str) -> bool:
        if self.use_db:
            async with get_database_service() as db:
                return await db.has_document(course_id)
        return self.storage.has_document(course_id)

    # --- public reads, used by the API routes -------------------------------
    async def list_courses(self, limit: int = 100, offset: int = 0) -> list[CourseRecord]:
        return await self._list_courses(limit, offset)

    async def get_course_record(self, course_id: str) -> CourseRecord:
        return await self._load_course(course_id)

    async def get_blueprint(self, course_id: str) -> CourseBlueprint:
        return await self._load_blueprint(course_id)

    async def has_blueprint(self, course_id: str) -> bool:
        return await self._has_blueprint(course_id)

    async def has_document(self, course_id: str) -> bool:
        return await self._has_document(course_id)

    async def load_document(self, course_id: str) -> CourseDocument:
        return await self._load_document(course_id)

    # --- phase 0: create ---------------------------------------------------
    async def create_course(
        self, course_input: CourseInput, *, run_planner: bool = True
    ) -> CourseRecord:
        course_id = new_course_id()
        record = CourseRecord(
            course_id=course_id,
            document_id=document_id_for_course(course_id),
            status="created",
            input=course_input,
            template_id=course_input.template_id,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        record = await self._save_course(record)
        log.info("Created course %s ('%s')", course_id, course_input.course_title)

        if run_planner:
            try:
                # Planning happens in the create request, so it is measured here
                # rather than in the generation waterfall.
                with run_metrics() as metrics:
                    await self.plan_course(record)
                metrics.log_waterfall(f"planning {course_id}")
            except Exception as exc:  # noqa: BLE001 - keep the course, report the failure
                log.exception("Planning failed for %s", course_id)
                record.last_error = f"planner: {exc}"
                record.warnings.append("Planning failed; blueprint not created.")
                record = await self._save_course(record)
                return record
            record = await self._load_course(course_id)
        return record

    # --- phase 1: plan -----------------------------------------------------
    async def plan_course(self, record: CourseRecord) -> CourseBlueprint:
        with metrics_phase("planner"):
            blueprint = await self.planner.plan(record.input)
            # Planned per-chapter summaries are the contract that lets chapters be
            # written in parallel, so make sure none are missing.
            await self.planner.ensure_planned_summaries(blueprint, record.input)
        await self._save_blueprint(record.course_id, blueprint)
        record.has_blueprint = True
        record.status = "planned"
        record.chapters = [
            ChapterProgress(chapter_id=chapter.id, title=chapter.title)
            for chapter in blueprint.chapters
        ]
        await self._save_course(record)
        return blueprint

    async def improve_toc(self, request: ImproveTocRequest) -> ImproveTocResponse:
        return await self.planner.improve_toc(request)

    # --- generation entrypoint --------------------------------------------
    async def start_generation(
        self, course_id: str, request: GenerateRequest
    ) -> GenerateResponse:
        """Background by default: return a job id immediately and let the caller poll.

        A 40-minute blocking HTTP request is one proxy timeout away from throwing
        away the whole run; the frontend already polls the course record.
        """
        record = await self._load_course(course_id)

        if request.mode == "sync":
            return await self.generate(course_id, request)

        existing = self._jobs.get(course_id)
        if existing is not None and not existing.done():
            run = record.run or RunInfo()
            log.info("Generation already running for %s (job %s)", course_id, run.job_id)
            return GenerateResponse(
                course_id=course_id,
                document_id=record.document_id,
                status=record.status,
                mode="background",
                job_id=run.job_id,
                accepted=True,
            )

        job_id = new_id("job")
        record.run = RunInfo(
            job_id=job_id,
            state="queued",
            mode="background",
            started_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            chapters_total=len(request.chapter_ids or record.chapters) or len(record.chapters),
            writing_mode=self.settings.writing_mode,
        )
        record.last_error = None
        await self._save_course(record)

        task = asyncio.create_task(self._run_job(course_id, request, job_id))
        self._jobs[course_id] = task
        task.add_done_callback(lambda _: self._jobs.pop(course_id, None))

        return GenerateResponse(
            course_id=course_id,
            document_id=record.document_id,
            status=record.status,
            mode="background",
            job_id=job_id,
            accepted=True,
        )

    async def _run_job(self, course_id: str, request: GenerateRequest, job_id: str) -> None:
        try:
            await self.generate(course_id, request, job_id=job_id)
        except asyncio.CancelledError:  # pragma: no cover
            await self._patch_run(course_id, state="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - a background run must not vanish silently
            log.exception("Background generation failed for %s", course_id)
            await self._patch_run(course_id, state="failed", error=str(exc)[:500])
            try:
                record = await self._load_course(course_id)
                record.status = "failed"
                record.last_error = str(exc)[:500]
                await self._save_course(record)
            except Exception:  # pragma: no cover
                pass

    async def job_state(self, course_id: str) -> RunInfo | None:
        try:
            record = await self._load_course(course_id)
            return record.run
        except NotFoundError:
            return None

    # --- phases 2+3: generate ---------------------------------------------
    async def generate(
        self, course_id: str, request: GenerateRequest, *, job_id: str = ""
    ) -> GenerateResponse:
        started = time.perf_counter()

        with run_metrics() as metrics:
            record = await self._load_course(course_id)
            if not await self._has_blueprint(course_id):
                await self.plan_course(record)
                record = await self._load_course(course_id)
            blueprint = await self._load_blueprint(course_id)
            template = load_template(blueprint.template_id or record.template_id)

            targets = self._select_chapters(blueprint, request)
            if request.resume:
                targets = [
                    chapter
                    for chapter in targets
                    if not self.storage.has_chapter(course_id, chapter.id, chapter.order)
                ]
                log.info("Resuming %s: %s chapters left", course_id, len(targets))
            if not targets:
                if request.resume:
                    return await self._finalise(
                        record=record,
                        blueprint=blueprint,
                        template=template,
                        request=request,
                        generated=[],
                        failed=[],
                        warnings=["Nothing left to generate."],
                        started=started,
                        job_id=job_id,
                        metrics_summary=metrics.summary(),
                    )
                raise ValidationFailedError("No matching chapters to generate")

            record.run = RunInfo(
                job_id=job_id or (record.run.job_id if record.run else ""),
                state="running",
                mode=request.mode,
                started_at=record.run.started_at if record.run else utc_now_iso(),
                updated_at=utc_now_iso(),
                chapters_total=len(targets),
                chapters_done=0,
                writing_mode=self.settings.writing_mode,
            )
            await self._save_course(record)

            warnings: list[str] = []

            # --- research (concurrent, cached) ------------------------------
            research_map = {}
            if not request.skip_research:
                record.status = "researching"
                await self._save_course(record)
                research_map = await self.research.research_chapters(
                    course_id=course_id,
                    blueprint=blueprint,
                    chapters=targets,
                    course_input=record.input,
                    force=request.force,
                    deep_chapter_ids=set(request.deep_research_chapter_ids or []),
                )
                for chapter in targets:
                    if chapter.id not in research_map:
                        warnings.append(f"{chapter.id}: research unavailable, wrote without it")

            # --- write + review --------------------------------------------
            record.status = "writing"
            await self._save_course(record)

            written = self._existing_summaries(course_id, targets)
            parallel = self.settings.writing_mode == "parallel"

            if parallel:
                generated, failed = await self._write_parallel(
                    record=record,
                    blueprint=blueprint,
                    template=template,
                    targets=targets,
                    research_map=research_map,
                    written=written,
                    request=request,
                )
            else:
                generated, failed = await self._write_sequential(
                    record=record,
                    blueprint=blueprint,
                    template=template,
                    targets=targets,
                    research_map=research_map,
                    written=written,
                    request=request,
                )

            # --- cross-chapter continuity ---------------------------------
            if parallel and self.settings.continuity_pass and len(generated) > 1:
                warnings.extend(await self._continuity_warnings(course_id, blueprint))

            record = await self._load_course(course_id)
            return await self._finalise(
                record=record,
                blueprint=blueprint,
                template=template,
                request=request,
                generated=generated,
                failed=failed,
                warnings=warnings,
                started=started,
                job_id=job_id,
                metrics_summary=None,
                metrics_obj=metrics,
            )

    # --- writing strategies -----------------------------------------------
    async def _write_parallel(
        self,
        *,
        record: CourseRecord,
        blueprint: CourseBlueprint,
        template: CourseTemplate,
        targets: list[BlueprintChapter],
        research_map: dict,
        written: dict[str, tuple[int, str, str]],
        request: GenerateRequest,
    ) -> tuple[list[str], list[str]]:
        """Every chapter at once, bounded by the writer limiter."""
        limiter = get_limiter("writer", self.settings.concurrency_for("writer"))
        generated: list[str] = []
        failed: list[str] = []
        done = 0
        total = len(targets)

        async def worker(chapter: BlueprintChapter) -> None:
            nonlocal done
            async with limiter.slot():
                continuity = self._continuity_for(chapter, blueprint, written)
                try:
                    artifact = await self._generate_chapter(
                        record=record,
                        blueprint=blueprint,
                        chapter=chapter,
                        template=template,
                        research=research_map.get(chapter.id),
                        continuity=continuity,
                    )
                except Exception as exc:  # noqa: BLE001 - one chapter must not sink the run
                    log.exception("Chapter %s failed", chapter.id)
                    failed.append(chapter.id)
                    await self._mark_progress(record, chapter.id, error=str(exc)[:500])
                    return
                generated.append(chapter.id)
                written[chapter.id] = (chapter.order, chapter.title, artifact.summary)
                done += 1
                # Make the chapter readable in the editor immediately.
                if request.build_document:
                    await self._rebuild_document(record.course_id)
                await self._patch_run(
                    record.course_id,
                    chapters_done=done,
                    chapters_total=total,
                    eta_seconds=self._estimate_eta(done, total),
                )

        with metrics_phase("write+review"):
            await asyncio.gather(*(worker(chapter) for chapter in targets))

        order = {chapter.id: chapter.order for chapter in targets}
        generated.sort(key=lambda cid: order.get(cid, 0))
        failed.sort(key=lambda cid: order.get(cid, 0))
        return generated, failed

    async def _write_sequential(
        self,
        *,
        record: CourseRecord,
        blueprint: CourseBlueprint,
        template: CourseTemplate,
        targets: list[BlueprintChapter],
        research_map: dict,
        written: dict[str, tuple[int, str, str]],
        request: GenerateRequest,
    ) -> tuple[list[str], list[str]]:
        """Original behaviour: each chapter sees the real text of the previous ones."""
        generated: list[str] = []
        failed: list[str] = []
        total = len(targets)

        with metrics_phase("write+review"):
            for index, chapter in enumerate(targets, start=1):
                continuity = self._continuity_for(chapter, blueprint, written)
                try:
                    artifact = await self._generate_chapter(
                        record=record,
                        blueprint=blueprint,
                        chapter=chapter,
                        template=template,
                        research=research_map.get(chapter.id),
                        continuity=continuity,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception("Chapter %s failed", chapter.id)
                    failed.append(chapter.id)
                    await self._mark_progress(record, chapter.id, error=str(exc)[:500])
                    continue
                generated.append(chapter.id)
                written[chapter.id] = (chapter.order, chapter.title, artifact.summary)
                if request.build_document:
                    await self._rebuild_document(record.course_id)
                await self._patch_run(
                    record.course_id,
                    chapters_done=index,
                    chapters_total=total,
                    eta_seconds=self._estimate_eta(index, total),
                )
        return generated, failed

    def _continuity_for(
        self,
        chapter: BlueprintChapter,
        blueprint: CourseBlueprint,
        written: dict[str, tuple[int, str, str]],
    ) -> ContinuityContext:
        """Neighbour summaries: the real one when it exists, the planned one otherwise."""
        previous: list[tuple[str, str]] = []
        upcoming: list[tuple[str, str]] = []
        used_planned = False

        for other in sorted(blueprint.chapters, key=lambda c: c.order):
            if other.id == chapter.id:
                continue
            actual = written.get(other.id)
            if actual is not None:
                summary = actual[2]
            else:
                summary = other.summary or other.objective
                used_planned = True
            if not summary:
                continue
            entry = (other.title, summary)
            if other.order < chapter.order:
                previous.append(entry)
            else:
                upcoming.append(entry)

        return ContinuityContext(previous=previous, upcoming=upcoming, planned=used_planned)

    async def _generate_chapter(
        self,
        *,
        record: CourseRecord,
        blueprint: CourseBlueprint,
        chapter: BlueprintChapter,
        template: CourseTemplate,
        research,
        continuity: ContinuityContext,
    ) -> GeneratedChapter:
        blocks, summary = await self.writer.write_chapter(
            blueprint=blueprint,
            chapter=chapter,
            template=template,
            course_input=record.input,
            research=research,
            continuity=continuity,
        )
        await self._mark_progress(record, chapter.id, researched=research is not None, written=True)

        review: ChapterReview | None = None
        revisions = 0
        try:
            review = await self.reviewer.review_chapter(
                blueprint=blueprint,
                chapter=chapter,
                template=template,
                course_input=record.input,
                blocks=blocks,
                continuity=continuity,
            )
            while review.needs_revision() and revisions < self.settings.max_review_revisions:
                revisions += 1
                log.info("Revising %s (pass %s)", chapter.id, revisions)
                blocks, summary = await self.writer.revise_chapter(
                    blueprint=blueprint,
                    chapter=chapter,
                    template=template,
                    course_input=record.input,
                    research=research,
                    continuity=continuity,
                    review=review,
                    blocks=blocks,
                    summary=summary,
                )
                review = await self.reviewer.review_chapter(
                    blueprint=blueprint,
                    chapter=chapter,
                    template=template,
                    course_input=record.input,
                    blocks=blocks,
                    continuity=continuity,
                )
        except Exception as exc:  # noqa: BLE001 - review is advisory, not fatal
            log.warning("Review failed for %s: %s", chapter.id, exc)

        artifact = to_generated_chapter(
            chapter=chapter,
            blocks=blocks,
            summary=summary,
            review=review,
            revisions=revisions,
            model="mock" if self.ai.is_mock else self.settings.writer_model,
        )
        self.storage.save_chapter(record.course_id, artifact)
        await self._mark_progress(
            record,
            chapter.id,
            reviewed=review is not None,
            review_score=review.scores.overall() if review else None,
        )
        return artifact

    async def _continuity_warnings(
        self, course_id: str, blueprint: CourseBlueprint
    ) -> list[str]:
        chapters = self.storage.load_all_chapters(course_id)
        summaries = [
            (chapter.chapter_id, chapter.title, chapter.summary)
            for chapter in chapters
            if chapter.summary
        ]
        if len(summaries) < 2:
            return []
        try:
            with metrics_phase("continuity"):
                report = await self.reviewer.review_continuity(
                    blueprint=blueprint, summaries=summaries
                )
        except Exception as exc:  # noqa: BLE001 - advisory only
            log.warning("Continuity pass failed for %s: %s", course_id, exc)
            return []
        return report.warnings()

    # --- finalisation ------------------------------------------------------
    async def _finalise(
        self,
        *,
        record: CourseRecord,
        blueprint: CourseBlueprint,
        template: CourseTemplate,
        request: GenerateRequest,
        generated: list[str],
        failed: list[str],
        warnings: list[str],
        started: float,
        job_id: str,
        metrics_summary: dict | None = None,
        metrics_obj=None,
    ) -> GenerateResponse:
        pages = 0
        images = 0

        if request.build_document:
            record.status = "assembling"
            await self._save_course(record)
            document = await self._rebuild_document(record.course_id)
            if document is None:
                document = await self.build_course_document(record.course_id)

            generate_images = (
                self.settings.enable_image_generation
                if request.generate_images is None
                else request.generate_images
            )
            if generate_images:
                # Images run after the document is already readable, so they are
                # never what the user is waiting on to start editing.
                record.status = "illustrating"
                await self._save_course(record)
                images = await self.images.generate_missing(
                    document=document, template=template, force=request.force
                )
                if images:
                    reflow_document(document, template)
                await self._save_document(document)
            pages = len(document.pages)
            record.has_document = True

        record.status = "failed" if failed and not generated else "ready"
        record.last_error = f"chapters failed: {', '.join(failed)}" if failed else None
        record.warnings = warnings

        summary = metrics_summary
        if summary is None and metrics_obj is not None:
            summary = metrics_obj.summary()
            metrics_obj.log_waterfall(f"course {record.course_id}")

        record.run = RunInfo(
            job_id=job_id or (record.run.job_id if record.run else ""),
            state="failed" if record.status == "failed" else "done",
            mode=request.mode,
            started_at=record.run.started_at if record.run else utc_now_iso(),
            updated_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            chapters_total=len(generated) + len(failed),
            chapters_done=len(generated),
            writing_mode=self.settings.writing_mode,
            eta_seconds=0.0,
            error=record.last_error,
            timings=summary or {},
        )
        await self._save_course(record)

        return GenerateResponse(
            course_id=record.course_id,
            document_id=record.document_id,
            status=record.status,
            chapters_generated=generated,
            chapters_failed=failed,
            images_generated=images,
            pages=pages,
            warnings=warnings,
            duration_seconds=round(time.perf_counter() - started, 2),
            mode=request.mode,
            job_id=job_id,
            accepted=False,
            timings=summary or {},
        )

    # --- document ---------------------------------------------------------
    def _document_lock(self, course_id: str) -> asyncio.Lock:
        lock = self._document_locks.get(course_id)
        if lock is None:
            lock = asyncio.Lock()
            self._document_locks[course_id] = lock
        return lock

    async def _rebuild_document(self, course_id: str) -> CourseDocument | None:
        """Rebuild and save the document from whatever chapters exist so far.

        Called after every chapter so the editor has something to open early.
        Failures are logged, never fatal - the final assembly will try again.
        """
        async with self._document_lock(course_id):
            try:
                return await self.build_course_document(course_id)
            except NotFoundError:
                return None
            except Exception as exc:  # noqa: BLE001
                log.warning("Incremental document rebuild failed for %s: %s", course_id, exc)
                return None

    async def build_course_document(self, course_id: str) -> CourseDocument:
        blueprint = await self._load_blueprint(course_id)
        template = load_template(blueprint.template_id)
        chapters = self.storage.load_all_chapters(course_id)
        if not chapters:
            raise NotFoundError(
                f"Course '{course_id}' has no generated chapters yet - run generation first"
            )
        existing = (
            await self._load_document(course_id) if await self._has_document(course_id) else None
        )
        document = build_document(
            course_id=course_id,
            blueprint=blueprint,
            template=template,
            chapters=chapters,
            existing=existing,
        )
        # Carry generated image paths across rebuilds so we don't pay twice.
        if existing is not None:
            self._carry_over_assets(existing, document)
        await self._save_document(document)
        return document

    @staticmethod
    def _carry_over_assets(old: CourseDocument, new: CourseDocument) -> None:
        from app.schemas.blocks import BlockType, merge_content

        by_prompt = {}
        for _, block in old.iter_blocks():
            if block.type is BlockType.IMAGE and block.content.get("path"):
                key = (block.content.get("purpose", ""), block.content.get("caption", ""))
                by_prompt[key] = block.content
        for _, block in new.iter_blocks():
            if block.type is not BlockType.IMAGE or block.content.get("path"):
                continue
            key = (block.content.get("purpose", ""), block.content.get("caption", ""))
            previous = by_prompt.get(key)
            if previous:
                block.content = merge_content(
                    block.type,
                    block.content,
                    {
                        "path": previous.get("path"),
                        "asset_id": previous.get("asset_id"),
                        "generated": True,
                        "width": previous.get("width"),
                        "height": previous.get("height"),
                    },
                )

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _select_chapters(
        blueprint: CourseBlueprint, request: GenerateRequest
    ) -> list[BlueprintChapter]:
        if not request.chapter_ids:
            return list(blueprint.chapters)
        wanted = set(request.chapter_ids)
        unknown = wanted - set(blueprint.chapter_ids())
        if unknown:
            raise ValidationFailedError(
                f"Unknown chapter ids: {', '.join(sorted(unknown))}",
                details={"available": blueprint.chapter_ids()},
            )
        return [chapter for chapter in blueprint.chapters if chapter.id in wanted]

    def _existing_summaries(
        self, course_id: str, targets: list[BlueprintChapter]
    ) -> dict[str, tuple[int, str, str]]:
        """Summaries of chapters already on disk, for continuity context."""
        target_ids = {chapter.id for chapter in targets}
        summaries: dict[str, tuple[int, str, str]] = {}
        for chapter in self.storage.load_all_chapters(course_id):
            if chapter.chapter_id in target_ids:
                continue  # about to be regenerated
            summaries[chapter.chapter_id] = (
                chapter.chapter_number,
                chapter.title,
                chapter.summary,
            )
        return summaries

    def _estimate_eta(self, done: int, total: int) -> float | None:
        """Remaining seconds, from this run's own measured pace."""
        metrics = current_metrics()
        if metrics is None or done <= 0 or done >= total:
            return 0.0 if done >= total else None
        elapsed_per_chapter = metrics.elapsed / done
        remaining = total - done
        if self.settings.writing_mode == "parallel":
            # Chapters finish in waves, so the remaining time scales with the
            # number of waves left rather than the number of chapters left.
            concurrency = max(1, self.settings.concurrency_for("writer"))
            waves_left = -(-remaining // concurrency)
            return round(elapsed_per_chapter * waves_left, 1)
        return round(elapsed_per_chapter * remaining, 1)

    async def _mark_progress(self, record: CourseRecord, chapter_id: str, **fields) -> None:
        for progress in record.chapters:
            if progress.chapter_id == chapter_id:
                for key, value in fields.items():
                    if value is not None:
                        setattr(progress, key, value)
                break
        await self._save_course(record)

    async def _patch_run(self, course_id: str, **fields) -> None:
        """Update the persisted run record without clobbering concurrent writes."""
        try:
            record = await self._load_course(course_id)
        except NotFoundError:  # pragma: no cover
            return
        run = record.run or RunInfo()
        for key, value in fields.items():
            setattr(run, key, value)
        run.updated_at = utc_now_iso()
        record.run = run
        await self._save_course(record)


_service: CourseService | None = None


def get_course_service() -> CourseService:
    global _service
    if _service is None:
        _service = CourseService()
    return _service


def reset_course_service() -> None:
    global _service
    _service = None
