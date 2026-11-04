/**
 * The knowledge-base screens' decisions: the category tree, the two separate status badges,
 * and which proposals an admin still has to deal with.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { KbCategory, KbItemDetail } from "../api/kb.ts";
import { buildCategoryTree, isBusy, lifecycleBadge, nextStep, processingBadge, proposals } from "./model.ts";

const category = (id: string, name: string, parent: string | null = null, items = 0, order = 0): KbCategory => ({
  id, parent_id: parent, name, slug: name.toLowerCase(), description: "", sort_order: order, is_suggested: false, item_count: items,
});

test("categories nest under their parents and totals include everything beneath", () => {
  const tree = buildCategoryTree([
    category("c", "Child B", "r", 2), category("b", "Child A", "r", 3), category("r", "Root", null, 1), category("z", "Other", null, 4),
    category("g", "Grandchild", "b", 5),
  ]);
  assert.deepEqual(tree.map((n) => [n.name, n.total]), [["Other", 4], ["Root", 11]]);
  const root = tree[1];
  assert.deepEqual(root.children.map((n) => [n.name, n.total]), [["Child A", 8], ["Child B", 2]]);
  assert.equal(root.children[0].children[0].name, "Grandchild");
});

test("explicit order wins over the alphabet, and a category whose parent is gone is not lost", () => {
  const tree = buildCategoryTree([category("a", "Alpha", null, 0, 2), category("b", "Beta", null, 0, 1), category("o", "Orphan", "archived", 1)]);
  assert.deepEqual(tree.map((n) => n.name), ["Orphan", "Beta", "Alpha"]); // by sort_order, not alphabetically
  assert.equal(tree.find((n) => n.name === "Orphan")?.total, 1);
  assert.deepEqual(buildCategoryTree([]), []);
});

test("lifecycle and processing are shown as separate badges, and the trash overrides the lifecycle", () => {
  assert.deepEqual(lifecycleBadge({ lifecycle: "published", deleted: false }), { label: "Published", tone: "success" });
  assert.deepEqual(lifecycleBadge({ lifecycle: "published", deleted: true }), { label: "In trash", tone: "danger" });
  assert.deepEqual(lifecycleBadge({ lifecycle: "draft", deleted: false }), { label: "Draft", tone: "neutral" });
  assert.deepEqual(processingBadge("ready"), { label: "Ready", tone: "success" });
  assert.deepEqual(processingBadge("failed"), { label: "Failed", tone: "danger" });
  assert.deepEqual(processingBadge("embedding"), { label: "Processing", tone: "warning" });
  assert.deepEqual(processingBadge("queued"), { label: "Waiting", tone: "warning" });
  assert.equal(isBusy("parsing"), true);
  assert.equal(isBusy("ready"), false);
  assert.equal(isBusy(null), false);
});

const item = (overrides: Partial<KbItemDetail> = {}): KbItemDetail => ({
  id: "i", number: 1, public_id: "src_x", title: "policy", description: "", lifecycle: "draft", deleted: false, needs_review: true, origin: "upload",
  category: null, tags: [], source_count: 1, processing: "ready", processing_error: null, unpublished_changes: false, updated_at: "2026-01-01T00:00:00Z",
  properties: { title_auto: true }, suggestions: {}, live_version_id: null, latest_version_id: "v1", relations: [],
  versions: [{ id: "v1", version_no: 1, processing_status: "ready", processing_error: null, warnings: [], change_note: "", created_at: "", live_from: null, live_until: null, is_live: false, passages: 3, files: ["a.pdf"], has_text: false }],
  ...overrides,
});

test("only proposals the item does not already reflect are offered", () => {
  const withSuggestions = item({
    suggestions: {
      title: "Refund policy", description: "How refunds work.",
      tags: [{ name: "Refunds", confidence: 0.8, source: "ai" }, { name: "policy", confidence: 0.6, source: "rules" }],
      category: { id: "c1", name: "Finance", status: "assigned", confidence: 0.9, alternatives: [] },
    },
    tags: ["POLICY"],
  });
  const found = proposals(withSuggestions);
  assert.deepEqual(found.map((p) => p.field), ["title", "description", "category", "tags"]);
  assert.deepEqual(found[3].value, ["Refunds"]); // "policy" is already there, whatever its case
  assert.equal((found[2] as { confident: boolean }).confident, true);
});

test("an admin's own choices are never proposed over", () => {
  const chosen = item({
    properties: {}, title: "Refund rules", description: "Mine", category: { id: "c", name: "Finance" },
    suggestions: { title: "Something else", description: "Other", category: { id: "c2", name: "HR", status: "needs_review", confidence: 0.2, alternatives: [] } },
  });
  assert.deepEqual(proposals(chosen), []);
  assert.deepEqual(proposals(item()), []); // and an item with no proposals shows none
});

test("a category the system is unsure of is labelled as only possible", () => {
  const found = proposals(item({ suggestions: { category: { id: "c", name: "HR", status: "needs_review", confidence: 0.2, alternatives: [] } } }));
  assert.equal(found[0].label, "Possible category");
});

test("the drawer leads with the next sensible step", () => {
  assert.match(nextStep(item()) ?? "", /Review the details, then publish/);
  assert.match(nextStep(item({ lifecycle: "published", unpublished_changes: true })) ?? "", /newer version is ready/);
  assert.equal(nextStep(item({ lifecycle: "published" })), null);
  assert.match(nextStep(item({ deleted: true })) ?? "", /trash/);
  const failed = item();
  failed.versions[0].processing_status = "failed";
  assert.match(nextStep(failed) ?? "", /Retry/);
  failed.versions[0].processing_status = "embedding";
  assert.match(nextStep(failed) ?? "", /Being processed/);
  assert.equal(nextStep(item({ versions: [] })), null);
});
