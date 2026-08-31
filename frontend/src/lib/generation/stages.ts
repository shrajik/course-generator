/**
 * Derives the generation stage list from the real backend course record.
 *
 * The backend POC persists genuine progress while it works: `course.status`, the
 * per-chapter `researched/written/reviewed` flags and the list of research files
 * already written to disk. Nothing here is simulated. If the backend later gains
 * a richer progress endpoint, only `deriveStages` needs to change.
 */

import type { CourseDetail, CourseStatus } from "@/lib/types/course";

export type StageStatus = "completed" | "active" | "pending" | "failed";

export interface GenerationStage {
  id: string;
  label: string;
  status: StageStatus;
  detail?: string;
}

export interface GenerationView {
  stages: GenerationStage[];
  percent: number;
  currentLabel: string;
  currentDetail: string;
  /** True when `currentDetail` is a chapter title rather than an "n/m" counter. */
  currentDetailIsTitle: boolean;
  finished: boolean;
  failed: boolean;
  /** Seconds remaining, measured by the backend from this run's own pace. */
  etaSeconds: number | null;
  /** True once the document exists - the editor can be opened from here. */
  documentReady: boolean;
}

export function formatEta(seconds: number | null): string | null {
  if (seconds === null || seconds <= 0) return null;
  if (seconds < 60) return "less than a minute left";
  const minutes = Math.round(seconds / 60);
  return `about ${minutes} minute${minutes === 1 ? "" : "s"} left`;
}

const WEIGHTS = {
  planning: 8,
  research: 27,
  writing: 35,
  reviewing: 15,
  images: 10,
  document: 5,
} as const;

const WRITING_GROUP_SIZE = 3;

const IN_FLIGHT: CourseStatus[] = [
  "researching",
  "writing",
  "reviewing",
  "assembling",
  "illustrating",
];

function statusLabel(status: StageStatus): string {
  switch (status) {
    case "completed":
      return "Completed";
    case "active":
      return "In Progress";
    case "failed":
      return "Failed";
    default:
      return "Pending";
  }
}

export function stageStatusLabel(stage: GenerationStage): string {
  return statusLabel(stage.status);
}

function ratioStage(
  id: string,
  label: string,
  done: number,
  total: number,
  isActive: boolean,
): { stage: GenerationStage; fraction: number } {
  const fraction = total > 0 ? Math.min(done / total, 1) : 0;
  let status: StageStatus = "pending";
  if (fraction >= 1) status = "completed";
  else if (isActive || done > 0) status = "active";
  return {
    stage: {
      id,
      label,
      status,
      detail: total > 0 && status !== "pending" ? `${done}/${total} chapters` : undefined,
    },
    fraction,
  };
}

