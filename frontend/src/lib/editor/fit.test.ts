/**
 * When a code cell's content changes height the blocks below it must move, and
 * nothing else may - in both directions. Growing prevents overlap; shrinking
 * stops a cell that lost its output from leaving dead space behind.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { PAGE_BOTTOM_MARGIN, resizeBlock, sheetHeight } from "./fit.ts";

type TestPage = Parameters<typeof resizeBlock>[0];
const SHEET = 1123;

function block(id: string, y: number, height: number, x = 64) {
  return {
    id,
    type: "paragraph",
    content: {},
    style: {},
    layout: { x, y, width: 666, height, z_index: 0 },
    meta: {},
  };
}

function page(blocks: ReturnType<typeof block>[], height = SHEET): TestPage {
  return {
    id: "page_1",
    page_number: 1,
    kind: "content",
    size: { width: 794, height },
    blocks,
  } as unknown as TestPage;
}

const ys = (p: TestPage) => Object.fromEntries(p.blocks.map((b) => [b.id, b.layout.y]));

// --- growing -------------------------------------------------------------------

test("the block takes the requested height", () => {
  const p = page([block("cell", 100, 150)]);
  assert.equal(resizeBlock(p, 0, 240, SHEET), true);
  assert.equal(p.blocks[0].layout.height, 240);
});

test("blocks below move down by exactly the growth", () => {
  const p = page([block("cell", 100, 150), block("below", 270, 60), block("further", 350, 60)]);
  resizeBlock(p, 0, 240, SHEET);
  assert.deepEqual(ys(p), { cell: 100, below: 360, further: 440 });
});

test("blocks above are untouched", () => {
  const p = page([block("above", 40, 50), block("cell", 100, 150), block("below", 270, 60)]);
  resizeBlock(p, 1, 240, SHEET);
  assert.equal(ys(p).above, 40);
});

test("a block flush against the old bottom edge still moves", () => {
  const p = page([block("cell", 100, 150), block("flush", 250, 40)]);
  resizeBlock(p, 0, 200, SHEET);
  assert.equal(ys(p).flush, 300);
});

test("a block beside the cell, above its bottom edge, is left alone", () => {
  const p = page([block("cell", 100, 150), block("beside", 120, 60, 400)]);
  resizeBlock(p, 0, 230, SHEET);
  assert.equal(ys(p).beside, 120);
});

test("the page grows when the content no longer fits", () => {
  const p = page([block("cell", 800, 150), block("below", 960, 60)]);
  resizeBlock(p, 0, 350, SHEET);
  // below now ends at 1220; plus the bottom margin.
  assert.equal(p.size.height, 1220 + PAGE_BOTTOM_MARGIN);
});

test("the page is left alone when everything still fits", () => {
  const p = page([block("cell", 100, 150), block("below", 270, 60)]);
  resizeBlock(p, 0, 240, SHEET);
  assert.equal(p.size.height, SHEET);
});

// --- shrinking -----------------------------------------------------------------

test("a block that lost content gives the room back and pulls the rest up", () => {
  const p = page([block("cell", 100, 240), block("below", 360, 60)]);
  assert.equal(resizeBlock(p, 0, 150, SHEET), true);
  assert.equal(p.blocks[0].layout.height, 150);
  assert.equal(ys(p).below, 270);
});

test("grow then shrink returns every block to where it started", () => {
  const p = page([block("cell", 100, 150), block("below", 270, 60)]);
  resizeBlock(p, 0, 250, SHEET);
  resizeBlock(p, 0, 150, SHEET);
  assert.deepEqual(ys(p), { cell: 100, below: 270 });
  assert.equal(p.blocks[0].layout.height, 150);
});

test("a page that was only grown by a cell returns to the sheet height", () => {
  const p = page([block("cell", 800, 150), block("below", 960, 60)]);
  resizeBlock(p, 0, 350, SHEET);
  assert.ok(p.size.height > SHEET);
  resizeBlock(p, 0, 150, SHEET);
  assert.equal(p.size.height, SHEET);
});

test("a page is never made shorter than the sheet", () => {
  const p = page([block("cell", 100, 400)]);
  resizeBlock(p, 0, 50, SHEET);
  assert.equal(p.size.height, SHEET);
});

test("a page keeps a taller sheet height from the template", () => {
  const letter = 1056;
  const p = page([block("cell", 100, 300)], letter);
  resizeBlock(p, 0, 100, letter);
  assert.equal(p.size.height, letter);
});

// --- noise and safety ----------------------------------------------------------

test("sub-pixel jitter changes nothing", () => {
  const p = page([block("cell", 100, 150), block("below", 270, 60)]);
  assert.equal(resizeBlock(p, 0, 151, SHEET), false);
  assert.equal(p.blocks[0].layout.height, 150);
  assert.equal(ys(p).below, 270);
});

test("an unknown block changes nothing", () => {
  const p = page([block("cell", 100, 150)]);
  assert.equal(resizeBlock(p, 9, 400, SHEET), false);
});

test("cells are independent: resizing one leaves the one above alone", () => {
  const p = page([block("first", 100, 150), block("second", 300, 150)]);
  resizeBlock(p, 1, 250, SHEET);
  assert.equal(p.blocks[0].layout.height, 150);
  assert.equal(p.blocks[1].layout.height, 250);
  assert.equal(ys(p).first, 100);
});

// --- the sheet height ----------------------------------------------------------

test("the sheet height comes from the template's page, else the default", () => {
  assert.equal(sheetHeight({ meta: { theme: { page: { height: 1056 } } } } as never), 1056);
  assert.equal(sheetHeight({ meta: { theme: { page: null } } } as never), 1123);
  assert.equal(sheetHeight({ meta: { theme: {} } } as never), 1123);
  assert.equal(sheetHeight({ meta: { theme: { page: { height: "nope" } } } } as never), 1123);
});
