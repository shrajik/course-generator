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

// --- pipeline reliability follow-up: real image progress + stopped/resumable states ---

test("images progress shows a real fraction once the backend reports images_total/images_done", () => {
  const view = deriveStages(
    detail("illustrating", { run: { state: "running", images_total: 4, images_done: 1, eta_seconds: null } }),
  );
  const images = view.stages.find((s) => s.id === "images");
  assert.ok(images);
  assert.equal(images!.status, "active");
  assert.equal(images!.detail, "1/4 images");
  // 1/4 of the images weight (10) contributes 2.5 points - well under 100.
  assert.ok(view.percent < 100);
});

test("images progress completes once images_done reaches images_total, even before status flips to ready", () => {
  const view = deriveStages(
    detail("illustrating", { run: { state: "running", images_total: 3, images_done: 3, eta_seconds: null } }),
  );
  const images = view.stages.find((s) => s.id === "images");
  assert.equal(images!.status, "completed");
});

test("a cancelled run is reported as stopped, not failed, and offers resume", () => {
  const view = deriveStages(detail("illustrating", { run: { state: "cancelled", eta_seconds: null } }));
  assert.equal(view.stopped, true);
  assert.equal(view.failed, false);
  assert.equal(view.isRunning, false);
  assert.equal(view.currentLabel, "Generation stopped");
});

test("an interrupted run (backend restart) is also reported as stopped, with its own label", () => {
  const view = deriveStages(detail("writing", { run: { state: "interrupted", eta_seconds: null } }));
  assert.equal(view.stopped, true);
  assert.equal(view.failed, false);
  assert.equal(view.currentLabel, "Generation was interrupted");
});

test("a run genuinely in progress is isRunning and not stopped", () => {
  const view = deriveStages(detail("writing", { run: { state: "running", eta_seconds: null } }));
  assert.equal(view.isRunning, true);
  assert.equal(view.stopped, false);
});

// --- "Building Document" sub-stage progress (visual repair, then review) ---

test("Building Document shows real visual-repair progress once the backend reports it", () => {
  const view = deriveStages(
    detail("assembling", {
      run: { state: "running", images_total: 5, images_done: 5, visual_repair_total: 4, visual_repair_done: 1, eta_seconds: null },
    }),
  );
  const document = view.stages.find((s) => s.id === "document");
  assert.ok(document);
  assert.equal(document!.status, "active");
  assert.equal(document!.detail, "Adding visuals to thin pages: 1/4");
});

test("Building Document shows real visual-review progress once repair has finished", () => {
  const view = deriveStages(
    detail("assembling", {
      run: {
        state: "running", images_total: 5, images_done: 5,
        visual_repair_total: 4, visual_repair_done: 4,
        visual_review_total: 7, visual_review_done: 2, eta_seconds: null,
      },
    }),
  );
  const document = view.stages.find((s) => s.id === "document");
  assert.equal(document!.detail, "Reviewing visuals: 2/7");
});

test("Building Document falls back to the generic label once both sub-stages are done", () => {
  const view = deriveStages(
    detail("assembling", {
      run: {
        state: "running", images_total: 5, images_done: 5,
        visual_repair_total: 4, visual_repair_done: 4,
        visual_review_total: 7, visual_review_done: 7, eta_seconds: null,
      },
    }),
  );
  const document = view.stages.find((s) => s.id === "document");
  assert.equal(document!.detail, "Assembling your final document");
});

test("Building Document falls back to the generic label when no sub-stage counters have been reported yet", () => {
  const view = deriveStages(detail("assembling", { run: { state: "running", eta_seconds: null } }));
  const document = view.stages.find((s) => s.id === "document");
  assert.equal(document!.detail, "Assembling your final document");
});