export function deriveStages(detail: CourseDetail | null): GenerationView {
  if (!detail) {
    return {
      stages: [
        { id: "planning", label: "Course Planning", status: "active" },
        { id: "research", label: "Deep Research", status: "pending" },
        { id: "writing", label: "Writing Chapters", status: "pending" },
        { id: "reviewing", label: "Reviewing Content", status: "pending" },
        { id: "images", label: "Generating Images", status: "pending" },
        { id: "document", label: "Building Document", status: "pending" },
      ],
      percent: 0,
      currentLabel: "Starting generation",
      currentDetail: "",
      currentDetailIsTitle: false,
      finished: false,
      failed: false,
      etaSeconds: null,
      documentReady: false,
    };
  }

  const { course, artifacts } = detail;
  const status = course.status;
  const chapters = course.chapters;
  const total = chapters.length;
  const failed = status === "failed" || course.run?.state === "failed";
  const running = IN_FLIGHT.includes(status) || course.run?.state === "running";
  const hasDocument = artifacts.document || course.has_document;
  /**
   * `status === "ready"` only means the last generate call returned - a partial
   * run (regenerating one chapter) reports it too. A course is complete when the
   * run finished, every chapter is written and the document exists.
   */
  const allWritten = total > 0 && chapters.every((chapter) => chapter.written);
  const ready = status === "ready" && allWritten && hasDocument;

  // --- planning -----------------------------------------------------------
  const planned = artifacts.blueprint || course.has_blueprint;
  const planning: GenerationStage = {
    id: "planning",
    label: "Course Planning",
    status: planned ? "completed" : running || status === "created" ? "active" : "pending",
    detail: planned && total ? `${total} chapters planned` : undefined,
  };
  const planningFraction = planned ? 1 : 0;

  // --- research (research/chapter_XX.json files appear as they land) -------
  const researched = Math.max(
    artifacts.research.length,
    chapters.filter((chapter) => chapter.researched).length,
  );
  const research = ratioStage(
    "research",
    "Deep Research",
    researched,
    total,
    status === "researching",
  );

  // --- writing, split into the chapter groups shown in the design ----------
  const writtenCount = chapters.filter((chapter) => chapter.written).length;
  const writingStages: GenerationStage[] = [];
  if (total === 0) {
    writingStages.push({ id: "writing", label: "Writing Chapters", status: "pending" });
  } else {
    for (let start = 0; start < total; start += WRITING_GROUP_SIZE) {
      const group = chapters.slice(start, start + WRITING_GROUP_SIZE);
      const first = start + 1;
      const last = start + group.length;
      const groupWritten = group.filter((chapter) => chapter.written).length;
      const groupFailed = group.some((chapter) => chapter.error);
      let stageStatus: StageStatus = "pending";
      if (groupWritten === group.length) stageStatus = "completed";
      else if (groupFailed) stageStatus = "failed";
      else if (groupWritten > 0 || writtenCount === start) {
        stageStatus = status === "writing" || status === "reviewing" ? "active" : "pending";
      }
      writingStages.push({
        id: `writing-${first}`,
        label: first === last ? `Writing Chapter ${first}` : `Writing Chapter ${first}-${last}`,
        status: stageStatus,
      });
    }
  }
  const writingFraction = total > 0 ? Math.min(writtenCount / total, 1) : 0;

  // --- review -------------------------------------------------------------
  const reviewedCount = chapters.filter((chapter) => chapter.reviewed).length;
  const reviewing = ratioStage(
    "reviewing",
    "Reviewing Content",
    reviewedCount,
    total,
    status === "reviewing" || (status === "writing" && reviewedCount > 0),
  );

  // --- images + document --------------------------------------------------
  const images: GenerationStage = {
    id: "images",
    label: "Generating Images",
    status: ready
      ? "completed"
      : status === "illustrating"
        ? "active"
        : failed
          ? "failed"
          : "pending",
  };
  const document: GenerationStage = {
    id: "document",
    label: "Building Document",
    status: ready
      ? "completed"
      : status === "assembling" || status === "illustrating"
        ? "active"
        : failed
          ? "failed"
          : "pending",
  };

  const stages = [
    planning,
    research.stage,
    ...writingStages,
    reviewing.stage,
    images,
    document,
  ];

  const percent = ready
    ? 100
    : Math.min(
        99,
        Math.round(
          planningFraction * WEIGHTS.planning +
            research.fraction * WEIGHTS.research +
            writingFraction * WEIGHTS.writing +
            (total > 0 ? Math.min(reviewedCount / total, 1) : 0) * WEIGHTS.reviewing +
            (images.status === "completed" ? WEIGHTS.images : 0) +
            (document.status === "completed" ? WEIGHTS.document : 0),
        ),
      );

  const active = stages.find((stage) => stage.status === "active");
  const writingChapter = chapters.find((chapter) => !chapter.written);

  let currentLabel = "Preparing";
  let currentDetail = "";
  let currentDetailIsTitle = false;
  if (ready) {
    currentLabel = "Course ready";
  } else if (failed) {
    currentLabel = "Generation failed";
    currentDetail = course.run?.error ?? course.last_error ?? "";
  } else if (active) {
    currentLabel = active.label;
    if (active.id.startsWith("writing") && writingChapter) {
      currentDetail = writingChapter.title;
      currentDetailIsTitle = true;
    } else if (active.detail) {
      currentDetail = active.detail;
    }
  }

  return {
    stages,
    percent,
    currentLabel,
    currentDetail,
    currentDetailIsTitle,
    finished: ready,
    failed,
    etaSeconds: ready ? 0 : (course.run?.eta_seconds ?? null),
    documentReady: hasDocument,
  };
}
