/**
 * Small, pure helpers for the Edit Visuals flowchart editor
 * (FlowchartEditor.tsx) - array/id bookkeeping only, no rendering, no
 * network calls, kept separate so they're independently testable the same
 * way chapters.ts (Content Studio Phase 8) already is.
 */

import type { DiagramEdge, DiagramNode } from "@/lib/types/document";

/** A short, unused node id in the same "n0", "n1", ... shape the backend
 * itself generates, never colliding with an id already in `nodes`. */
export function nextNodeId(nodes: DiagramNode[]): string {
  const used = new Set(nodes.map((n) => n.id));
  let index = nodes.length;
  let id = `n${index}`;
  while (used.has(id)) {
    index += 1;
    id = `n${index}`;
  }
  return id;
}

export function blankNode(id: string): DiagramNode {
  return { id, label: "New step", detail: "", role: "", color: "" };
}

export function blankEdge(source: string, target: string): DiagramEdge {
  return { source, target, label: "" };
}

/** Moves the item at `index` one slot earlier (-1) or later (+1); a no-op
 * (returns the same array reference) at either end. */
export function moveItem<T>(items: T[], index: number, direction: -1 | 1): T[] {
  const target = index + direction;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/** Edges referencing a node id that no longer exists are dropped - called
 * after a node delete so the spec sent to the server is never internally
 * inconsistent (an edge to/from nothing). */
export function dropDanglingEdges(edges: DiagramEdge[], nodeIds: Set<string>): DiagramEdge[] {
  return edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
}
