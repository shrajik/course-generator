import { request } from "./client";
import type {
  CourseBlueprint,
  CourseDetail,
  CourseInput,
  CourseRecord,
  ImproveTocRequest,
  ImproveTocResponse,
} from "@/lib/types/course";

export function createCourse(
  input: CourseInput,
  options: { runPlanner?: boolean } = {},
): Promise<CourseRecord> {
  return request<CourseRecord>("/api/courses", {
    method: "POST",
    body: { ...input, run_planner: options.runPlanner ?? true },
  });
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
