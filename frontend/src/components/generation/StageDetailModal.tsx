"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, ExternalLink, ImageIcon, Loader2, XCircle } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { ApiError } from "@/lib/api/client";
import { getBlueprint, getChapter, getChapterResearch } from "@/lib/api/courses";
import { allBlocks } from "@/lib/editor/blocks";
import type { GenerationStage } from "@/lib/generation/stages";
import type {
  ChapterResearch,
  CourseBlueprint,
  GeneratedChapterSummary,
} from "@/lib/types/course";
import type { CourseDocument } from "@/lib/types/document";

function Loading() {
  return (
    <div className="flex items-center gap-2 py-6 text-[12.5px] text-ink-500">
      <Loader2 size={15} className="animate-spin text-brand-600" />
      Loading…
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return <p className="py-4 text-[12.5px] text-danger">{message}</p>;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
      {children}
    </p>
  );
}

// --- Course Planning -------------------------------------------------------

function PlanningDetail({ courseId }: { courseId: string }) {
  const [blueprint, setBlueprint] = useState<CourseBlueprint | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getBlueprint(courseId)
      .then((data) => active && setBlueprint(data))
      .catch((caught) =>
        active && setError(caught instanceof ApiError ? caught.message : "Could not load the plan."),
      );
    return () => {
      active = false;
    };
  }, [courseId]);

  if (error) return <ErrorState message={error} />;
  if (!blueprint) return <Loading />;

  return (
    <div className="space-y-4">
      {blueprint.course_summary ? (
        <p className="text-[13px] leading-6 text-ink-700">{blueprint.course_summary}</p>
      ) : null}
      {blueprint.learning_objectives.length ? (
        <div>
          <SectionLabel>Learning objectives</SectionLabel>
          <ul className="list-disc space-y-1 pl-4 text-[12.5px] leading-5 text-ink-600">
            {blueprint.learning_objectives.map((objective, index) => (
              <li key={index}>{objective}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div>
        <SectionLabel>Planned chapters ({blueprint.chapters.length})</SectionLabel>
        <ol className="space-y-2.5">
          {blueprint.chapters.map((chapter) => (
            <li key={chapter.id} className="rounded-[8px] border border-line bg-canvas px-3 py-2.5">
              <p className="text-[12.5px] font-semibold text-ink">{chapter.title}</p>
              {chapter.summary ? (
                <p className="mt-0.5 text-[12px] leading-5 text-ink-500">{chapter.summary}</p>
              ) : null}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

// --- Deep Research -----------------------------------------------------------

function ResearchDetail({ courseId, chapterIds }: { courseId: string; chapterIds: string[] }) {
  const [results, setResults] = useState<Record<string, ChapterResearch | "unavailable">>({});

  useEffect(() => {
    let active = true;
    setResults({});
    chapterIds.forEach((chapterId) => {
      getChapterResearch(courseId, chapterId)
        .then((data) => active && setResults((current) => ({ ...current, [chapterId]: data })))
        .catch(() => active && setResults((current) => ({ ...current, [chapterId]: "unavailable" })));
    });
    return () => {
      active = false;
    };
  }, [courseId, chapterIds]);

  if (chapterIds.length === 0) {
    return <p className="py-4 text-[12.5px] text-ink-500">No chapters have finished research yet.</p>;
  }

  return (
    <div className="space-y-4">
      {chapterIds.map((chapterId) => {
        const result = results[chapterId];
        return (
          <div key={chapterId}>
            {!result ? (
              <Loading />
            ) : result === "unavailable" ? (
              <p className="text-[12.5px] text-ink-400">No sources recorded for this chapter.</p>
            ) : (
              <div>
                <SectionLabel>
                  {result.chapter_title || chapterId} · {result.mode === "deep" ? "Deep Research" : "Web search"}
                </SectionLabel>
                {result.payload.references.length === 0 ? (
                  <p className="text-[12px] text-ink-400">No sources were cited for this chapter.</p>
                ) : (
                  <ul className="space-y-2">
                    {result.payload.references.map((reference, index) => (
                      <li key={index} className="rounded-[8px] border border-line px-3 py-2">
                        {reference.url ? (
                          <a
                            href={reference.url}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center gap-1.5 text-[12.5px] font-medium text-brand-600 hover:underline"
                          >
                            {reference.title || reference.url}
                            <ExternalLink size={11} className="shrink-0" />
                          </a>
                        ) : (
                          <p className="text-[12.5px] font-medium text-ink">{reference.title}</p>
                        )}
                        {reference.note ? (
                          <p className="mt-0.5 text-[11.5px] text-ink-500">{reference.note}</p>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// --- Writing / Reviewing (both read the same chapter detail) ---------------

function ChaptersDetail({
  courseId,
  chapterIds,
  showReview,
}: {
  courseId: string;
  chapterIds: string[];
  showReview: boolean;
}) {
  const [results, setResults] = useState<Record<string, GeneratedChapterSummary | "unavailable">>({});

  useEffect(() => {
    let active = true;
    setResults({});
    chapterIds.forEach((chapterId) => {
      getChapter(courseId, chapterId)
        .then((data) => active && setResults((current) => ({ ...current, [chapterId]: data.chapter })))
        .catch(() => active && setResults((current) => ({ ...current, [chapterId]: "unavailable" })));
    });
    return () => {
      active = false;
    };
  }, [courseId, chapterIds]);

  if (chapterIds.length === 0) {
    return <p className="py-4 text-[12.5px] text-ink-500">No chapters are done yet.</p>;
  }

  return (
    <div className="space-y-4">
      {chapterIds.map((chapterId) => {
        const chapter = results[chapterId];
        if (!chapter) return <Loading key={chapterId} />;
        if (chapter === "unavailable") {
          return (
            <p key={chapterId} className="text-[12.5px] text-ink-400">
              Details unavailable for this chapter.
            </p>
          );
        }
        const review = chapter.review;
        const scoreValues = review
          ? Object.values(review.scores).filter((value): value is number => typeof value === "number")
          : [];
        const overall = scoreValues.length
          ? scoreValues.reduce((a, b) => a + b, 0) / scoreValues.length
          : null;
        return (
          <div key={chapterId} className="rounded-[8px] border border-line bg-canvas px-3 py-2.5">
            <p className="text-[12.5px] font-semibold text-ink">{chapter.title}</p>
            {chapter.summary ? (
              <p className="mt-0.5 text-[12px] leading-5 text-ink-500">{chapter.summary}</p>
            ) : null}
            <p className="mt-1 text-[11.5px] text-ink-400">{chapter.blocks.length} content blocks</p>

            {showReview ? (
              !review ? (
                <p className="mt-2 text-[11.5px] text-ink-400">Not reviewed yet.</p>
              ) : (
                <div className="mt-2.5 border-t border-line pt-2.5">
                  <div className="flex items-center gap-1.5">
                    {review.approved ? (
                      <CheckCircle2 size={13} className="text-success" />
                    ) : (
                      <XCircle size={13} className="text-danger" />
                    )}
                    <span className="text-[12px] font-medium text-ink">
                      {review.approved ? "Approved" : "Changes requested"}
                    </span>
                    {overall !== null ? (
                      <span className="text-[11.5px] text-ink-400">· {overall.toFixed(1)}/10 avg score</span>
                    ) : null}
                  </div>
                  {review.summary ? (
                    <p className="mt-1 text-[12px] leading-5 text-ink-600">{review.summary}</p>
                  ) : null}
                  {review.issues.length ? (
                    <ul className="mt-1.5 space-y-1">
                      {review.issues.map((issue, index) => (
                        <li key={index} className="text-[11.5px] leading-5 text-ink-500">
                          <span className="font-medium text-ink-700">{issue.severity}</span>
                          {issue.category ? ` (${issue.category})` : ""}: {issue.description}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              )
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

// --- Images / Document (derived client-side from the already-loaded doc) ---

function ImagesDetail({ document }: { document: CourseDocument | null }) {
  if (!document) {
    return <p className="py-4 text-[12.5px] text-ink-500">Open the editor to see the generated images.</p>;
  }
  const images = allBlocks(document).filter(
    (block) => block.type === "image" && typeof block.content.path === "string",
  );
  if (images.length === 0) {
    return <p className="py-4 text-[12.5px] text-ink-500">No images were generated for this course.</p>;
  }
  return (
    <div>
      <SectionLabel>{images.length} image{images.length === 1 ? "" : "s"} generated</SectionLabel>
      <ul className="space-y-1.5">
        {images.map((block) => (
          <li key={block.id} className="flex items-center gap-2 text-[12.5px] text-ink-600">
            <ImageIcon size={13} className="shrink-0 text-ink-400" />
            {(block.content.caption as string) || (block.content.purpose as string) || "Untitled image"}
          </li>
        ))}
      </ul>
    </div>
  );
}

function DocumentDetail({ document }: { document: CourseDocument | null }) {
  if (!document) {
    return <p className="py-4 text-[12.5px] text-ink-500">Open the editor to see the built document.</p>;
  }
  return (
    <ul className="space-y-1.5 text-[12.5px] text-ink-600">
      <li>{document.pages.length} page{document.pages.length === 1 ? "" : "s"} built</li>
      <li>{document.meta.chapter_ids.length} chapters included</li>
      <li>Template: {document.template_id}</li>
    </ul>
  );
}

// --- entry point --------------------------------------------------------

export function StageDetailModal({
  courseId,
  stage,
  document,
  onClose,
}: {
  courseId: string;
  stage: GenerationStage | null;
  document: CourseDocument | null;
  onClose: () => void;
}) {
  return (
    <Modal open={stage !== null} title={stage?.label ?? ""} onClose={onClose}>
      {stage?.id === "planning" ? <PlanningDetail courseId={courseId} /> : null}
      {stage?.id === "research" ? (
        <ResearchDetail courseId={courseId} chapterIds={stage.chapterIds ?? []} />
      ) : null}
      {stage?.id.startsWith("writing-") ? (
        <ChaptersDetail courseId={courseId} chapterIds={stage.chapterIds ?? []} showReview={false} />
      ) : null}
      {stage?.id === "reviewing" ? (
        <ChaptersDetail courseId={courseId} chapterIds={stage.chapterIds ?? []} showReview />
      ) : null}
      {stage?.id === "images" ? <ImagesDetail document={document} /> : null}
      {stage?.id === "document" ? <DocumentDetail document={document} /> : null}
    </Modal>
  );
}
