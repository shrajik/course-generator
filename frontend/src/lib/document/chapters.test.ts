/**
 * Run with `npm test` (Node's built-in runner; no extra dependencies) -
 * mirrors links.test.ts's own conventions.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { chaptersFromDocument } from "./chapters.ts";
import type { Block, CourseDocument, Page } from "../types/document.ts";

function block(partial: Partial<Block> & { id: string; type: Block["type"] }): Block {
  return {
    content: {},
    style: {},
    layout: { x: 0, y: 0, width: 0, height: 0, z_index: 0 },
    meta: {},
    ...partial,
  };
}

function page(partial: Partial<Page> & { id: string; blocks: Block[] }): Page {
  return {
    page_number: 1,
    size: { width: 794, height: 1123 },
    background: null,
    kind: "content",
    ...partial,
  };
}

function doc(pages: Page[]): CourseDocument {
  return {
    document_id: "d",
    course_id: "c",
    course_title: "T",
    template_id: "technical_v1",
    version: 1,
    created_at: "",
    updated_at: "",
    meta: { audience: "", template_name: "", chapter_ids: [], theme: {}, generated_with: "", sources: [] },
    pages,
  };
}

test("groups blocks by chapter_id and takes the title from the heading block", () => {
  const p1 = page({
    id: "p1",
    page_number: 1,
    blocks: [
      block({ id: "b1", type: "heading", content: { text: "Chapter One" }, meta: { chapter_id: "ch1", chapter_number: 1 } }),
      block({ id: "b2", type: "paragraph", content: { text: "Intro text." }, meta: { chapter_id: "ch1" } }),
    ],
  });
  const chapters = chaptersFromDocument(doc([p1]));
  assert.equal(chapters.length, 1);
  assert.equal(chapters[0].title, "Chapter One");
  assert.equal(chapters[0].pages[0].blocks.length, 2);
});

test("a chapter spanning two pages gets a pages entry per page", () => {
  const p1 = page({
    id: "p1",
    page_number: 1,
    blocks: [block({ id: "b1", type: "heading", content: { text: "Ch" }, meta: { chapter_id: "ch1", chapter_number: 1 } })],
  });
  const p2 = page({
    id: "p2",
    page_number: 2,
    blocks: [block({ id: "b2", type: "paragraph", meta: { chapter_id: "ch1" } })],
  });
  const chapters = chaptersFromDocument(doc([p1, p2]));
  assert.equal(chapters.length, 1);
  assert.equal(chapters[0].pages.length, 2);
  assert.equal(chapters[0].pages[0].page.id, "p1");
  assert.equal(chapters[0].pages[1].page.id, "p2");
});

test("chapters are sorted by chapter_number, not by appearance order", () => {
  const p1 = page({
    id: "p1",
    page_number: 1,
    blocks: [
      block({ id: "b1", type: "heading", content: { text: "Second" }, meta: { chapter_id: "ch2", chapter_number: 2 } }),
      block({ id: "b2", type: "heading", content: { text: "First" }, meta: { chapter_id: "ch1", chapter_number: 1 } }),
    ],
  });
  const chapters = chaptersFromDocument(doc([p1]));
  assert.deepEqual(chapters.map((c) => c.title), ["First", "Second"]);
});

test("blocks with no chapter_id (cover/toc content) are ignored, and non-content pages are skipped entirely", () => {
  const cover = page({ id: "cover", page_number: 1, kind: "cover", blocks: [block({ id: "b0", type: "heading", meta: { chapter_id: "ch1" } })] });
  const p1 = page({
    id: "p1",
    page_number: 2,
    blocks: [
      block({ id: "b1", type: "heading", content: { text: "Ch" }, meta: {} }),
      block({ id: "b2", type: "paragraph", content: { text: "x" }, meta: { chapter_id: "ch1", chapter_number: 1 } }),
    ],
  });
  const chapters = chaptersFromDocument(doc([cover, p1]));
  assert.equal(chapters.length, 1);
  assert.equal(chapters[0].pages[0].blocks.length, 1);
});

test("an empty document produces no chapters", () => {
  assert.deepEqual(chaptersFromDocument(doc([])), []);
});
