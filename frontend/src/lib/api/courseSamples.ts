import { request } from "./client";

export interface CourseSampleEntry {
  id: string;
  course_id: string;
  title: string;
  topic: string;
  description: string;
  tags: string[];
  approved: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface CourseSampleListResponse {
  items: CourseSampleEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface CourseSampleCreateRequest {
  course_id: string;
  title?: string;
  topic?: string;
  description?: string;
  tags?: string[];
}

export interface CourseSampleUpdateRequest {
  title?: string;
  topic?: string;
  description?: string;
  tags?: string[];
  approved?: boolean;
}

export function listCourseSamples(
  options: { search?: string; approved_only?: boolean; limit?: number; offset?: number } = {},
): Promise<CourseSampleListResponse> {
  return request<CourseSampleListResponse>("/api/course-samples", { query: { ...options } });
}

export function registerCourseSample(payload: CourseSampleCreateRequest): Promise<CourseSampleEntry> {
  return request<CourseSampleEntry>("/api/course-samples", { method: "POST", body: payload });
}

export function updateCourseSample(
  id: string,
  payload: CourseSampleUpdateRequest,
): Promise<CourseSampleEntry> {
  return request<CourseSampleEntry>(`/api/course-samples/${id}`, { method: "PATCH", body: payload });
}

export function deleteCourseSample(id: string): Promise<void> {
  return request<void>(`/api/course-samples/${id}`, { method: "DELETE" });
}
