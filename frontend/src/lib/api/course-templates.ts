/**
 * Uploaded course templates (DOCX or Markdown). Each upload is processed
 * once into a generation blueprint - sections, writing rules, visual rules,
 * worked examples and reusable pictures - that drives courses made from it.
 * Separate from lib/api/templates.ts, which manages the admin JSON templates.
 */

import { apiUrl, request, requestForm } from "./client";

export type CourseTemplateType = "technical" | "non_technical";
export type TemplateVisibility = "private" | "shared";

export interface BlueprintTextRules {
  tone: string;
  reading_level: string;
  depth: string;
  words_per_chapter: number | null;
  paragraph_length: string;
  section_length: string;
  explanation_patterns: string[];
  examples: string[];
  terminology: string[];
  assessments: string[];
  introductions: string[];
  conclusions: string[];
  include: string[];
  avoid: string[];
  other: string[];
  /** Fields inferred from the file's examples rather than stated in it. */
  inferred: string[];
}

export interface BlueprintVisualRule {
  id: string;
  scope: "chapter" | "each_section" | "section";
  section_key: string;
  kind: "any" | "diagram" | "illustration" | "concept_experience" | "chart" | "photo" | "screenshot";
  diagram_kind: string;
  instruction: string;
  required: boolean;
  placement: "anywhere" | "after_text" | "before_text" | "end_of_section";
  labels: string[];
  labelled: boolean;
  caption_required: boolean;
  count: number | null;
  aspect: "" | "landscape" | "portrait" | "square";
  aspect_text: string;
  asset_id: string;
  origin: "stated" | "inferred";
  /** none = the system cannot make it (e.g. screenshots); partial = with limits. */
  support: "full" | "partial" | "none";
  note: string;
}

export interface BlueprintExample {
  id: string;
  section_key: string;
  kind: "text" | "image" | "code" | "table";
  text: string;
  caption: string;
  asset_id: string;
  pattern: string[];
}

export interface BlueprintAsset {
  id: string;
  path: string;
  content_type: string;
  width: number | null;
  height: number | null;
  caption: string;
  context: string;
  section_key: string;
  role: "example" | "reusable";
}

export interface TemplateBlueprint {
  schema_version: number;
  source_format: "docx" | "md" | "json";
  text: BlueprintTextRules;
  visuals: BlueprintVisualRule[];
  section_intro_images: boolean | null;
  examples: BlueprintExample[];
  assets: BlueprintAsset[];
  design: { applied: boolean; reason: string; extracted: Record<string, unknown> };
  warnings: string[];
  ignored: string[];
  problems: string[];
}

export interface BlueprintSection {
  key: string;
  label: string;
  required: boolean;
  guidance: string;
  block_types: string[];
}

/** What the DOCX parser detected, could not reproduce, and did not
 * recognise. Mirrors docx_parser.parser.TemplateParseResult.report(). */
export interface TemplateParseReport {
  parser_version: number | null;
  detected: Record<string, unknown>;
  warnings: string[];
  unsupported_placeholders: string[];
  supported_placeholders: string[];
  sections: { key: string; label: string; required: boolean; block_types: string[] }[];
}

export interface CourseTemplateDocumentSummary {
  id: string;
  name: string;
  template_type: CourseTemplateType;
  description: string;
  source_format: "docx" | "md";
  content_format: "markdown";
  /** Registry id (`uploaded:{uuid}`) to send as template_id_override, or null
   * when the document has no usable parse and cannot drive generation. */
  template_id: string | null;
  parse_report: TemplateParseReport | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  visibility: TemplateVisibility;
  /** The current user may share, unshare or delete it. */
  can_manage: boolean;
  /** False for uploads from before blueprints; `unusable_reason` says why. */
  usable: boolean;
  unusable_reason: string;
  sections: BlueprintSection[];
  blueprint: TemplateBlueprint | null;
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
  visibility: TemplateVisibility;
  applyDesign: boolean;
  file: File;
}): Promise<CourseTemplateDocumentDetail> {
  const form = new FormData();
  form.set("name", input.name);
  form.set("template_type", input.templateType);
  form.set("description", input.description);
  form.set("visibility", input.visibility);
  form.set("apply_design", input.applyDesign ? "true" : "false");
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

export function setCourseTemplateVisibility(
  id: string,
  visibility: TemplateVisibility,
): Promise<CourseTemplateDocumentSummary> {
  return request<CourseTemplateDocumentSummary>(`/api/course-template-documents/${id}`, {
    method: "PATCH",
    body: { visibility },
  });
}

export function deleteCourseTemplateDocument(id: string): Promise<void> {
  return request<void>(`/api/course-template-documents/${id}`, { method: "DELETE" });
}

/** URL of a picture extracted from a DOCX template (auth cookie applies). */
export function courseTemplateAssetUrl(id: string, assetId: string): string {
  return apiUrl(`/api/course-template-documents/${id}/assets/${assetId}`);
}
