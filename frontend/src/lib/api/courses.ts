import { request } from "./client";
import type {
  ChapterDetailResponse,
  ChapterResearch,
  CourseActivityListResponse,
  CourseBlueprint,
  CourseDetail,
  CourseInput,
  CourseRecord,
  CourseReview,
  ImproveTocRequest,
  ImproveTocResponse,
  WorkspaceActivityListResponse,
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

/** The web-search/Deep Research findings (including sources) for one
 * chapter - see ResearchAgent. Throws a 404 ApiError if the chapter has no
 * cached research (e.g. offline/mock mode). */
export function getChapterResearch(courseId: string, chapterId: string): Promise<ChapterResearch> {
  return request<ChapterResearch>(`/api/courses/${courseId}/chapters/${chapterId}/research`);
}

/** The drafted chapter (title/summary/block count) plus its reviewer verdict,
 * if reviewed yet - what the "Writing Chapter N" / "Reviewing Content" stages
 * on the generation screen drill into. */
export function getChapter(courseId: string, chapterId: string): Promise<ChapterDetailResponse> {
  return request<ChapterDetailResponse>(`/api/courses/${courseId}/chapters/${chapterId}`);
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

/** Every activity entry across every course the caller owns - the
 * workspace-wide feed behind the sidebar's "History" page. */
export function getWorkspaceActivity(limit = 50): Promise<WorkspaceActivityListResponse> {
  return request<WorkspaceActivityListResponse>("/api/activity", {
    query: { limit },
  });
}
