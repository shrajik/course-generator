"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, RotateCcw, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { StepHeader } from "@/components/ui/StepHeader";
import { ProgressRing } from "./ProgressRing";
import { StageList } from "./StageList";
import { getCourse } from "@/lib/api/courses";
import { generateCourse } from "@/lib/api/generation";
import { ApiError } from "@/lib/api/client";
import { deriveStages, formatEta } from "@/lib/generation/stages";
import { useCourseDraft } from "@/lib/state/course-draft";
import type { CourseDetail } from "@/lib/types/course";

const POLL_INTERVAL_MS = 2000;

export function GenerationProgress({ courseId }: { courseId: string }) {
  const router = useRouter();
  const { update } = useCourseDraft();
  const [detail, setDetail] = useState<CourseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const startedRef = useRef(false);

  const view = useMemo(() => deriveStages(detail), [detail]);

  /** Fired once per course; a reload just resumes polling. */
  const start = useCallback(
    async (force: boolean) => {
      const flag = `course-creator:generating:${courseId}`;
      if (!force && window.sessionStorage.getItem(flag) === "1") return;
      window.sessionStorage.setItem(flag, "1");
      try {
        await generateCourse(courseId, force ? { force: true } : {});
      } catch (caught) {
        setError(
          caught instanceof ApiError
            ? caught.message
            : "Generation could not be completed. See the backend logs for details.",
        );
      }
    },
    [courseId],
  );

  // Poll the real course record for progress.
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const next = await getCourse(courseId, controller.signal);
        if (cancelled) return;
        setDetail(next);
        update({ courseId: next.course.course_id, documentId: next.course.document_id });
        if (!startedRef.current) {
          startedRef.current = true;
          if (next.course.status !== "ready") void start(false);
        }
      } catch (caught) {
        if (cancelled || caught instanceof DOMException) return;
        setError(caught instanceof ApiError ? caught.message : "Lost contact with the backend.");
      }
    };

    void poll();
    const timer = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
    // `update` is stable via useCallback in the provider.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId, start]);

  const documentId = detail?.course.document_id;
  /**
   * The backend saves the document after every chapter, so the editor is usable
   * long before the whole course is finished. Offer it as soon as it exists.
   */
  const canOpenEditor = view.documentReady && Boolean(documentId);

  // Auto-navigate only when the run is actually complete.
  useEffect(() => {
    if (!view.finished || !documentId) return;
    const timer = window.setTimeout(() => router.push(`/editor/${documentId}`), 900);
    return () => window.clearTimeout(timer);
  }, [view.finished, documentId, router]);

  const handleRetry = async () => {
    setRetrying(true);
    setError(null);
    await start(true);
    setRetrying(false);
  };

  return (
    <section className="panel p-6">
      <StepHeader
        step={3}
        title="Generating Your Course"
        subtitle="This may take a few minutes. You can leave this page."
      />

      <div className="mt-8 grid items-start gap-8 md:grid-cols-[minmax(0,1fr)_1px_minmax(0,1.15fr)]">
        <div className="flex flex-col items-center justify-center py-4 text-center">
          <ProgressRing percent={view.percent} />
          <p className="mt-6 text-[13px] font-semibold text-ink">
            {view.currentLabel}
            {view.currentDetailIsTitle ? "—" : ""}
          </p>
          {view.currentDetail ? (
            <p
              className={
                view.currentDetailIsTitle
                  ? "mt-0.5 max-w-[240px] text-[13px] font-semibold text-ink"
                  : "mt-0.5 text-[12px] text-ink-500"
              }
            >
              {view.currentDetail}
            </p>
          ) : null}
          {!view.finished && formatEta(view.etaSeconds) ? (
            <p className="mt-2 text-[11.5px] text-ink-400">{formatEta(view.etaSeconds)}</p>
          ) : null}
        </div>

        <div className="hidden h-full w-px bg-line md:block" aria-hidden />

        <div className="md:pl-2">
          <StageList stages={view.stages} />
        </div>
      </div>

      {error ? (
        <div className="mt-6 flex flex-wrap items-center justify-between gap-3 rounded-card border border-red-200 bg-red-50 px-4 py-3">
          <p className="text-[12.5px] text-red-800">{error}</p>
          <Button variant="outline" size="sm" onClick={handleRetry} disabled={retrying}>
            <RotateCcw size={13} />
            {retrying ? "Retrying…" : "Retry generation"}
          </Button>
        </div>
      ) : (
        <div className="mt-7 flex flex-wrap items-center justify-between gap-3 rounded-card border border-brand-100 bg-brand-50 px-4 py-3.5">
          <div className="flex items-start gap-2.5">
            <Sparkles size={15} className="mt-[1px] shrink-0 text-brand-600" aria-hidden />
            <p className="text-[12.5px] leading-[1.5] text-ink-700">
              {view.finished ? (
                <>
                  Your course is ready.
                  <br />
                  Opening the editor…
                </>
              ) : canOpenEditor ? (
                <>
                  The first chapters are ready to edit.
                  <br />
                  You can start now — the rest will appear as they finish.
                </>
              ) : (
                <>
                  Our AI is researching, writing and designing your course…
                  <br />
                  We&apos;ll notify you when it&apos;s ready!
                </>
              )}
            </p>
          </div>
          {canOpenEditor && documentId ? (
            <Button size="sm" onClick={() => router.push(`/editor/${documentId}`)}>
              {view.finished ? "Open editor" : "Start editing"}
              <ArrowRight size={13} />
            </Button>
          ) : null}
        </div>
      )}

      {detail?.course.warnings.length ? (
        <ul className="mt-3 space-y-1">
          {detail.course.warnings.map((warning, index) => (
            <li key={index} className="text-[11.5px] text-amber-700">
              {warning}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
