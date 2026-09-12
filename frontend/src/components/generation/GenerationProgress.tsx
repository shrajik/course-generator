"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, RotateCcw, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { StepHeader } from "@/components/ui/StepHeader";
import { ProgressRing } from "./ProgressRing";
import { StageList } from "./StageList";
import { LiveDocumentPreview } from "./LiveDocumentPreview";
import { getCourse } from "@/lib/api/courses";
import { generateCourse } from "@/lib/api/generation";
import { getCourseDocument } from "@/lib/api/documents";
import { apiUrl, ApiError } from "@/lib/api/client";
import { deriveStages, emptyStateMessage, formatEta } from "@/lib/generation/stages";
import { useCourseDraft } from "@/lib/state/course-draft";
import type { ChapterStreamEvent, CourseDetail } from "@/lib/types/course";
import type { CourseDocument } from "@/lib/types/document";

const POLL_INTERVAL_MS = 2000;

/** What the live-preview panel shows for one chapter while it's mid-stream.
 * `text` intentionally never goes back to empty once it has real content -
 * the reviewer's own critique call has no prose to stream (see
 * course_service.py), so without this the panel would flash blank the
 * instant a chapter finishes writing and review begins. */
export interface ChapterStreamView {
  phase: ChapterStreamEvent["phase"];
  text: string;
  done: boolean;
}

export function GenerationProgress({ courseId }: { courseId: string }) {
  const router = useRouter();
  const { update } = useCourseDraft();
  const [detail, setDetail] = useState<CourseDetail | null>(null);
  const [liveDocument, setLiveDocument] = useState<CourseDocument | null>(null);
  const [streamingChapters, setStreamingChapters] = useState<Record<string, ChapterStreamView>>(
    {},
  );
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const startedRef = useRef(false);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const pollRef = useRef<() => Promise<void>>(async () => {});

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

    const stopPolling = () => {
      if (pollTimerRef.current !== undefined) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = undefined;
      }
    };

    const poll = async () => {
      let next: CourseDetail;
      try {
        next = await getCourse(courseId, controller.signal);
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
        return;
      }

      // The backend saves the document after every chapter, so it exists long
      // before the run finishes - but only fetch it once the course record
      // actually reports one instead of hammering an endpoint that's expected
      // to 404 on every tick during research/before the first chapter lands.
      const hasDocument = next.artifacts.document || next.course.has_document;
      if (hasDocument) {
        try {
          const nextDocument = await getCourseDocument(courseId);
          if (!cancelled) setLiveDocument(nextDocument);
        } catch {
          // transient - keep whatever we last had.
        }
      }

      // Nothing more will change once the run has reached a terminal state -
      // stop polling until the user explicitly retries (see handleRetry).
      const failed = next.course.status === "failed" || next.course.run?.state === "failed";
      if (next.course.status === "ready" || failed) {
        stopPolling();
      }
    };

    pollRef.current = poll;
    void poll();
    pollTimerRef.current = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      stopPolling();
    };
    // `update` is stable via useCallback in the provider.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId, start]);

  // Live writing/review text, pushed by the backend over SSE as the model
  // actually produces it (see app/core/generation_stream.py) - not polling.
  // A native EventSource reconnects on its own on a drop, and the server
  // replays whatever's currently in flight to a fresh connection, which is
  // also what makes a page refresh resume rather than restart (nothing is
  // ever POSTed here - this is a read-only window onto the run already
  // started by `generateCourse` above).
  useEffect(() => {
    const source = new EventSource(apiUrl(`/api/courses/${courseId}/generate/stream`), {
      withCredentials: true,
    });
    source.onmessage = (event) => {
      let payload: ChapterStreamEvent;
      try {
        payload = JSON.parse(event.data) as ChapterStreamEvent;
      } catch {
        return;
      }
      setStreamingChapters((current) => {
        const previous = current[payload.chapter_id];
        // Never regress from real text to nothing - the reviewer's own call
        // has no prose to stream, so a "reviewing" event naturally carries
        // an empty `text`; keep showing the last real content until either
        // a revision streams something new or the real document lands.
        const text = payload.text || previous?.text || "";
        return {
          ...current,
          [payload.chapter_id]: { phase: payload.phase, text, done: payload.done },
        };
      });
    };
    // EventSource retries automatically; nothing else to do here beyond not
    // letting a transient error take down the (independent) polling UI.
    source.onerror = () => {};
    return () => source.close();
  }, [courseId]);

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
    // Polling stops once a run reaches ready/failed (see the effect above);
    // a manual retry starts a fresh run, so resume it instead of leaving the
    // UI frozen on the old outcome.
    if (pollTimerRef.current === undefined) {
      void pollRef.current();
      pollTimerRef.current = setInterval(() => pollRef.current(), POLL_INTERVAL_MS);
    }
    setRetrying(false);
  };

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        step={3}
        title="Generating Your Course"
        subtitle="This may take a few minutes. You can leave this page."
      />

      <div className="grid items-start gap-6 lg:grid-cols-[380px_1fr]">
        <section className="panel flex flex-col gap-6 p-6">
          <div className="flex flex-col items-center py-2 text-center">
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

          <div className="border-t border-line pt-6">
            <StageList stages={view.stages} />
          </div>

          {error || view.failed ? (
            <div className="flex flex-col gap-3 rounded-card border border-red-200 bg-red-50 px-4 py-3">
              <p className="text-[12.5px] text-red-800">
                {error || view.currentDetail || "Generation failed. See the backend logs for details."}
              </p>
              <Button variant="outline" size="sm" onClick={handleRetry} disabled={retrying}>
                <RotateCcw size={13} />
                {retrying ? "Retrying…" : "Retry generation"}
              </Button>
            </div>
          ) : (
            <div className="flex flex-col gap-3 rounded-card border border-brand-100 bg-brand-50 px-4 py-3.5">
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
            <ul className="space-y-1">
              {detail.course.warnings.map((warning, index) => (
                <li key={index} className="text-[11.5px] text-amber-700">
                  {warning}
                </li>
              ))}
            </ul>
          ) : null}
        </section>

        <div className="h-[600px] lg:h-[760px]">
          <LiveDocumentPreview
            courseTitle={detail?.course.input.course_title ?? "Your course"}
            audience={detail?.course.input.target_audience ?? ""}
            document={liveDocument}
            chapters={detail?.course.chapters ?? []}
            emptyMessage={emptyStateMessage(view)}
            streamingChapters={streamingChapters}
          />
        </div>
      </div>
    </div>
  );
}
