/**
 * Mirrors backend/app/schemas/course.py and blueprint.py.
 * Keep these in sync with the Pydantic models.
 */

export type TemplateKind = "technical" | "non_technical";

export const TEMPLATE_IDS: Record<TemplateKind, string> = {
  technical: "technical_v1",
  non_technical: "non_technical_v1",
};

// --- live generation streaming ----------------------------------------------
// Mirrors backend/app/core/generation_stream.py's ChapterStreamState.to_event().

export type ChapterStreamPhase = "writing" | "reviewing" | "research";

export interface ChapterStreamEvent {
  chapter_id: string;
  phase: ChapterStreamPhase;
  text: string;
  done: boolean;
  sequence: number;
}

export interface TocItem {
  title: string;
  sections: string[];
  notes: string;
}

/** Sent as `target_audience` - a short descriptive sentence rather than a
 * bare word, so the planner/writer prompts keep enough context to work
 * with (replaces what used to be a free-text audience description). The
 * first option is this course draft's default (see EMPTY_DRAFT). */
export const LEVEL_OPTIONS: { value: string; label: string }[] = [
  {
    value: "Beginners with little to no prior experience in this subject.",
    label: "Beginner",
  },
  {
    value:
      "Intermediate learners with some practical experience who want to deepen their skills.",
    label: "Intermediate",
  },
  {
    value: "Advanced practitioners seeking expert-level depth and nuance.",
    label: "Advanced",
  },
];

export type AgeGroup = "KG_PRIMARY" | "MIDDLE_SECONDARY" | "HIGHER_ED" | "PROFESSIONAL";
export type EduLevel = "PRIMARY" | "SECONDARY" | "BACHELORS" | "MASTERS_PHD";
/** Only meaningful when age_group === "PROFESSIONAL" - see
 * backend/app/course/pedagogy.py's professional_profile_brief/seniority_brief,
 * which apply the same gating. null = no opinion (no effect on generation). */
export type ProfessionalProfile = "generalist" | "specialist";
export type Seniority = "junior" | "middle" | "senior";

/** Mirrors backend/app/schemas/learner.py - keep the two in sync. */
export interface LearnerProfile {
  age_group: AgeGroup;
  edu_level: EduLevel;
  source_domain: string;
  target_domain: string;
  jargon_density: number;
  scaffolding_depth: number;
  gamification_index: number;
  chunk_word_cap: number;
  manual_override: boolean;
  professional_profile?: ProfessionalProfile | null;
  seniority?: Seniority | null;
}

/** 1 = text only, 2 = text + diagrams/images (the default, unchanged
 * pre-existing behaviour), 3 = provisional experiential-learning framing.
 * See backend/app/agents/prompts.py's content_level_brief and
 * backend/app/schemas/course.py's own field docstring. */
export type ContentLevel = 1 | 2 | 3;

export const CONTENT_LEVEL_LABELS: Record<ContentLevel, string> = {
  1: "Text only",
  2: "Text + diagrams/images",
  3: "Experiential (scenarios & hands-on)",
};

export const CONTENT_LEVEL_HELP: Record<ContentLevel, string> = {
  1: "No generated diagrams, flowcharts or illustrations - every concept explained in text alone.",
  2: "The default. Text plus diagrams, flowcharts and illustrations where they genuinely help.",
  3: "Provisional: in addition to level 2's content, prioritises realistic scenarios, decisions and hands-on practice where the topic supports them.",
};

/** HOW a concept is taught - independent of ContentLevel (which assets
 * accompany it) and LearnerProfile (who it's for). Mirrors backend/app/
 * schemas/course.py's LearningApproach Literal - keep the two in sync. */
export type LearningApproach =
  | "explanatory"
  | "example_driven"
  | "scenario_based"
  | "situation_based"
  | "hands_on"
  | "conversational";

export const LEARNING_APPROACH_OPTIONS: { value: LearningApproach; label: string; help: string }[] = [
  { value: "explanatory", label: "Explanatory", help: "Conceptual understanding - what and why." },
  { value: "example_driven", label: "Example-driven", help: "Real worked examples, step by step." },
  {
    value: "scenario_based",
    label: "Scenario-based",
    help: "Realistic contexts with objectives, constraints and decisions.",
  },
  {
    value: "situation_based",
    label: "Situation-based",
    help: "A specific situation, evaluated across multiple possible responses.",
  },
  {
    value: "hands_on",
    label: "Hands-on",
    help: "Actionable exercises with instructions and evaluation criteria.",
  },
  {
    value: "conversational",
    label: "Conversational",
    help: "Written dialogue / guided Q&A, not live interactive tutoring.",
  },
];

