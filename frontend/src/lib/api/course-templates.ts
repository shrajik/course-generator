/**
 * Uploaded course template documents (DOCX/Markdown, normalized to
 * Markdown server-side). Separate from lib/api/templates.ts, which manages
 * the admin JSON pipeline-schema templates - a different feature that
 * happens to share the word "template".
 */

import { request, requestForm } from "./client";

export type CourseTemplateType = "technical" | "non_technical";

export interface CourseTemplateDocumentSummary {
  id: string;
  name: string;
  template_type: CourseTemplateType;
  description: string;
  source_format: "docx" | "md";
  content_format: "markdown";
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface CourseTemplateDocumentDetail extends CourseTemplateDocumentSummary {
  markdown_content: string;
}

export async function listCourseTemplateDocuments(): Promise<CourseTemplateDocumentSummary[]> {
  const response = await request<{ templates: CourseTemplateDocumentSummary[] }>(
    "/api/course-template-documents",
  );
  return response.templates;
}

export async function getCourseTemplateDocument(id: string): Promise<CourseTemplateDocumentDetail> {
  return request<CourseTemplateDocumentDetail>(`/api/course-template-documents/${id}`);
}

export async function uploadCourseTemplateDocument(input: {
  name: string;
  templateType: CourseTemplateType;
  description: string;
  file: File;
}): Promise<CourseTemplateDocumentDetail> {
  const form = new FormData();
  form.set("name", input.name);
  form.set("template_type", input.templateType);
  form.set("description", input.description);
  form.set("file", input.file);
  return requestForm<CourseTemplateDocumentDetail>("/api/course-template-documents", form);
}

export interface TemplateTypeClassification {
  template_type: CourseTemplateType;
  reasoning: string;
}

/** Backs the hardcoded "Default Template" option: given the course title/
 * audience entered so far, asks the backend to decide Technical vs
 * Non-Technical rather than making the user pick. */
export function classifyTemplateType(input: {
  courseTitle: string;
  targetAudience: string;
}): Promise<TemplateTypeClassification> {
  return request<TemplateTypeClassification>("/api/course-template-documents/classify-type", {
    method: "POST",
    body: { course_title: input.courseTitle, target_audience: input.targetAudience },
  });
}
