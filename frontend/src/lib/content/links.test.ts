/**
 * Run with `npm test` (Node's built-in runner; no extra dependencies) -
 * mirrors jargon.test.ts's own conventions.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseLinks } from "./links.ts";

test("a bare URL becomes a link segment", () => {
  assert.deepEqual(parseLinks("https://docs.python.org/3/"), [
    { kind: "link", url: "https://docs.python.org/3/" },
  ]);
});

test("a title-dash-url source line splits into text then link", () => {
  assert.deepEqual(parseLinks("Official Python Docs — https://docs.python.org/3/"), [
    { kind: "text", text: "Official Python Docs — " },
    { kind: "link", url: "https://docs.python.org/3/" },
  ]);
});

test("trailing sentence punctuation is not part of the link", () => {
  assert.deepEqual(parseLinks("See https://example.test/a."), [
    { kind: "text", text: "See " },
    { kind: "link", url: "https://example.test/a" },
    { kind: "text", text: "." },
  ]);
});

test("multiple links in one string are each recognised", () => {
  assert.deepEqual(parseLinks("a: https://a.test/x, b: https://b.test/y"), [
    { kind: "text", text: "a: " },
    { kind: "link", url: "https://a.test/x" },
    { kind: "text", text: ", b: " },
    { kind: "link", url: "https://b.test/y" },
  ]);
});

test("a bare domain with no scheme is never linked", () => {
  assert.deepEqual(parseLinks("visit docs.python.org for more"), [
    { kind: "text", text: "visit docs.python.org for more" },
  ]);
});

test("text with no URL is untouched", () => {
  assert.deepEqual(parseLinks("just ordinary prose, nothing to link"), [
    { kind: "text", text: "just ordinary prose, nothing to link" },
  ]);
});

test("a URL in parentheses keeps the closing paren out of the link", () => {
  assert.deepEqual(parseLinks("(see https://example.test/page)"), [
    { kind: "text", text: "(see " },
    { kind: "link", url: "https://example.test/page" },
    { kind: "text", text: ")" },
  ]);
});

test("visible text always round-trips to the original source", () => {
  const source = "Title — https://a.test/x, and more: https://b.test/y.";
  const rebuilt = parseLinks(source)
    .map((segment) => (segment.kind === "link" ? segment.url : segment.text))
    .join("");
  assert.equal(rebuilt, source);
});

test("empty and missing input are safe", () => {
  assert.deepEqual(parseLinks(""), []);
  // @ts-expect-error - defensive runtime check for a caller passing undefined
  assert.deepEqual(parseLinks(undefined), []);
});
