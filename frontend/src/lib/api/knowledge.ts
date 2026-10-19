import { request, requestForm } from "./client";
import type { SourceRecord } from "./sources";

/** A library document: a source record plus the fields only the library uses. */
export interface KnowledgeDocument extends SourceRecord {
  global_number: number | null;
  enabled: boolean;
  description: string;
}

export interface KnowledgeStats {
  documents: number;
  searchable: number;
  passages: number;
  processing: number;
}

export interface KnowledgeList {
  documents: KnowledgeDocument[];
  stats: KnowledgeStats;
}

export interface KnowledgeChunk {
  index: number;
  heading_path: string[];
  page: number | null;
  text: string;
  words: number;
}

export interface KnowledgeHit {
  ref: string;
  filename: string;
  heading: string;
  page: number | null;
  text: string;
  score: number;
}

export function listKnowledge(): Promise<KnowledgeList> {
  return request<KnowledgeList>("/api/knowledge");
}

export function uploadKnowledge(file: File, description = ""): Promise<KnowledgeDocument> {
  const form = new FormData();
  form.append("file", file);
  form.append("description", description);
  return requestForm<KnowledgeDocument>("/api/knowledge", form);
}

export function updateKnowledge(
  id: string,
  patch: { enabled?: boolean; description?: string },
): Promise<KnowledgeDocument> {
  return request<KnowledgeDocument>(`/api/knowledge/${id}`, { method: "PATCH", body: patch });
}

export function deleteKnowledge(id: string): Promise<void> {
  return request<void>(`/api/knowledge/${id}`, { method: "DELETE" });
}

export function knowledgeChunks(id: string, offset = 0, limit = 20): Promise<KnowledgeChunk[]> {
  return request<KnowledgeChunk[]>(`/api/knowledge/${id}/chunks`, { query: { offset, limit } });
}

/** Try a query as a chapter's search would: what a course would be offered. */
export function searchKnowledge(query: string, limit = 5): Promise<KnowledgeHit[]> {
  return request<KnowledgeHit[]>("/api/knowledge/search", { method: "POST", body: { query, limit } });
}
