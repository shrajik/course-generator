/**
 * What the knowledge-base screens decide, kept free of React so it can be tested: the category
 * tree, how an item's two separate statuses are shown, and which of the system's proposals an
 * admin still has to accept or dismiss.
 */

import type { KbCategory, KbItem, KbItemDetail, Lifecycle, Processing } from "../api/kb.ts";

export type Tone = "neutral" | "success" | "warning" | "danger";

export interface TreeNode extends KbCategory {
  children: TreeNode[];
  /** Items in this category and everything beneath it. */
  total: number;
}

/** Categories arranged by `parent_id`, each with its running total. A category whose parent is
 * missing (archived) is shown at the top level rather than lost. */
export function buildCategoryTree(categories: KbCategory[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>();
  for (const category of categories) nodes.set(category.id, { ...category, children: [], total: category.item_count });
  const roots: TreeNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.parent_id ? nodes.get(node.parent_id) : undefined;
    (parent ? parent.children : roots).push(node);
  }
  const byOrder = (a: TreeNode, b: TreeNode) => a.sort_order - b.sort_order || a.name.localeCompare(b.name);
  const settle = (node: TreeNode): number => {
    node.children.sort(byOrder);
    node.total = node.item_count + node.children.reduce((sum, child) => sum + settle(child), 0);
    return node.total;
  };
  roots.sort(byOrder);
  roots.forEach(settle);
  return roots;
}

const LIFECYCLE: Record<Lifecycle, { label: string; tone: Tone }> = {
  draft: { label: "Draft", tone: "neutral" },
  published: { label: "Published", tone: "success" },
  unpublished: { label: "Unpublished", tone: "warning" },
  archived: { label: "Archived", tone: "neutral" },
};

/** The admin's decision about the item. A deleted item shows as in the trash whatever it was before. */
export function lifecycleBadge(item: Pick<KbItem, "lifecycle" | "deleted">): { label: string; tone: Tone } {
  return item.deleted ? { label: "In trash", tone: "danger" } : LIFECYCLE[item.lifecycle];
}

const BUSY: Processing[] = ["queued", "parsing", "chunking", "embedding", "classifying"];

export function isBusy(status: Processing | null): boolean {
  return status !== null && BUSY.includes(status);
}

/** What the pipeline has done with the newest version - deliberately a separate badge from the lifecycle. */
export function processingBadge(status: Processing | null): { label: string; tone: Tone } {
  if (status === null || status === "ready") return { label: "Ready", tone: "success" };
  if (status === "failed") return { label: "Failed", tone: "danger" };
  return { label: status === "queued" ? "Waiting" : "Processing", tone: "warning" };
}

export type Proposal =
  | { field: "title"; label: string; value: string }
  | { field: "description"; label: string; value: string }
  | { field: "category"; label: string; value: { id: string; name: string }; confident: boolean }
  | { field: "tags"; label: string; value: string[] };

/** Proposals the system made that the item does not yet reflect. Nothing here is required: an admin
 * can ignore them all, and an item the system filled in on its own simply shows none. */
export function proposals(item: KbItemDetail): Proposal[] {
  const found: Proposal[] = [];
  const s = item.suggestions ?? {};
  if (s.title && s.title !== item.title && item.properties?.title_auto === true) {
    found.push({ field: "title", label: "Suggested title", value: s.title });
  }
  if (s.description && !item.description) {
    found.push({ field: "description", label: "Suggested description", value: s.description });
  }
  if (s.category && !item.category) {
    found.push({
      field: "category",
      label: s.category.status === "assigned" ? "Suggested category" : "Possible category",
      value: { id: s.category.id, name: s.category.name },
      confident: s.category.status === "assigned",
    });
  }
  const have = new Set(item.tags.map((tag) => tag.toLowerCase()));
  const missing = (s.tags ?? []).map((tag) => tag.name).filter((name) => !have.has(name.toLowerCase()));
  if (missing.length > 0) found.push({ field: "tags", label: "Suggested tags", value: missing });
  return found;
}

/** The one action an admin most likely wants next, so the drawer can lead with it. */
export function nextStep(item: KbItemDetail): string | null {
  const newest = item.versions[0];
  if (!newest) return null;
  if (newest.processing_status === "failed") return "Processing failed. Retry it, or upload the file again.";
  if (isBusy(newest.processing_status)) return "Being processed. This page updates by itself.";
  if (item.deleted) return "In the trash. Restore it to use it again.";
  if (item.lifecycle === "draft") return "Review the details, then publish to make it available to courses.";
  if (item.lifecycle === "published" && item.unpublished_changes) return "A newer version is ready. Publish it to replace the live one.";
  return null;
}
