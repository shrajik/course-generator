import { request } from "./client";
import type {
  CourseActivityListResponse,
  CourseBlueprint,
  CourseDetail,
  CourseInput,
  CourseRecord,
  CourseReview,
  ImproveTocRequest,
  ImproveTocResponse,
} from "@/lib/types/course";

export interface CourseListItem {
  course_id: string;
  document_id: string;
  course_title: string;
  template_id: string;
  status: string;
  review_status: string;
  updated_at: string;
}

export interface CourseListResponse {
  courses: CourseListItem[];
  count: number;
}

export interface TemplateSummary {
  template_id: string;
  kind: string;
  name: string;
  description: string;
  sections: { key: string; label: string; required: boolean }[];
  required_block_types: string[];
}

export function createCourse(
  input: CourseInput,
  options: { runPlanner?: boolean } = {},
): Promise<CourseRecord> {
  return request<CourseRecord>("/api/courses", {
    method: "POST",
    body: { ...input, run_planner: options.runPlanner ?? true },
  });
}

export function listCourses(): Promise<CourseListResponse> {
  return request<CourseListResponse>("/api/courses");
}

export function listTemplates(): Promise<{ templates: TemplateSummary[] }> {
  return request<{ templates: TemplateSummary[] }>("/api/courses/templates");
}

export function improveToc(payload: ImproveTocRequest): Promise<ImproveTocResponse> {
  return request<ImproveTocResponse>("/api/courses/improve-toc", {
    method: "POST",
    body: payload,
  });
}

export function getCourse(courseId: string, signal?: AbortSignal): Promise<CourseDetail> {
  return request<CourseDetail>(`/api/courses/${courseId}`, { signal });
}

export function getBlueprint(courseId: string): Promise<CourseBlueprint> {
  return request<CourseBlueprint>(`/api/courses/${courseId}/blueprint`);
}

// --- review/approval workflow ------------------------------------------------

export function getCourseReview(courseId: string): Promise<CourseReview> {
  return request<CourseReview>(`/api/courses/${courseId}/review`);
}

export function submitForReview(courseId: string): Promise<CourseReview> {
  return request<CourseReview>(`/api/courses/${courseId}/submit-for-review`, {
    method: "POST",
  });
}

export function approveCourse(courseId: string, comment?: string): Promise<CourseReview> {
  return request<CourseReview>(`/api/courses/${courseId}/approve`, {
    method: "POST",
    body: comment ? { comment } : undefined,
  });
}

export function requestCourseChanges(courseId: string, comment: string): Promise<CourseReview> {
  return request<CourseReview>(`/api/courses/${courseId}/request-changes`, {
    method: "POST",
    body: { comment },
  });
}

// --- activity log --------------------------------------------------------

export function getCourseActivity(
  courseId: string,
  limit = 20,
): Promise<CourseActivityListResponse> {
  return request<CourseActivityListResponse>(`/api/courses/${courseId}/activity`, {
    query: { limit },
  });
}
