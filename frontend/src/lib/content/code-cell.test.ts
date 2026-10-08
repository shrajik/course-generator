/**
 * Output must belong to the code that produced it (acceptance test 8: stale
 * output), and every cell is independent (acceptance test 9).
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CELL,
  codeLines,
  describeOutcome,
  outputLabel,
  outputState,
  readExecution,
} from "./code-cell.ts";

const RAN = {
  status: "success",
  language: "python",
  stdout: "30\n",
  stderr: "",
  execution_time: 0.31,
  source: "print(10 + 20)",
};

const cell = (overrides: Record<string, unknown> = {}) => ({
  language: "python",
  code: "print(10 + 20)",
  execution: RAN,
  ...overrides,
});

test("a cell that has never run has no output", () => {
  assert.deepEqual(outputState(cell({ execution: null })), { kind: "none" });
  assert.deepEqual(outputState({ language: "python", code: "x" }), { kind: "none" });
});

test("output is current while the code is unchanged", () => {
  const state = outputState(cell());
  assert.equal(state.kind, "current");
  assert.equal(state.kind === "current" && state.execution.stdout, "30\n");
});

test("editing the code makes the old output stale", () => {
  assert.deepEqual(outputState(cell({ code: "print(10 + 21)" })), { kind: "stale" });
});

test("a trailing space counts as an edit", () => {
  assert.deepEqual(outputState(cell({ code: "print(10 + 20) " })), { kind: "stale" });
});

test("changing the language makes the old output stale", () => {
  assert.deepEqual(outputState(cell({ language: "javascript" })), { kind: "stale" });
});

test("restoring the original code makes the output current again", () => {
  assert.equal(outputState(cell({ code: "changed" })).kind, "stale");
  assert.equal(outputState(cell({ code: "print(10 + 20)" })).kind, "current");
});

test("stale output is never exposed, even though it is still stored", () => {
  const state = outputState(cell({ code: "print(99)" }));
  assert.equal("execution" in state, false);
});

test("cells do not share state", () => {
  const first = cell({ code: "print(1)", execution: { ...RAN, source: "print(1)", stdout: "1\n" } });
  const second = cell({ code: "print(2)", execution: { ...RAN, source: "print(1)", stdout: "1\n" } });
  assert.equal(outputState(first).kind, "current");
  // The second cell's code moved on; the first cell's result is unaffected.
  assert.equal(outputState(second).kind, "stale");
  assert.equal(outputState(first).kind, "current");
});

test("malformed stored executions are ignored rather than trusted", () => {
  assert.equal(readExecution("nope"), null);
  assert.equal(readExecution({ status: "weird" }), null);
  assert.equal(readExecution(null), null);
  assert.equal(outputState(cell({ execution: { status: "weird" } })).kind, "none");
});

test("a cell shows one numbered row per line, without a phantom trailing row", () => {
  assert.deepEqual(codeLines("a\nb\n"), ["a", "b"]);
  assert.deepEqual(codeLines("a\r\nb"), ["a", "b"]);
  assert.deepEqual(codeLines(""), [""]);
  assert.deepEqual(codeLines("a\n\nc"), ["a", "", "c"]);
});

test("output headers match the spec for every status", () => {
  const base = readExecution(RAN)!;
  assert.equal(outputLabel(base), "OUTPUT · SUCCESS · 0.31s");
  assert.equal(
    outputLabel({ ...base, status: "error", execution_time: 0.42 }),
    "OUTPUT · ERROR · 0.42s",
  );
  assert.equal(
    outputLabel({ ...base, status: "timeout", execution_time: 10 }),
    "OUTPUT · TIMEOUT · 10.00s",
  );
});

test("the row height is a whole number of pixels, so heights add up exactly", () => {
  assert.equal(CELL.rowH % 1, 0);
  assert.ok(CELL.rowH >= Math.ceil(CELL.font));
});

test("outcomes read in plain language", () => {
  const base = readExecution(RAN)!;
  assert.equal(describeOutcome(base), "Success");
  assert.equal(describeOutcome({ ...base, status: "timeout" }), "Timed out");
  assert.equal(describeOutcome({ ...base, status: "error", phase: "run" }), "Error");
  assert.equal(describeOutcome({ ...base, status: "error", phase: "compile" }), "Compilation failed");
});
