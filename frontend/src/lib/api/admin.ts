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
