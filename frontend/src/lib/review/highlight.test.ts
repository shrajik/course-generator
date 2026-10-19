/**
 * What the reviewer is shown as flagged must be exactly the flagged sentence,
 * and source markers must be found wherever they appear.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { segmentText, sourceNumber, splitMarkers } from "./highlight.ts";

const finding = (id: string, start: number | null, end: number | null) => ({ finding_id: id, start, end });

const TEXT = "First sentence here. Second claim is flagged. Third sentence ends it.";

test("a flagged sentence becomes its own segment and the rest stays plain", () => {
  const start = TEXT.indexOf("Second");
  const end = TEXT.indexOf(" Third");
  const parts = segmentText(TEXT, [finding("a", start, end)]);
  assert.deepEqual(parts.map((p) => p.text), ["First sentence here. ", "Second claim is flagged.", " Third sentence ends it."]);
  assert.equal(parts[1].finding?.finding_id, "a");
  assert.equal(parts[0].finding, undefined);
  assert.equal(parts.map((p) => p.text).join(""), TEXT);
});

test("several findings are placed in text order whatever order they arrive in", () => {
  const parts = segmentText(TEXT, [finding("late", 45, 69), finding("early", 0, 20)]);
  assert.deepEqual(parts.filter((p) => p.finding).map((p) => p.finding!.finding_id), ["early", "late"]);
  assert.equal(parts.map((p) => p.text).join(""), TEXT);
});

test("a finding with no position, or an impossible one, is left out of the text", () => {
  for (const bad of [finding("a", null, null), finding("b", 30, 10), finding("c", -1, 5), finding("d", 0, 9999), finding("e", 5, 5)]) {
    assert.deepEqual(segmentText(TEXT, [bad]), [{ text: TEXT }]);
  }
});

test("a finding that overlaps an earlier one is skipped, not double-marked", () => {
  const parts = segmentText(TEXT, [finding("outer", 0, 40), finding("inner", 10, 30)]);
  assert.deepEqual(parts.filter((p) => p.finding).map((p) => p.finding!.finding_id), ["outer"]);
  assert.equal(parts.map((p) => p.text).join(""), TEXT);
});

test("no findings leaves the text whole, and empty text stays empty", () => {
  assert.deepEqual(segmentText(TEXT, []), [{ text: TEXT }]);
  assert.deepEqual(segmentText("", []), [{ text: "" }]);
});

test("a flagged stretch at the very start or end leaves no empty neighbours", () => {
  const parts = segmentText("Only sentence.", [finding("a", 0, 14)]);
  assert.deepEqual(parts, [{ text: "Only sentence.", finding: finding("a", 0, 14) }]);
});

test("source markers are found, including several in one bracket", () => {
  const parts = splitMarkers("Refunds take 14 days [S1.1]. See also [S2.3, S2.4] for more.");
  assert.deepEqual(parts.filter((p) => p.kind === "ref").map((p) => (p as { ref: string }).ref), ["S1.1", "S2.3", "S2.4"]);
  assert.equal(parts[0].kind, "text");
});

test("brackets that are not source markers are left as text", () => {
  assert.deepEqual(splitMarkers("An array a[0] and [Note] and [S1] stay as written."), [
    { kind: "text", text: "An array a[0] and [Note] and [S1] stay as written." },
  ]);
});

test("text with no marker comes back as one piece", () => {
  assert.deepEqual(splitMarkers("Plain."), [{ kind: "text", text: "Plain." }]);
  assert.deepEqual(splitMarkers(""), []);
});

test("the source number is read from a reference", () => {
  assert.equal(sourceNumber("S2.14"), 2);
  assert.equal(sourceNumber("S10.1"), 10);
  assert.equal(sourceNumber("nope"), null);
});

test("a knowledge-library reference is a marker too, mixed freely with course sources", () => {
  const parts = splitMarkers("Rotate passwords every ninety days [G2.5]. See [S1.3, G10.1].");
  assert.deepEqual(parts.filter((p) => p.kind === "ref").map((p) => (p as { ref: string }).ref), ["G2.5", "S1.3", "G10.1"]);
});

test("only a course source has a tab; a library reference has no source number", () => {
  assert.equal(sourceNumber("G2.5"), null);
});
