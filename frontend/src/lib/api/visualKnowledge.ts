import { request } from "./client";

export interface VisualKnowledgeEntry {
  id: string;
  course_id: string | null;
  asset_path: string;
  asset_url: string | null;
  kind: "illustration" | "diagram";
  diagram_kind: string | null;
  topic: string;
  description: string;
  tags: string[];
  approved: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface VisualKnowledgeListResponse {
  items: VisualKnowledgeEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface VisualKnowledgeCreateRequest {
  course_id?: string;
  asset_path: string;
  kind?: "illustration" | "diagram";
  diagram_kind?: string;
  topic?: string;
  description?: string;
  tags?: string[];
}

export interface VisualKnowledgeUpdateRequest {
  topic?: string;
  description?: string;
  tags?: string[];
  approved?: boolean;
}

export function listVisualKnowledge(
  options: {
    search?: string;
    kind?: string;
    approved_only?: boolean;
    limit?: number;
    offset?: number;
  } = {},
): Promise<VisualKnowledgeListResponse> {
  return request<VisualKnowledgeListResponse>("/api/visual-knowledge", { query: { ...options } });
}

export function registerVisualKnowledge(
  payload: VisualKnowledgeCreateRequest,
): Promise<VisualKnowledgeEntry> {
  return request<VisualKnowledgeEntry>("/api/visual-knowledge", { method: "POST", body: payload });
}

export function updateVisualKnowledge(
  id: string,
  payload: VisualKnowledgeUpdateRequest,
): Promise<VisualKnowledgeEntry> {
  return request<VisualKnowledgeEntry>(`/api/visual-knowledge/${id}`, {
    method: "PATCH",
    body: payload,
  });
}

export function deleteVisualKnowledge(id: string): Promise<void> {
  return request<void>(`/api/visual-knowledge/${id}`, { method: "DELETE" });
}
