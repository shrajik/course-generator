/**
 * Run with `npm test` (Node's built-in runner; no extra dependencies) -
 * mirrors jargon.test.ts's/links.test.ts's own conventions.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseMath } from "./math.ts";

test("inline $...$ math becomes an inline math segment", () => {
  assert.deepEqual(parseMath("The energy is $K = \\frac{1}{2} m v^2$ here."), [
    { kind: "text", text: "The energy is " },
    { kind: "math", tex: "K = \\frac{1}{2} m v^2", display: false },
    { kind: "text", text: " here." },
  ]);
});

test("display $$...$$ math becomes a display math segment", () => {
  assert.deepEqual(parseMath("Before $$E = mc^2$$ after"), [
    { kind: "text", text: "Before " },
    { kind: "math", tex: "E = mc^2", display: true },
    { kind: "text", text: " after" },
  ]);
});

test("\\(...\\) and \\[...\\] are recognised the same as $...$/$$...$$", () => {
  assert.deepEqual(parseMath("inline \\(a + b\\) and display \\[a^2 + b^2\\]"), [
    { kind: "text", text: "inline " },
    { kind: "math", tex: "a + b", display: false },
    { kind: "text", text: " and display " },
    { kind: "math", tex: "a^2 + b^2", display: true },
  ]);
});

test("multiple inline formulas in one string are each recognised", () => {
  assert.deepEqual(parseMath("$a$ plus $b$ equals $c$"), [
    { kind: "math", tex: "a", display: false },
    { kind: "text", text: " plus " },
    { kind: "math", tex: "b", display: false },
    { kind: "text", text: " equals " },
    { kind: "math", tex: "c", display: false },
  ]);
});

test("text with no math is untouched", () => {
  assert.deepEqual(parseMath("just ordinary prose, nothing to typeset"), [
    { kind: "text", text: "just ordinary prose, nothing to typeset" },
  ]);
});

test("an empty $$ pair is kept as plain text, not treated as math", () => {
  assert.deepEqual(parseMath("price is $$ nothing here"), [
    { kind: "text", text: "price is $$ nothing here" },
  ]);
});

test("visible text/tex always round-trips to the original source", () => {
  const source = "Work: $W = \\int F\\,ds$, and energy: $$K = \\frac{1}{2}mv^2$$ done.";
  const rebuilt = parseMath(source)
    .map((segment) => (segment.kind === "math" ? segment.tex : segment.text))
    .join("")
    // math segments drop their own delimiters, so compare against the
    // delimiter-stripped source rather than the raw original.
    .replace(/\$\$|\$/g, "");
  assert.equal(rebuilt, source.replace(/\$\$|\$/g, ""));
});

test("empty and missing input are safe", () => {
  assert.deepEqual(parseMath(""), []);
  // @ts-expect-error - defensive runtime check for a caller passing undefined
  assert.deepEqual(parseMath(undefined), []);
});
