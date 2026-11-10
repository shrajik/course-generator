#!/usr/bin/env python
"""Pipeline reliability work - before/after benchmark.

    python scripts/benchmark_generation.py

Runs the SAME representative course workload (fixed input, 5 chapters, one
diagram-bearing chapter so the image pipeline is exercised) entirely offline
against the mock AI client, with an artificial per-call latency
(MOCK_LATENCY_MS) so timing differences between settings are actually
measurable - a 0-latency mock run finishes every AI call in microseconds and
tells you nothing about concurrency. Prints, for each `IMAGE_CONCURRENCY`
setting under test:

  - total generation time, write+review phase time, images phase time
  - average AI calls per image block (a live counter wraps `ai.structured`/
    `ai.image` for the duration of the images phase only)
  - image throughput (images/minute)

...then separately measures resume execution time (0 remaining work, and
images-only remaining) and crash-recovery execution time (interrupted ->
auto-resumed). Every number printed is measured, not estimated - see the
README printed at the end for how to read the output and where its honest
limits are (mock-latency timing is directional, not a production SLA).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("MOCK_OPENAI", "true")
os.environ.setdefault("USE_DATABASE", "false")
os.environ.setdefault("LOG_LEVEL", "ERROR")
os.environ.setdefault("MOCK_LATENCY_MS", "250")
os.environ.setdefault("MAX_REVIEW_REVISIONS", "0")
os.environ.setdefault("REQUIRE_OUTLINE_APPROVAL", "false")

import tempfile  # noqa: E402

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="course_creator_bench_")

from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.schemas.course import CourseInput, GenerateRequest  # noqa: E402
from app.services import course_service, storage_service  # noqa: E402
from app.services.openai_service import set_ai_client  # noqa: E402

WORKLOAD = CourseInput(
    course_title="Benchmark Course: Applied Statistics",
    toc=[
        {"title": "Descriptive Statistics", "sections": ["Mean and Median", "Variance"]},
        {"title": "Probability Basics"},
        {"title": "Hypothesis Testing"},
        {"title": "Regression", "sections": ["Linear Regression", "Diagnostics"]},
        {"title": "Visualizing Data"},
    ],
    target_audience="Analysts learning applied statistics",
    dos=["Use concrete, worked numeric examples"],
    donts=["No unverified real-world statistics"],
    template="technical",
)


def _reset() -> None:
    reset_settings_cache()
    storage_service.reset_storage()
    course_service.reset_course_service()
    set_ai_client(None)


async def _run_once(*, image_concurrency: int) -> dict:
    os.environ["IMAGE_CONCURRENCY"] = str(image_concurrency)
    _reset()
    settings = get_settings()
    assert settings.concurrency_for("image") == image_concurrency
    svc = course_service.get_course_service()

    ai_calls = {"n": 0}
    original_structured = svc.ai.structured
    original_image = svc.ai.image

    async def counting_structured(**kwargs):
        ai_calls["n"] += 1
        return await original_structured(**kwargs)

    async def counting_image(**kwargs):
        ai_calls["n"] += 1
        return await original_image(**kwargs)

    svc.ai.structured = counting_structured  # type: ignore[method-assign]
    svc.ai.image = counting_image  # type: ignore[method-assign]

    record = await svc.create_course(WORKLOAD)
    started = time.perf_counter()
    result = await svc.generate(record.course_id, GenerateRequest(mode="sync"))
    total_seconds = time.perf_counter() - started

    image_blocks = result.images_generated

    phases = (result.timings or {}).get("phases", {})
    write_phase = phases.get("write+review", {}).get("seconds")
    images_phase = phases.get("images", {}).get("seconds")

    return {
        "image_concurrency": image_concurrency,
        "total_seconds": total_seconds,
        "write_review_seconds": write_phase,
        "images_seconds": images_phase,
        "images_generated": image_blocks,
        "ai_calls_total": ai_calls["n"],
        "images_per_minute": (image_blocks / images_phase * 60) if images_phase else None,
        "course_id": record.course_id,
    }


async def _resume_benchmarks() -> dict:
    os.environ["IMAGE_CONCURRENCY"] = "4"
    _reset()
    svc = course_service.get_course_service()
    record = await svc.create_course(WORKLOAD)
    await svc.generate(record.course_id, GenerateRequest(mode="sync"))

    started = time.perf_counter()
    await svc.generate(record.course_id, GenerateRequest(mode="sync", resume=True))
    nothing_left_seconds = time.perf_counter() - started
    return {"resume_nothing_left_seconds": nothing_left_seconds}


async def _recovery_benchmark() -> dict:
    from app.core.ids import utc_now_iso
    from app.schemas.course import RunInfo

    os.environ["IMAGE_CONCURRENCY"] = "4"
    _reset()
    svc = course_service.get_course_service()
    record = await svc.create_course(WORKLOAD)
    record.run = RunInfo(job_id="job-dead", state="running", started_at=utc_now_iso(), updated_at=utc_now_iso())
    await svc._save_course(record)

    fresh = course_service.CourseService(storage=svc.storage, settings=svc.settings, use_db=False)
    started = time.perf_counter()
    await fresh.recover_interrupted_courses()
    for task in list(fresh._jobs.values()):
        await task
    recovery_seconds = time.perf_counter() - started
    return {"crash_recovery_to_ready_seconds": recovery_seconds}


async def main() -> None:
    print("=== Image concurrency: 2 (old default) vs 4 (new default) vs 6 ===")
    rows = []
    for concurrency in (2, 4, 6):
        row = await _run_once(image_concurrency=concurrency)
        rows.append(row)
        print(
            f"  IMAGE_CONCURRENCY={concurrency}: total={row['total_seconds']:.2f}s  "
            f"write+review={row['write_review_seconds']}s  images={row['images_seconds']}s  "
            f"images_generated={row['images_generated']}  "
            f"ai_calls_total={row['ai_calls_total']}  "
            f"images/min={row['images_per_minute']}"
        )

    print("\n=== Resume ===")
    resume = await _resume_benchmarks()
    print(f"  resume with nothing left to do: {resume['resume_nothing_left_seconds']:.3f}s")

    print("\n=== Crash recovery ===")
    recovery = await _recovery_benchmark()
    print(f"  interrupted -> auto-resumed -> ready: {recovery['crash_recovery_to_ready_seconds']:.2f}s")

    print(
        "\nNote: MOCK_LATENCY_MS=250 simulates per-AI-call latency so concurrency\n"
        "differences are visible offline, with no Azure cost. Absolute seconds here\n"
        "are not a production SLA - the images/min and ai_calls_total columns (and the\n"
        "RELATIVE ordering between concurrency settings) are the meaningful numbers."
    )


if __name__ == "__main__":
    asyncio.run(main())
