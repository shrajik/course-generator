import assert from "node:assert/strict";
import { test } from "node:test";

import { collectCitations, groupBySource, originLabel, originOf } from "./citations.ts";

const doc = {
  pages: [
    {
      blocks: [
        { id: "b1", content: { text: "Leave is 20 days a year [G2.5]." }, meta: { chapter_id: "chapter_1" } },
        { id: "b2", content: { items: ["Apply in advance [G2.5, S1.3].", "No marker here."] }, meta: { chapter_id: "chapter_1" } },
      ],
    },
    {
      blocks: [
        { id: "b3", content: { questions: [{ question: "How long? [G7.1]", answer: "20 days" }] }, meta: { chapter_id: "chapter_2" } },
        { id: "b4", content: { text: "An array a[0] is not a citation." }, meta: { chapter_id: "chapter_2" } },
      ],
    },
  ],
};

test("every marker is found wherever it sits in a block, with its place", () => {
  const found = collectCitations(doc);
  assert.deepEqual([...found.keys()], ["G2.5", "S1.3", "G7.1"]);
  assert.deepEqual(found.get("G2.5")!.map((p) => p.blockId), ["b1", "b2"]);
  assert.deepEqual(found.get("G7.1"), [{ blockId: "b3", pageIndex: 1, chapterId: "chapter_2" }]);
});

test("the origin is read from the reference", () => {
  assert.equal(originOf("G2.5"), "knowledge");
  assert.equal(originOf("S1.3"), "upload");
  assert.equal(originLabel("knowledge"), "Knowledge library");
});

test("citations are grouped by document, library first, and a lost passage stays visible", () => {
  const groups = groupBySource(collectCitations(doc), [
    { ref: "G2.5", source_id: "src_policy", filename: "Leave Policy.pdf", heading: "", page: 3, text: "..." },
    { ref: "S1.3", source_id: "src_mine", filename: "Onboarding.docx", heading: "", page: null, text: "..." },
  ]);
  assert.deepEqual(
    groups.map((g) => [g.origin, g.filename, g.refs, g.citations, g.chapterIds]),
    [
      ["knowledge", "Leave Policy.pdf", ["G2.5"], 2, ["chapter_1"]],
      ["knowledge", "", ["G7.1"], 1, ["chapter_2"]],
      ["upload", "Onboarding.docx", ["S1.3"], 1, ["chapter_1"]],
    ],
  );
});
