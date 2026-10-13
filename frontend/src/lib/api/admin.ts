import { request } from "./client";
import type { Role } from "@/lib/types/auth";

export interface AdminHealthResponse {
  status: string;
}

export interface AdminDashboardResponse {
  total_users: number;
  total_courses: number;
  system_status: string;
}

export interface AdminUser {
  id: string;
  email: string;
  role: Role;
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminUserListResponse {
  users: AdminUser[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminUserListOptions {
  search?: string;
  role?: Role;
  is_active?: boolean;
  is_verified?: boolean;
  limit?: number;
  offset?: number;
}

export interface AdminCourse {
  id: string;
  course_id: string;
  document_id: string;
  title: string;
  status: string;
  template_id: string;
  owner: string | null;
  target_audience: string | null;
  language: string | null;
  tone: string | null;
  toc_count: number;
  chapters_count: number;
  has_blueprint: boolean;
  has_document: boolean;
  last_error: string | null;
  warnings: string[];
  /** Additive reflection of the course's quality_snapshot.outcome - see
   * CourseRecord in lib/types/course.ts. `null` when no snapshot exists
   * yet. Distinct from `status`, which never changes for a quality finding. */
  quality_outcome: "passed" | "failed" | null;
  run_state: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface AdminCourseListResponse {
  courses: AdminCourse[];
  total: number;
  limit: number;
  offset: number;
  statuses: string[];
}

export interface AdminCourseListOptions {
  search?: string;
  status?: string;
  owner?: string;
  limit?: number;
  offset?: number;
}

export function getAdminHealth(): Promise<AdminHealthResponse> {
  return request<AdminHealthResponse>("/admin/health");
}

export function getAdminDashboard(): Promise<AdminDashboardResponse> {
  return request<AdminDashboardResponse>("/admin/dashboard");
}

export function listAdminUsers(
  options: AdminUserListOptions = {},
): Promise<AdminUserListResponse> {
  const query: Record<string, string | number | boolean | undefined> = { ...options };
  return request<AdminUserListResponse>("/admin/users", { query });
}

export function getAdminUser(userId: string): Promise<AdminUser> {
  return request<AdminUser>(`/admin/users/${userId}`);
}

export function updateAdminUserRole(userId: string, role: Role): Promise<AdminUser> {
  return request<AdminUser>(`/admin/users/${userId}/role`, {
    method: "PATCH",
    body: { role },
  });
}

export function updateAdminUserStatus(userId: string, isActive: boolean): Promise<AdminUser> {
  return request<AdminUser>(`/admin/users/${userId}/status`, {
    method: "PATCH",
    body: { is_active: isActive },
  });
}

export function listAdminCourses(
  options: AdminCourseListOptions = {},
): Promise<AdminCourseListResponse> {
  const query: Record<string, string | number | boolean | undefined> = { ...options };
  return request<AdminCourseListResponse>("/admin/courses", { query });
}

export function getAdminCourse(courseId: string): Promise<AdminCourse> {
  return request<AdminCourse>(`/admin/courses/${courseId}`);
}

/** One real AI call made while generating a course - mirrors
 * backend/app/schemas/admin.py's ModelUsageRecord exactly (MOM Phase 2.2 -
 * AI Model Transparency). No cost field exists on the backend - tokens are
 * captured, never converted to a dollar figure, so none is shown here. */
export interface AdminModelUsageRecord {
  kind: string;
  purpose: string;
  model: string;
  provider: string;
  seconds: number;
  input_tokens: number;
  output_tokens: number;
  retries: number;
  failed: boolean;
  used_fallback: boolean;
  timestamp: string;
}

/** Mirrors AdminModelUsageResponse. `job_id` is null when this course has
 * never had a recorded generation run at all - distinct from a run that
 * made zero real AI calls, where `calls` is `[]` but `job_id` is set. */
export interface AdminModelUsageResponse {
  course_id: string;
  job_id: string | null;
  run_state: string | null;
  run_created_at: string | null;
  calls: AdminModelUsageRecord[];
}

/** Takes the course's own `course_id` (e.g. "crs_abc123"), NOT its database
 * `id` (UUID) - this one admin endpoint's path parameter is the string
 * course_id, unlike GET /admin/courses/{id} which takes the UUID. Passing
 * the UUID here silently 404s instead of erroring loudly, so this is easy
 * to get wrong - see backend/app/api/admin.py's admin_course_model_usage. */
export function getAdminCourseModelUsage(courseId: string): Promise<AdminModelUsageResponse> {
  return request<AdminModelUsageResponse>(`/admin/courses/${courseId}/model-usage`);
}
