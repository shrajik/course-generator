import { request } from "./client";

/** Artifact memory (Memory 2): SME-approved, immutable, reusable course artifacts. */

export type ArtifactType = "chapter" | "visual" | "questions" | "assessment" | "activity" | "case_study" | "other";
export type ReviewStatus = "pending_review" | "approved" | "changes_requested";

export interface ArtifactSummary {
  id: string;
  number: number;
  public_id: string;
  artifact_type: ArtifactType;
  title: string;
  description: string;
  course_id: string | null;
  chapter_id: string | null;
  course_title: string;
  status: "active" | "archived";
  updated_at: string;
  latest_version_id: string;
  latest_version: number;
  review_status: ReviewStatus;
  review_comment: string | null;
  submitted_at: string | null;
  active_version: number | null;
  ever_approved: boolean;
}

export interface ArtifactVersion {
  id: string;
  version_no: number;
  review_status: ReviewStatus;
  search_text: string;
  source_refs: string[];
  generation_run_id: string | null;
  model_info: Record<string, unknown>;
  locked: boolean;
  is_active: boolean;
  indexed: boolean;
  created_at: string;
  submitted_at: string | null;
  reviewed_at: string | null;
  review_comment: string | null;
  approved_by: string | null;
  approved_at: string | null;
}

export interface ArtifactLink {
  id: string;
  link_type: string;
  note: string;
  direction: "out" | "in";
  target_course_id: string | null;
  target_kb_number: number | null;
  target_kb_ordinal: number | null;
  other_id: string | null;
  other_number: number | null;
  other_title: string | null;
}

export interface ArtifactDetail extends ArtifactSummary {
  versions: ArtifactVersion[];
  links: ArtifactLink[];
  activity: { id: string; action: string; created_at: string }[];
}

export interface ArtifactHit {
  ref: string;
  artifact_type: ArtifactType;
  title: string;
  course_title: string;
  text: string;
  score: number;
}

export interface ArtifactFilters {
  artifact_type?: ArtifactType;
  review_status?: ReviewStatus;
  status?: "active" | "archived";
  q?: string;
  limit?: number;
  offset?: number;
}

export const listArtifacts = (filters: ArtifactFilters = {}) =>
  request<{ items: ArtifactSummary[]; total: number }>("/api/artifacts", { query: { ...filters } });
export const artifactCounts = () => request<Record<ReviewStatus, number>>("/api/artifacts/counts");
export const getArtifact = (id: string) => request<ArtifactDetail>(`/api/artifacts/${id}`);
export const approveVersion = (versionId: string, comment?: string) =>
  request<unknown>(`/api/artifacts/versions/${versionId}/approve`, { method: "POST", body: { comment: comment || null } });
export const requestChanges = (versionId: string, comment: string) =>
  request<unknown>(`/api/artifacts/versions/${versionId}/request-changes`, { method: "POST", body: { comment } });
export const archiveArtifact = (id: string) => request<unknown>(`/api/artifacts/${id}/archive`, { method: "POST" });
export const restoreArtifact = (id: string) => request<unknown>(`/api/artifacts/${id}/restore`, { method: "POST" });
export const searchArtifacts = (query: string, types: ArtifactType[] = [], limit = 5) =>
  request<ArtifactHit[]>("/api/artifacts/search", { method: "POST", body: { query, types, limit } });
