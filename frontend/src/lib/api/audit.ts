import { ApiError, request } from "./client";

export type FindingKind =
  | "invalid_citation"
  | "contradicted"
  | "weak_citation"
  | "unsupported"
  | "assumption";
export type FindingStatus = "open" | "accepted" | "dismissed";

/** Mirrors backend app/schemas/audit.py. */
export interface AuditFinding {
  finding_id: string;
  block_id: string;
  page_number: number | null;
  chapter: string;
  sentence: string;
  /** Where the sentence sits in its block's text, when known. */
  start?: number | null;
  end?: number | null;
  kind: FindingKind;
  severity: "high" | "medium" | "low";
  refs: string[];
  evidence: { ref: string; filename: string; page: number | null; heading: string; excerpt: string }[];
  explanation: string;
  verified_by: "rules" | "ai";
  status: FindingStatus;
}

export interface AuditReport {
  course_id: string;
  generated_at: string;
  source_count: number;
  ai_used: boolean;
  sentences_checked: number;
  findings: AuditFinding[];
  warnings: string[];
  summary: {
    findings: number;
    open: number;
    by_severity: { high: number; medium: number; low: number };
    supported_percent: number;
    cited_percent: number;
  };
}

/** The latest fact check, or null when none has been run yet. */
export async function getAudit(courseId: string): Promise<AuditReport | null> {
  try {
    return await request<AuditReport>(`/api/courses/${courseId}/audit`);
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 404) return null;
    throw caught;
  }
}

export function runAudit(courseId: string): Promise<AuditReport> {
  return request<AuditReport>(`/api/courses/${courseId}/audit`, { method: "POST" });
}

export function setFindingStatus(
  courseId: string,
  findingId: string,
  status: FindingStatus,
): Promise<AuditFinding> {
  return request<AuditFinding>(`/api/courses/${courseId}/audit/findings/${findingId}`, {
    method: "PATCH",
    body: { status },
  });
}
