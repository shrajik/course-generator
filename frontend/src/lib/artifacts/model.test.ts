/** How artifact memory reads to a reviewer: states, what can be decided, what future courses are offered. */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { ArtifactDetail, ArtifactVersion } from "../api/artifacts.ts";
import { citation, isOffered, provenance, reviewActions, reviewBadge, typeLabel, versionRows } from "./model.ts";

const version = (no: number, overrides: Partial<ArtifactVersion> = {}): ArtifactVersion => ({
  id: `v${no}`, version_no: no, review_status: "pending_review", search_text: "", source_refs: [], generation_run_id: null, model_info: {},
  locked: false, is_active: false, indexed: false, created_at: "", submitted_at: null, reviewed_at: null, review_comment: null, approved_by: null, approved_at: null,
  ...overrides,
});

const artifact = (versions: ArtifactVersion[], overrides: Partial<ArtifactDetail> = {}): ArtifactDetail => ({
  id: "a", number: 7, public_id: "art_x", artifact_type: "chapter", title: "Refund rules", description: "", course_id: "c1", chapter_id: "ch_1",
  course_title: "Refund Handling", status: "active", updated_at: "", latest_version_id: versions[0].id, latest_version: versions[0].version_no,
  review_status: versions[0].review_status, review_comment: null, submitted_at: null, active_version: null, ever_approved: false,
  versions, links: [], activity: [], ...overrides,
});

test("review states read plainly", () => {
  assert.deepEqual(reviewBadge("approved"), { label: "Approved", tone: "success" });
  assert.deepEqual(reviewBadge("pending_review"), { label: "Waiting for review", tone: "warning" });
  assert.deepEqual(reviewBadge("changes_requested"), { label: "Changes requested", tone: "danger" });
  assert.equal(typeLabel("case_study"), "Case study");
  assert.equal(typeLabel("questions"), "Question set");
});

test("the citation is the artifact number and the version number", () => {
  assert.equal(citation({ number: 7 }, { version_no: 2 }), "A7.2");
});

test("only the newest version waiting for review can be decided", () => {
  const waiting = artifact([version(2), version(1, { review_status: "approved", locked: true, is_active: true })]);
  assert.deepEqual(reviewActions(waiting, waiting.versions[0]), { canDecide: true, reason: null });
  assert.match(reviewActions(waiting, waiting.versions[1]).reason ?? "", /locked/);
  const sentBack = artifact([version(1, { review_status: "changes_requested" })]);
  assert.match(reviewActions(sentBack, sentBack.versions[0]).reason ?? "", /Sent back/);
  const superseded = artifact([version(3), version(2)]);
  assert.match(reviewActions(superseded, superseded.versions[1]).reason ?? "", /newer version/);
  const archived = artifact([version(1)], { status: "archived" });
  assert.equal(reviewActions(archived, archived.versions[0]).canDecide, false);
});

test("a version is offered to future courses only if it is the active one of an artifact that is not archived", () => {
  assert.equal(isOffered({ status: "active" }, { is_active: true }), true);
  assert.equal(isOffered({ status: "archived" }, { is_active: true }), false);
  assert.equal(isOffered({ status: "active" }, { is_active: false }), false);
});

test("versions are listed newest first with what is offered marked", () => {
  const detail = artifact([version(3), version(2, { review_status: "approved", locked: true, is_active: true }), version(1, { review_status: "approved", locked: true })]);
  const rows = versionRows(detail);
  assert.deepEqual(rows.map((r) => [r.ref, r.offered, r.canDecide]), [["A7.3", false, true], ["A7.2", true, false], ["A7.1", false, false]]);
});

test("provenance names the course, the run and what it was written from", () => {
  assert.equal(provenance({ course_title: "Refund Handling", course_id: "c1" }, { generation_run_id: "job_7", source_refs: ["G2.5", "S1.3"] }), "Refund Handling · run job_7 · from G2.5, S1.3");
  assert.equal(provenance({ course_title: "", course_id: "c1" }), "c1");
  assert.equal(provenance({ course_title: "", course_id: null }), "No course");
});
