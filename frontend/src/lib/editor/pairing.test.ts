/**
 * A section-intro illustration must never hide the text beside it: the
 * paragraph is told to wrap around exactly the corner the picture occupies.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { FLOAT_GAP_BOTTOM, FLOAT_GAP_RIGHT, isSectionIntroImage, wrapAroundFor } from "./pairing.ts";

type B = Parameters<typeof wrapAroundFor>[1];

function block(
  id: string,
  type: string,
  layout: { x: number; y: number; width: number; height: number },
  content: Record<string, unknown> = {},
): B {
  return { id, type, content, style: {}, layout: { z_index: 0, ...layout }, meta: {} } as unknown as B;
}

const IMAGE = block("img", "image", { x: 64, y: 140, width: 210, height: 118 }, {
  illustration_style: "section_intro",
});
const PARAGRAPH = block("p", "paragraph", { x: 64, y: 140, width: 666, height: 200 });

test("the paragraph beside a section-intro image wraps around its corner", () => {
  assert.deepEqual(wrapAroundFor([IMAGE, PARAGRAPH], PARAGRAPH), {
    width: 210 + FLOAT_GAP_RIGHT,
    height: 118 + FLOAT_GAP_BOTTOM,
  });
});

test("the corner reserved matches the PDF's float (image width + 16px, height + 10px)", () => {
  const wrap = wrapAroundFor([IMAGE, PARAGRAPH], PARAGRAPH);
  assert.equal(wrap?.width, 226);
  assert.equal(wrap?.height, 128);
});

test("an ordinary image does not make a paragraph wrap", () => {
  const plain = block("img", "image", { x: 64, y: 140, width: 666, height: 375 });
  assert.equal(wrapAroundFor([plain, PARAGRAPH], PARAGRAPH), null);
});

test("an image elsewhere on the page does not", () => {
  const below = block("img", "image", { x: 64, y: 500, width: 210, height: 118 }, {
    illustration_style: "section_intro",
  });
  assert.equal(wrapAroundFor([below, PARAGRAPH], PARAGRAPH), null);
});

test("moving the image away ends the pairing", () => {
  const moved = block("img", "image", { x: 64, y: 143, width: 210, height: 118 }, {
    illustration_style: "section_intro",
  });
  assert.equal(wrapAroundFor([moved, PARAGRAPH], PARAGRAPH), null);
});

test("only paragraphs wrap", () => {
  const heading = block("h", "heading", { x: 64, y: 140, width: 666, height: 40 });
  assert.equal(wrapAroundFor([IMAGE, heading], heading), null);
});

test("style matching is case- and whitespace-insensitive", () => {
  const image = block("img", "image", { x: 64, y: 140, width: 210, height: 118 }, {
    illustration_style: "  Section_Intro ",
  });
  assert.equal(isSectionIntroImage(image), true);
});

test("a page with no images is unaffected", () => {
  assert.equal(wrapAroundFor([PARAGRAPH], PARAGRAPH), null);
});
