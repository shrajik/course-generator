/**
 * Run with `npm test` (Node's built-in runner; no extra dependencies) -
 * mirrors flowchartSpec.test.ts's own conventions.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { blankLabel, clampPct, nextLabelId, pctFromPointer } from "./overlayLabels.ts";
import type { OverlayLabel } from "../types/document.ts";

test("nextLabelId picks an id not already used", () => {
  const labels: OverlayLabel[] = [
    { id: "ol0", text: "a", x_pct: 0, y_pct: 0, style: "label" },
    { id: "ol1", text: "b", x_pct: 0, y_pct: 0, style: "label" },
  ];
  assert.equal(nextLabelId(labels), "ol2");
});

test("nextLabelId on an empty list", () => {
  assert.equal(nextLabelId([]), "ol0");
});

test("blankLabel has the expected shape", () => {
  assert.deepEqual(blankLabel("ol3"), { id: "ol3", text: "New label", x_pct: 50, y_pct: 50, style: "label", color: "" });
});

test("clampPct keeps values within 0..100", () => {
  assert.equal(clampPct(-5), 0);
  assert.equal(clampPct(105), 100);
  assert.equal(clampPct(42.5), 42.5);
});

test("pctFromPointer maps a point to a percentage of the given rect", () => {
  const rect = { left: 100, top: 50, width: 200, height: 100 };
  assert.deepEqual(pctFromPointer(200, 100, rect), { x_pct: 50, y_pct: 50 });
  assert.deepEqual(pctFromPointer(100, 50, rect), { x_pct: 0, y_pct: 0 });
  assert.deepEqual(pctFromPointer(300, 150, rect), { x_pct: 100, y_pct: 100 });
});

test("pctFromPointer clamps a point outside the rect", () => {
  const rect = { left: 0, top: 0, width: 100, height: 100 };
  assert.deepEqual(pctFromPointer(-20, 150, rect), { x_pct: 0, y_pct: 100 });
});

test("pctFromPointer is safe against a zero-size rect", () => {
  const rect = { left: 0, top: 0, width: 0, height: 0 };
  assert.deepEqual(pctFromPointer(10, 10, rect), { x_pct: 50, y_pct: 50 });
});
