"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import { approvePhase, getCourse } from "@/lib/api/courses";
import { useEditor } from "@/lib/editor/store";
import type { CoursePhase, CoursePhases, PhaseStatus } from "@/lib/types/course";

/**
 * Content Studio Phase 3/5 - ADDIE phase-gated approval. Renders nothing for
 * any course without active phase-gating (no design model, SAM, Bloom/
 * Kirkpatrick alone, or any course from before this feature - `phases` is
 * null in every one of those cases).
 */

const STATUS_CLASSES: Record<PhaseStatus, string> = {
  pending: "border-line bg-canvas text-ink-500",
  in_review: "border-blue-200 bg-blue-50 text-blue-800",
  approved: "border-emerald-200 bg-emerald-50 text-emerald-800",
};

const PHASE_LABELS: Record<CoursePhase, string> = {
  analysis_design: "Analysis & Design",
  development: "Development",
  implementation_evaluation: "Implementation & Evaluation",
};

const PHASE_ORDER: CoursePhase[] = ["analysis_design", "development", "implementation_evaluation"];

export function PhaseApprovalButton({ courseId: overrideCourseId }: { courseId?: string } = {}) {
  const editor = useEditor();
  const courseId = overrideCourseId ?? editor.document?.course_id ?? null;
  const [phases, setPhases] = useState<CoursePhases | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!courseId) return;
    let cancelled = false;
    getCourse(courseId)
      .then((detail) => {
        if (cancelled) return;
        setPhases(detail.course.phases ?? null);
      })
      .catch(() => setPhases(null));
    return () => {
      cancelled = true;
    };
  }, [courseId]);

  if (!courseId || !phases) return null;

  // analysis_design already has its own approval action (approve-outline,
  // wired elsewhere) - never a duplicate button for it here.
  const actionable = PHASE_ORDER.find(
    (phase) => phase !== "analysis_design" && phases[phase].status === "in_review",
  );

  const approve = async (phase: CoursePhase) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const record = await approvePhase(courseId, phase);
      setPhases(record.phases ?? null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The phase could not be approved.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex shrink-0 items-center gap-1.5">
      {PHASE_ORDER.map((phase) => (
        <span
          key={phase}
          className={`inline-flex h-7 shrink-0 items-center whitespace-nowrap rounded-[8px] border px-2 text-[11.5px] font-medium ${STATUS_CLASSES[phases[phase].status]}`}
        >
          {PHASE_LABELS[phase]}
        </span>
      ))}
      {actionable ? (
        <Button size="sm" onClick={() => void approve(actionable)} disabled={busy}>
          {busy ? <Loader2 size={13} className="animate-spin" /> : null}
          Approve {PHASE_LABELS[actionable]}
        </Button>
      ) : null}
      {error ? <span className="text-[12px] text-red-700">{error}</span> : null}
    </div>
  );
}
