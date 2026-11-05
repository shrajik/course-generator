import assert from "node:assert/strict";
import { test } from "node:test";

import { createRequestGuard } from "./requestGuard.ts";

test("a single started request is current", () => {
  const guard = createRequestGuard();
  const a = guard.start();
  assert.equal(guard.isCurrent(a), true);
});

test("starting a second request makes the first stale", () => {
  const guard = createRequestGuard();
  const a = guard.start();
  const b = guard.start();
  assert.equal(guard.isCurrent(a), false);
  assert.equal(guard.isCurrent(b), true);
});

test("out-of-order resolution: an earlier request resolving after a later one is still stale - the exact race from the bug report", () => {
  const guard = createRequestGuard();
  const a = guard.start(); // Course A's fetch starts first
  const b = guard.start(); // admin switches to Course B before A resolves
  // A resolves AFTER B (the reported race) - A must never be current again.
  assert.equal(guard.isCurrent(a), false);
  assert.equal(guard.isCurrent(b), true);
});

test("invalidate() alone, with no new request started, makes any in-flight request stale", () => {
  // Switching away from a course (or closing the panel) without the admin
  // ever re-expanding Model Usage for the new course - the old fetch must
  // still never be allowed to apply once it resolves.
  const guard = createRequestGuard();
  const a = guard.start();
  guard.invalidate();
  assert.equal(guard.isCurrent(a), false);
});

test("after invalidate(), a freshly started request is current again", () => {
  const guard = createRequestGuard();
  guard.invalidate();
  const a = guard.start();
  assert.equal(guard.isCurrent(a), true);
});

test("repeated/overlapping requests for the same course: only the most recently started one is current", () => {
  const guard = createRequestGuard();
  const a = guard.start();
  const b = guard.start();
  const c = guard.start();
  assert.equal(guard.isCurrent(a), false);
  assert.equal(guard.isCurrent(b), false);
  assert.equal(guard.isCurrent(c), true);
});
