import { request, requestForm } from "./client";

export type SourceStatus = "uploaded" | "parsing" | "chunking" | "embedding" | "ready" | "failed";

/** Mirrors backend app/schemas/source.py. */
export interface SourceRecord {
  source_id: string;
  filename: string;
  kind: "pdf" | "docx" | "txt" | "md";
  size_bytes: number;
  status: SourceStatus;
  message: string;
  error: string | null;
  page_count: number | null;
  word_count: number;
  chunk_count: number;
  embedded_chunks: number;
  outline: string[];
  created_at: string;
  updated_at: string;
}

export const ACCEPTED_SOURCE_TYPES = ".pdf,.docx,.txt,.md,.markdown";

export function isProcessing(status: SourceStatus): boolean {
  return status !== "ready" && status !== "failed";
}

export function uploadSource(file: File): Promise<SourceRecord> {
  const form = new FormData();
  form.append("file", file);
  return requestForm<SourceRecord>("/api/sources", form);
}

export function listSources(): Promise<SourceRecord[]> {
  return request<SourceRecord[]>("/api/sources");
}

export function getSource(sourceId: string): Promise<SourceRecord> {
  return request<SourceRecord>(`/api/sources/${sourceId}`);
}

export function deleteSource(sourceId: string): Promise<void> {
  return request<void>(`/api/sources/${sourceId}`, { method: "DELETE" });
}