export interface CourseInput {
  course_title: string;
  toc: TocItem[];
  target_audience: string;
  dos: string[];
  donts: string[];
  template: TemplateKind;
  language: string;
  tone: string;
  /** Optional: courses created before the pedagogy layer have no profile. */
  learner_profile?: LearnerProfile | null;
  /** Registry template id. Omitted for the built-in technical/non_technical
   * pair (resolved from `template`); set to `uploaded:{uuid}` when the user
   * picked an uploaded DOCX template. */
  template_id_override?: string | null;
  /** Defaults to 2 server-side when omitted - see ContentLevel above. */
  content_level?: ContentLevel;
  /** Defaults to [] server-side when omitted - see LearningApproach above. */
  learning_approaches?: LearningApproach[];
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

// --- review/approval workflow ------------------------------------------------
// draft -> in_review -> approved
//                     -> changes_requested -> in_review (resubmit) -> ...

export type ReviewStatus = "draft" | "in_review" | "changes_requested" | "approved";

export interface ReviewHistoryEntry {
  action: "submitted" | "approved" | "changes_requested";
  by: string | null;
  at: string;
  comment: string | null;
}

export interface CourseReview {
  course_id: string;
  owner_id: string | null;
  review_status: ReviewStatus;
  review_comment: string | null;
  reviewer_id: string | null;
  reviewed_at: string | null;
  history: ReviewHistoryEntry[];
}

// --- activity log ------------------------------------------------------------

export type ActivityAction =
  | "created"
  | "updated"
  | "submitted_for_review"
  | "changes_requested"
  | "approved"
  | "exported";

export interface CourseActivityEntry {
  id: string;
  action: ActivityAction;
  user_id: string | null;
  user_email: string | null;
  message: string | null;
  created_at: string;
}

export interface CourseActivityListResponse {
  course_id: string;
  activities: CourseActivityEntry[];
}

/** One CourseActivityEntry plus which course it happened in - the
 * workspace-wide feed spans every course the caller owns. */
export interface WorkspaceActivityEntry extends CourseActivityEntry {
  course_id: string;
  document_id: string;
  course_title: string;
}

export interface WorkspaceActivityListResponse {
  activities: WorkspaceActivityEntry[];
}

export interface CourseRecord {
  course_id: string;
  document_id: string;
  run?: RunInfo | null;
  status: CourseStatus;
  input: CourseInput;
  template_id: string;
  owner_id: string | null;
  review_status: ReviewStatus;
  review_comment: string | null;
  reviewer_id: string | null;
  reviewed_at: string | null;
  review_history: ReviewHistoryEntry[];
  created_at: string;
  updated_at: string;
  chapters: ChapterProgress[];
  has_blueprint: boolean;
  has_document: boolean;
  pdf_path: string | null;
  last_error: string | null;
  warnings: string[];
  /** The last time generation finished evaluating this course's quality -
   * see QualitySnapshot below. `null` when the course has never finished
   * generating, or evaluation itself failed that time. Distinct from
   * `status`, which never changes because of a quality finding. */
  quality_snapshot: QualitySnapshot | null;
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
  /** Additive reflection of quality_snapshot.outcome - see CourseRecord's
   * own field for the full explanation. Not used by this app's own
   * generation-progress UI (which always polls GET /api/courses/{id} and
   * reads `course.quality_snapshot` there instead), but present for any
   * caller of the synchronous (`mode: "sync"`) generate endpoint. */
  quality_outcome: "passed" | "failed" | null;
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

/** A source the research stage found for one chapter (web search / Deep
 * Research) - see ResearchAgent in the backend. `note` is an optional
 * one-line reason the model kept it, not always populated. */
export interface ChapterReference {
  title: string;
  url: string;
  note: string;
}

export interface ChapterResearch {
  chapter_id: string;
  chapter_title: string;
  mode: "fast" | "deep" | "mock" | string;
  model: string;
  generated_at: string;
  payload: {
    references: ChapterReference[];
    [key: string]: unknown;
  };
}

// --- chapter detail (drives the generation screen's "what happened" view) ---

export interface ReviewIssue {
  severity: "blocker" | "major" | "minor" | string;
  category: string;
  description: string;
  suggestion: string;
}

export interface ChapterReviewResult {
  approved: boolean;
  scores: Record<string, number | null>;
  issues: ReviewIssue[];
  summary: string;
  reviewed_at: string;
}

export interface GeneratedChapterSummary {
  chapter_id: string;
  chapter_number: number;
  title: string;
  summary: string;
  blocks: unknown[];
  review: ChapterReviewResult | null;
  revisions: number;
  model: string;
  generated_at: string;
}

export interface ChapterDetailResponse {
  chapter: GeneratedChapterSummary;
  research_available: boolean;
}

// --- quality report (MOM Phase 8/9) ------------------------------------------
// Mirrors backend/app/services/quality_framework.py - keep the two in sync.

export type QualityDimension = "accuracy" | "completeness" | "pedagogy" | "visual" | "document";
export type QualitySeverity = "blocker" | "major" | "minor";

export interface QualityFinding {
  artifact: string;
  dimension: QualityDimension;
  severity: QualitySeverity;
  reason: string;
  description: string;
  remediation: string;
  source: string;
}

export interface QualityReport {
  course_id: string;
  document_id: string;
  /** "proposed" until a stakeholder formally signs off on these thresholds -
   * see quality_framework.py's own docstring. Never render this as a claim
   * of certified quality. */
  standards_version: string;
  standards_status: "proposed" | "approved";
  generated_at: string;
  findings: QualityFinding[];
}

/** The audit-trail record from the last time generation finished evaluating
 * this course - distinct from the live `report` above, which always
 * reflects the course's CURRENT state. See QualitySnapshot's own docstring
 * in backend/app/schemas/course.py. */
export interface QualitySnapshot {
  standards_version: string;
  standards_status: "proposed" | "approved";
  evaluated_at: string;
  outcome: "passed" | "failed";
  hard_failure_count: number;
  warning_count: number;
}

/** GET /api/courses/{id}/quality's actual response shape - the live report
 * plus (when one exists) the historical snapshot from the last finished
 * generation, kept visibly distinct per the quality-gate audit's own
 * requirement rather than conflated into one object. */
export interface QualityReportResponse {
  report: QualityReport;
  historical_snapshot: QualitySnapshot | null;
}
