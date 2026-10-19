import { request } from "./client";

/** The Review Assistant's summary (backend app/course/review_brief.py). */
export interface ReviewBrief {
  headline: string;
  ready_to_approve: boolean;
  blockers: string[];
  advisories: string[];
  chapters: { chapter: string; high: number; medium: number; low: number }[];
  stats: {
    sentences_checked: number;
    open_findings: number;
    qa_pass: number;
    qa_fail: number;
    qa_review: number;
    fact_checked: boolean;
  };
}

export interface CourseSourceInfo {
  number: number;
  prefix: string;
  source_id: string;
  filename: string;
  kind: string;
  page_count: number | null;
  chunk_count: number;
  status: string;
}

export interface Passage {
  ref: string;
  source_id: string;
  filename: string;
  heading: string;
  page: number | null;
  text: string;
}

export function getReviewBrief(courseId: string): Promise<ReviewBrief> {
  return request<ReviewBrief>(`/api/courses/${courseId}/review-brief`);
}

export function getCourseSources(courseId: string): Promise<CourseSourceInfo[]> {
  return request<CourseSourceInfo[]>(`/api/courses/${courseId}/sources`);
}

export function getSourcePassages(
  courseId: string,
  number: number,
  offset = 0,
  limit = 40,
): Promise<Passage[]> {
  return request<Passage[]>(`/api/courses/${courseId}/sources/${number}/passages`, {
    query: { offset, limit },
  });
}

export function getPassagesByRefs(courseId: string, refs: string[]): Promise<Passage[]> {
  return request<Passage[]>(`/api/courses/${courseId}/passages`, { query: { refs: refs.join(",") } });
}
