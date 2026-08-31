/**
 * Mirrors backend/app/schemas/course.py and blueprint.py.
 * Keep these in sync with the Pydantic models.
 */

export type TemplateKind = "technical" | "non_technical";

export const TEMPLATE_IDS: Record<TemplateKind, string> = {
  technical: "technical_v1",
  non_technical: "non_technical_v1",
};

export interface TocItem {
  title: string;
  sections: string[];
  notes: string;
}

export interface CourseInput {
  course_title: string;
  toc: TocItem[];
  target_audience: string;
  dos: string[];
  donts: string[];
  template: TemplateKind;
  language: string;
  tone: string;
}

export interface CreateCourseRequest extends CourseInput {
  run_planner: boolean;
}

export type CourseStatus =
  | "created"
  | "planned"
  | "researching"
  | "writing"
  | "reviewing"
  | "assembling"
  | "illustrating"
  | "ready"
  | "failed";

export interface ChapterProgress {
  chapter_id: string;
  title: string;
  researched: boolean;
  written: boolean;
  reviewed: boolean;
  review_score: number | null;
  error: string | null;
}

export type RunState = "queued" | "running" | "done" | "failed" | "cancelled";

/** Live state of the current or last generation run, persisted by the backend. */
export interface RunInfo {
  job_id: string;
  state: RunState;
  mode: "background" | "sync";
  started_at: string;
  updated_at: string;
  finished_at: string;
  chapters_total: number;
  chapters_done: number;
  writing_mode: string;
  eta_seconds: number | null;
  error: string | null;
  timings: Record<string, unknown>;
}

export interface CourseRecord {
  course_id: string;
  document_id: string;
  run?: RunInfo | null;
  status: CourseStatus;
  input: CourseInput;
  template_id: string;
  created_at: string;
  updated_at: string;
  chapters: ChapterProgress[];
  has_blueprint: boolean;
  has_document: boolean;
  pdf_path: string | null;
  last_error: string | null;
  warnings: string[];
}

// --- TOC improvement --------------------------------------------------------

export interface ImproveTocRequest {
  course_title: string;
  toc: TocItem[];
  audience: string;
  template: TemplateKind;
  dos: string[];
  donts: string[];
}

export interface TocChange {
  action: string;
  target: string;
  proposed: string;
  reason: string;
}

export interface ImproveTocResponse {
  suggested_toc: TocItem[];
  changes: TocChange[];
  reasoning: string;
  missing_concepts: string[];
  duplicate_topics: string[];
}

// --- blueprint --------------------------------------------------------------

export interface ChapterSection {
  title: string;
  summary: string;
  key_points: string[];
}

export interface BlueprintChapter {
  id: string;
  title: string;
  order: number;
  objective: string;
  summary: string;
  sections: ChapterSection[];
  required_blocks: string[];
  optional_blocks: string[];
  key_concepts: string[];
  prerequisites: string[];
  research_questions: string[];
  estimated_words: number;
  difficulty: string;
}

export interface BlueprintCritique {
  missing_concepts: string[];
  duplicate_topics: string[];
  ordering_issues: string[];
  learning_progression_notes: string;
  suggested_toc: string[];
}

export interface CourseBlueprint {
  course_title: string;
  audience: string;
  template_id: string;
  course_summary: string;
  learning_objectives: string[];
  prerequisites: string[];
  tone: string;
  dos: string[];
  donts: string[];
  chapters: BlueprintChapter[];
  critique: BlueprintCritique;
  generated_at: string;
}

// --- generation -------------------------------------------------------------

export interface GenerateRequest {
  chapter_ids?: string[] | null;
  force?: boolean;
  skip_research?: boolean;
  generate_images?: boolean | null;
  build_document?: boolean;
  /** background returns a job id immediately; sync blocks until the run ends. */
  mode?: "background" | "sync";
  resume?: boolean;
  deep_research_chapter_ids?: string[] | null;
}

export interface GenerateResponse {
  course_id: string;
  document_id: string;
  status: CourseStatus;
  chapters_generated: string[];
  chapters_failed: string[];
  images_generated: number;
  pages: number;
  warnings: string[];
  duration_seconds: number;
  mode: "background" | "sync";
  job_id: string;
  accepted: boolean;
  timings: Record<string, unknown>;
}

export interface CourseDetail {
  course: CourseRecord;
  blueprint?: CourseBlueprint;
  artifacts: {
    blueprint: boolean;
    document: boolean;
    chapters: Array<{
      chapter_id: string;
      chapter_number: number;
      title: string;
      blocks: number;
      revisions: number;
      reviewed: boolean;
    }>;
    research: string[];
    pdf: string | null;
  };
}
