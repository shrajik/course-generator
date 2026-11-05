/**
 * Run with `npm test` (Node's built-in runner; no extra dependencies) -
 * mirrors lib/document/chapters.test.ts's own conventions.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { blankEdge, blankNode, dropDanglingEdges, moveItem, nextNodeId } from "./flowchartSpec.ts";
import type { DiagramNode } from "../types/document.ts";

test("nextNodeId picks an id not already used", () => {
  const nodes: DiagramNode[] = [{ id: "n0", label: "a" }, { id: "n1", label: "b" }];
  assert.equal(nextNodeId(nodes), "n2");
});

test("nextNodeId skips past a gap left by a deleted node", () => {
  const nodes: DiagramNode[] = [{ id: "n0", label: "a" }, { id: "n2", label: "c" }];
  const id = nextNodeId(nodes);
  assert.ok(!nodes.some((n) => n.id === id));
});

test("nextNodeId on an empty list", () => {
  assert.equal(nextNodeId([]), "n0");
});

test("blankNode/blankEdge have the expected shape", () => {
  assert.deepEqual(blankNode("n5"), { id: "n5", label: "New step", detail: "", role: "", color: "" });
  assert.deepEqual(blankEdge("n0", "n1"), { source: "n0", target: "n1", label: "" });
});

test("moveItem swaps with the previous item", () => {
  const items = ["a", "b", "c"];
  assert.deepEqual(moveItem(items, 1, -1), ["b", "a", "c"]);
});

test("moveItem swaps with the next item", () => {
  const items = ["a", "b", "c"];
  assert.deepEqual(moveItem(items, 1, 1), ["a", "c", "b"]);
});

test("moveItem is a no-op past either end", () => {
  const items = ["a", "b", "c"];
  assert.equal(moveItem(items, 0, -1), items);
  assert.equal(moveItem(items, 2, 1), items);
});

test("dropDanglingEdges keeps only edges whose both ends still exist", () => {
  const edges = [
    { source: "n0", target: "n1" },
    { source: "n1", target: "n2" }, // n2 deleted
  ];
  const kept = dropDanglingEdges(edges, new Set(["n0", "n1"]));
  assert.deepEqual(kept, [{ source: "n0", target: "n1" }]);
});
