/**
 * The contract every surface relies on: a `<jargon>` term is *displayed* as
 * its label, while the stored source keeps the markup so saving an edited
 * block cannot destroy it.
 *
 * Run with `npm test` (Node's built-in runner; no extra dependencies).
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { hasJargon, parseJargon, stripJargon } from "./jargon.ts";

const SAMPLE = '<jargon term="planning fallacy">planning fallacy</jargon>';

test("a jargon term renders as its label, not as markup", () => {
  assert.deepEqual(parseJargon(SAMPLE), [
    { kind: "jargon", term: "planning fallacy", label: "planning fallacy" },
  ]);
  assert.equal(stripJargon(SAMPLE), "planning fallacy");
});

test("the stored source is never altered by reading it", () => {
  const stored = `Memory is a biased narrator. The ${SAMPLE} leads people to underestimate task time.`;
  const before = stored;

  parseJargon(stored);
  stripJargon(stored);

  // What the editor holds - and therefore what a blur would commit - is
  // byte-identical to what it was given.
  assert.equal(stored, before);
  assert.ok(stored.includes('<jargon term="planning fallacy">'));
});

test("surrounding text is preserved in order", () => {
  assert.deepEqual(
    parseJargon(`The ${SAMPLE} bites.`),
    [
      { kind: "text", text: "The " },
      { kind: "jargon", term: "planning fallacy", label: "planning fallacy" },
      { kind: "text", text: " bites." },
    ],
  );
});

test("the label and the term can differ", () => {
  assert.deepEqual(parseJargon('<jargon term="On-the-job metric">metric</jargon>'), [
    { kind: "jargon", term: "On-the-job metric", label: "metric" },
  ]);
});

test("a term containing > survives", () => {
  // A naive `[^>]*` pattern stops at the first ">" and leaks the remainder.
  assert.deepEqual(parseJargon('<jargon term="Focus > Busyness">Focus > Busyness</jargon>'), [
    { kind: "jargon", term: "Focus > Busyness", label: "Focus > Busyness" },
  ]);
});

test("single-quoted attributes work", () => {
  assert.deepEqual(parseJargon("<jargon term='Baseline audit'>audit</jargon>"), [
    { kind: "jargon", term: "Baseline audit", label: "audit" },
  ]);
});

test("uppercase markup is handled", () => {
  assert.deepEqual(parseJargon('<JARGON TERM="PTPR">PTPR</JARGON>'), [
    { kind: "jargon", term: "PTPR", label: "PTPR" },
  ]);
});

test("a tag with no term is plain text, since there is nothing to define", () => {
  assert.deepEqual(parseJargon("<jargon>bare</jargon>"), [{ kind: "text", text: "bare" }]);
});

test("malformed and orphaned tags never reach the reader", () => {
  assert.deepEqual(parseJargon('unclosed <jargon term="x">tail'), [
    { kind: "text", text: "unclosed tail" },
  ]);
  assert.deepEqual(parseJargon("</jargon> orphan"), [{ kind: "text", text: " orphan" }]);
});

test("ordinary text is untouched", () => {
  assert.deepEqual(parseJargon("plain > angle bracket stays"), [
    { kind: "text", text: "plain > angle bracket stays" },
  ]);
  assert.equal(stripJargon("no tags at all"), "no tags at all");
  assert.equal(hasJargon("no tags at all"), false);
  assert.equal(hasJargon(SAMPLE), true);
});

test("visible text always round-trips to the stripped source", () => {
  const source = `A ${SAMPLE} and <jargon term='T'>t</jargon> plus <jargon>bare</jargon> end`;
  const visible = parseJargon(source)
    .map((segment) => (segment.kind === "jargon" ? segment.label : segment.text))
    .join("");
  assert.equal(visible, stripJargon(source));
});

test("empty and missing input are safe", () => {
  assert.deepEqual(parseJargon(""), []);
  assert.equal(stripJargon(""), "");
});
