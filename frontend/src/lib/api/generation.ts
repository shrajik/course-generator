import { request } from "./client";
import type { GenerateRequest, GenerateResponse, RunInfo } from "@/lib/types/course";

/**
 * Generation runs in the background: this resolves as soon as the backend has
 * accepted the job, and progress is read from the course record while it works.
 * `mode: "sync"` is still available for scripts that want to block.
 */
export function generateCourse(
  courseId: string,
  payload: GenerateRequest = {},
): Promise<GenerateResponse> {
  return request<GenerateResponse>(`/api/courses/${courseId}/generate`, {
    method: "POST",
    body: { mode: "background", ...payload },
  });
}

export function getRunState(
  courseId: string,
  signal?: AbortSignal,
): Promise<{ course_id: string; run: RunInfo | null }> {
  return request(`/api/courses/${courseId}/run`, { signal });
}

/**
 * Stop a running/queued generation. Already-completed chapters, images and
 * the document are left exactly as they are. Safe to call even if nothing
 * is actually running. Resume later with `generateCourse(courseId, { resume: true })`.
 */
export function cancelGeneration(
  courseId: string,
): Promise<{ course_id: string; cancelled: boolean }> {
  return request(`/api/courses/${courseId}/cancel`, { method: "POST" });
}
