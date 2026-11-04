import { request, requestForm } from "./client";

/** The admin knowledge base (PostgreSQL). Lifecycle (the admin's decision) and processing (what the
 * pipeline has done) are separate fields everywhere, and never merged into one status. */

export type Lifecycle = "draft" | "published" | "unpublished" | "archived";
export type RelationType = "related_to" | "references" | "derived_from" | "depends_on" | "supersedes" | "duplicate_of";
export type Processing = "queued" | "parsing" | "chunking" | "embedding" | "classifying" | "ready" | "failed";

export interface KbItem {
  id: string;
  number: number;
  public_id: string;
  title: string;
  description: string;
  lifecycle: Lifecycle;
  deleted: boolean;
  needs_review: boolean;
  origin: "migrated" | "upload" | "manual";
  category: { id: string; name: string } | null;
  tags: string[];
  source_count: number;
  processing: Processing | null;
  processing_error: string | null;
  unpublished_changes: boolean;
  updated_at: string;
}

export interface KbVersion {
  id: string;
  version_no: number;
  processing_status: Processing;
  processing_error: string | null;
  warnings: string[];
  change_note: string;
  created_at: string;
  live_from: string | null;
  live_until: string | null;
  is_live: boolean;
  passages: number;
  files: string[];
  has_text: boolean;
}

export interface KbRelation {
  id: string;
  relation_type: RelationType;
  note: string;
  source: string;
  direction: "out" | "in";
  other_id: string;
  other_number: number;
  other_title: string;
}

export interface KbSuggestions {
  generated_by?: "model" | "rules";
  title?: string;
  description?: string;
  tags?: { name: string; confidence: number; source: string }[];
  category?: {
    id: string;
    name: string;
    status: "assigned" | "needs_review";
    confidence: number;
    alternatives: { id: string; name: string; similarity: number }[];
  } | null;
}

export interface KbItemDetail extends KbItem {
  properties: Record<string, unknown>;
  suggestions: KbSuggestions;
  live_version_id: string | null;
  latest_version_id: string | null;
  versions: KbVersion[];
  relations: KbRelation[];
}

export interface KbCategory {
  id: string;
  parent_id: string | null;
  name: string;
  slug: string;
  description: string;
  sort_order: number;
  is_suggested: boolean;
  item_count: number;
}

export interface KbChunk {
  ordinal: number;
  ref: string;
  heading: string;
  page: number | null;
  text: string;
  words: number;
  topics: string[];
  embedded: boolean;
  is_live: boolean;
}

export interface KbActivity {
  id: string;
  action: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface KbHit {
  ref: string;
  filename: string;
  heading: string;
  page: number | null;
  text: string;
  score: number;
}

export interface KbFilters {
  q?: string;
  category_id?: string;
  tag?: string;
  lifecycle?: Lifecycle;
  needs_review?: boolean;
  deleted?: boolean;
  limit?: number;
  offset?: number;
}

export function kbStatus() {
  return request<{ backend: "files" | "postgres"; database: boolean }>("/api/kb/status");
}

export function listItems(filters: KbFilters = {}) {
  return request<{ items: KbItem[]; total: number }>("/api/kb/items", { query: { ...filters } });
}

export function getItem(id: string) {
  return request<KbItemDetail>(`/api/kb/items/${id}`);
}

export function createItem(input: { files?: File[]; bodyMd?: string; title?: string }) {
  const form = new FormData();
  for (const file of input.files ?? []) form.append("files", file);
  if (input.bodyMd) form.append("body_md", input.bodyMd);
  if (input.title) form.append("title", input.title);
  return requestForm<{ id: string; number: number; public_id: string; version_id: string }>("/api/kb/items", form);
}

export function updateItem(
  id: string,
  patch: { title?: string; description?: string; category_id?: string | null; tags?: string[] },
) {
  return request<KbItemDetail>(`/api/kb/items/${id}`, { method: "PATCH", body: patch });
}

export function addVersion(id: string, input: { files?: File[]; bodyMd?: string; keepPreviousSources: boolean; changeNote?: string }) {
  const form = new FormData();
  for (const file of input.files ?? []) form.append("files", file);
  if (input.bodyMd !== undefined) form.append("body_md", input.bodyMd);
  form.append("keep_previous_sources", String(input.keepPreviousSources));
  if (input.changeNote) form.append("change_note", input.changeNote);
  return requestForm<{ version_id: string; version_no: number }>(`/api/kb/items/${id}/versions`, form);
}

export function publishVersion(id: string, versionId: string, expectedLive: string | null) {
  return request<{ result: "noop" | "publish" | "switch" }>(`/api/kb/items/${id}/versions/${versionId}/publish`, {
    method: "POST",
    body: { expected_live_version_id: expectedLive },
  });
}

export function publishItem(id: string) {
  return request<{ result: string }>(`/api/kb/items/${id}/publish`, { method: "POST" });
}

export function retryVersion(id: string, versionId: string) {
  return request<{ result: string }>(`/api/kb/items/${id}/versions/${versionId}/retry`, { method: "POST" });
}

export function lifecycleAction(id: string, action: "unpublish" | "archive" | "restore" | "delete" | "undelete") {
  return request<KbItem>(`/api/kb/items/${id}/${action}`, { method: "POST" });
}

export function itemChunks(id: string, versionId?: string, offset = 0, limit = 30) {
  return request<KbChunk[]>(`/api/kb/items/${id}/chunks`, { query: { version_id: versionId, offset, limit } });
}

export function itemActivity(id: string) {
  return request<KbActivity[]>(`/api/kb/items/${id}/activity`);
}

export function addRelation(id: string, toItemId: string, type: RelationType) {
  return request<{ id: string }>(`/api/kb/items/${id}/relations`, { method: "POST", body: { to_item_id: toItemId, relation_type: type } });
}

export function removeRelation(relationId: string) {
  return request<void>(`/api/kb/relations/${relationId}`, { method: "DELETE" });
}

export function listCategories() {
  return request<KbCategory[]>("/api/kb/categories");
}

export function createCategory(name: string, parentId?: string | null) {
  return request<{ id: string }>("/api/kb/categories", { method: "POST", body: { name, parent_id: parentId ?? null } });
}

export function approveCategory(id: string) {
  return request<unknown>(`/api/kb/categories/${id}`, { method: "PATCH", body: { approve: true } });
}

export function searchKb(query: string, limit = 5) {
  return request<KbHit[]>("/api/kb/search", { method: "POST", body: { query, limit } });
}
