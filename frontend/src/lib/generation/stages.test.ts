/**
 * Quality audit follow-up (Phase 3): `course.quality_snapshot` is a new,
 * additive field on CourseRecord. These tests exist to prove deriveStages'
 * existing ready/finished/polling-relevant behaviour is completely
 * unaffected by its presence - the quality-attention banner in
 * GenerationProgress.tsx reads `quality_snapshot` directly and separately,
 * it must never change what `ready`/`finished` mean (see IN_FLIGHT and the
 * `ready` derivation in stages.ts, and GenerationProgress.tsx's own
 * `status === "ready"` poll-stop / auto-redirect conditions, which this
 * file does not touch).
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { deriveStages } from "./stages.ts";

type Detail = Parameters<typeof deriveStages>[0];

function detail(
  status: string,
  overrides: Record<string, unknown> = {},
  qualitySnapshot: Record<string, unknown> | null = null,
): Detail {
  const chapter = { chapter_id: "c1", title: "Chapter One", researched: true, written: true, reviewed: true };
  return {
    course: {
      course_id: "crs_1",
      document_id: "doc_1",
      status,
      chapters: [chapter],
      has_document: true,
      has_blueprint: true,
      warnings: [],
      last_error: null,
      quality_snapshot: qualitySnapshot,
      run: null,
      ...overrides,
    },
    artifacts: { blueprint: true, document: true, chapters: [], research: [], pdf: null },
  } as unknown as Detail;
}

test("a ready course with no quality snapshot is still finished (pre-existing, unaffected courses)", () => {
  const view = deriveStages(detail("ready", {}, null));
  assert.equal(view.finished, true);
  assert.equal(view.failed, false);
});

test("a ready course with a PASSED quality snapshot is still finished, same as before", () => {
  const view = deriveStages(
    detail("ready", {}, { standards_version: "2026.1", outcome: "passed", hard_failure_count: 0 }),
  );
  assert.equal(view.finished, true);
  assert.equal(view.failed, false);
});

test("a ready course with a FAILED quality snapshot is STILL finished and NOT marked failed - status/quality are independent, exactly as designed", () => {
  const view = deriveStages(
    detail("ready", {}, { standards_version: "2026.1", outcome: "failed", hard_failure_count: 1 }),
  );
  assert.equal(view.finished, true);
  assert.equal(view.failed, false);
});

test("a course still generating (not status==='ready') is not finished, regardless of any quality snapshot", () => {
  const view = deriveStages(
    detail(
      "writing",
      { chapters: [{ chapter_id: "c1", title: "Chapter One", researched: true, written: false, reviewed: false }] },
      { standards_version: "2026.1", outcome: "failed", hard_failure_count: 1 },
    ),
  );
  assert.equal(view.finished, false);
});

test("a genuinely failed generation run is unaffected by quality_snapshot being present", () => {
  const view = deriveStages(detail("failed", {}, { standards_version: "2026.1", outcome: "failed", hard_failure_count: 1 }));
  assert.equal(view.failed, true);
  assert.equal(view.finished, false);
});
