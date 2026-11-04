/**
 * What the artifact memory screens decide, free of React so it can be tested: how an artifact's
 * review state reads, what a reviewer may do with a version, and where an artifact came from.
 */

import type { ArtifactDetail, ArtifactSummary, ArtifactType, ArtifactVersion, ReviewStatus } from "../api/artifacts.ts";

export type Tone = "neutral" | "success" | "warning" | "danger";

const TYPES: Record<ArtifactType, string> = {
  chapter: "Chapter",
  visual: "Visual",
  questions: "Question set",
  assessment: "Assessment",
  activity: "Activity",
  case_study: "Case study",
  other: "Other",
};

export const typeLabel = (type: ArtifactType): string => TYPES[type] ?? "Other";

export function reviewBadge(status: ReviewStatus): { label: string; tone: Tone } {
  if (status === "approved") return { label: "Approved", tone: "success" };
  if (status === "changes_requested") return { label: "Changes requested", tone: "danger" };
  return { label: "Waiting for review", tone: "warning" };
}

/** The citation a writer uses for a version: artifact number and version number. */
export const citation = (artifact: Pick<ArtifactSummary, "number">, version: Pick<ArtifactVersion, "version_no">): string =>
  `A${artifact.number}.${version.version_no}`;

/** What a reviewer can do with a version. Only a version waiting for review can be decided, and only the
 * newest one: an older one has been superseded by what the author sent since. */
export function reviewActions(artifact: ArtifactDetail, version: ArtifactVersion): { canDecide: boolean; reason: string | null } {
  if (artifact.status === "archived") return { canDecide: false, reason: "This artifact is archived." };
  if (version.review_status === "approved") return { canDecide: false, reason: "Approved versions are locked." };
  if (version.review_status === "changes_requested") return { canDecide: false, reason: "Sent back to the author." };
  if (version.id !== artifact.latest_version_id) return { canDecide: false, reason: "A newer version exists." };
  return { canDecide: true, reason: null };
}

/** Whether future courses are offered this version: approved, the current one, and not archived. */
export const isOffered = (artifact: Pick<ArtifactSummary, "status">, version: Pick<ArtifactVersion, "is_active">): boolean =>
  artifact.status === "active" && version.is_active;

/** Where an artifact came from, for the line under its title. */
export function provenance(artifact: Pick<ArtifactSummary, "course_title" | "course_id">, version?: Pick<ArtifactVersion, "generation_run_id" | "source_refs">): string {
  const parts = [artifact.course_title || artifact.course_id || "No course"];
  if (version?.generation_run_id) parts.push(`run ${version.generation_run_id}`);
  if (version && version.source_refs.length > 0) parts.push(`from ${version.source_refs.join(", ")}`);
  return parts.join(" · ");
}

/** Versions as the reviewer reads them: newest first, with the one future courses are offered marked. */
export function versionRows(artifact: ArtifactDetail) {
  return [...artifact.versions]
    .sort((a, b) => b.version_no - a.version_no)
    .map((version) => ({ version, offered: isOffered(artifact, version), ref: citation(artifact, version), ...reviewActions(artifact, version) }));
}
