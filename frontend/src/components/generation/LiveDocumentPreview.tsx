"use client";

import { useMemo } from "react";
import { FileText, Loader2 } from "lucide-react";
import { assetUrl } from "@/lib/api/documents";
import { asString, asStringArray } from "@/lib/editor/blocks";
import { isSvgPath, useInlineDiagramSvg } from "@/lib/editor/useInlineDiagramSvg";
import type { ChapterProgress } from "@/lib/types/course";
import type { Block, BlockContent, CourseDocument } from "@/lib/types/document";
import type { ChapterStreamView } from "./GenerationProgress";

interface LiveDocumentPreviewProps {
  courseTitle: string;
  audience: string;
  document: CourseDocument | null;
  chapters: ChapterProgress[];
  /** A real (never invented) description of what the backend is doing right
   * now, shown only until the first actual section exists. */
  emptyMessage: string;
  /** Real model output, streamed live over SSE - see GenerationProgress. */
  streamingChapters: Record<string, ChapterStreamView>;
}

/**
 * A read-only, flowing (non-paginated) rendering of the real CourseDocument as
 * it's built - not the fixed-size A4 canvas the editor/PDF preview use, since
 * this needs to read comfortably in a narrower, continuously-scrolling panel.
 * Content only ever comes from the backend's own document/chapter data; chapters
 * not written yet render as a labelled skeleton, never invented text.
 */
export function LiveDocumentPreview({
  courseTitle,
  audience,
  document,
  chapters,
  emptyMessage,
  streamingChapters,
}: LiveDocumentPreviewProps) {
  const { introBlocks, chapterBlocks, hasRealToc } = useMemo(() => {
    const blocks = document?.pages.flatMap((page) => page.blocks) ?? [];
    const byChapter = new Map<string, Block[]>();
    const intro: Block[] = [];
    let sawToc = false;
    for (const block of blocks) {
      const chapterId = block.meta.chapter_id;
      if (!chapterId) {
        intro.push(block);
        if (block.meta.section_key === "toc") sawToc = true;
        continue;
      }
      const existing = byChapter.get(chapterId);
      if (existing) existing.push(block);
      else byChapter.set(chapterId, [block]);
    }
    return { introBlocks: intro, chapterBlocks: byChapter, hasRealToc: sawToc };
  }, [document]);

  const activeChapterId = chapters.find((chapter) => !chapter.written)?.chapter_id ?? null;
  // The plan (chapter titles) is known as soon as planning finishes - real
  // data, well before any content is written - so the panel has something
  // structural to show immediately instead of staying blank.
  const hasAnyContent = introBlocks.length > 0 || chapterBlocks.size > 0 || chapters.length > 0;

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-panel border border-line bg-white shadow-card">
      <div className="flex items-center gap-2 border-b border-line px-5 py-3">
        <FileText size={14} className="text-brand-600" />
        <p className="text-[12px] font-semibold uppercase tracking-wide text-ink-500">
          Live document preview
        </p>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6 sm:px-10">
        {!hasAnyContent ? (
          <div className="flex h-full min-h-[280px] flex-col items-center justify-center gap-2 text-center text-ink-400">
            <Loader2 size={18} className="animate-spin text-brand-500" />
            <p className="text-[12.5px]">{emptyMessage}</p>
          </div>
        ) : (
          <article className="mx-auto max-w-[640px]">
            <h1 className="gen-block-in text-[26px] font-bold leading-tight tracking-[-0.02em] text-ink">
              {courseTitle}
            </h1>

            {introBlocks.length === 0 && audience ? (
              <div className="gen-block-in mt-3">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                  Course Overview
                </p>
                <p className="mt-1 text-[13px] leading-[1.6] text-ink-600">
                  Designed for: {audience}
                </p>
              </div>
            ) : null}

            {introBlocks.map((block) => (
              <FlowBlock key={block.id} block={block} documentId={document?.document_id ?? ""} />
            ))}

            {!hasRealToc && chapters.length > 0 ? (
              <div className="gen-block-in mt-6">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                  Table of Contents
                </p>
                <ol className="mt-2 space-y-1.5">
                  {chapters.map((chapter, index) => (
                    <li
                      key={chapter.chapter_id}
                      className="flex items-baseline gap-2 text-[13px] text-ink-600"
                    >
                      <span className="w-5 shrink-0 tabular-nums text-ink-400">{index + 1}.</span>
                      <span className={chapter.written ? "text-ink-700" : "text-ink-400"}>
                        {chapter.title}
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
            ) : null}

            {chapters.map((chapter, index) => {
              const blocks = chapterBlocks.get(chapter.chapter_id);
              if (blocks && blocks.length > 0) {
                return (
                  <section key={chapter.chapter_id} className="gen-block-in mt-8">
                    {blocks.map((block) => (
                      <FlowBlock
                        key={block.id}
                        block={block}
                        documentId={document?.document_id ?? ""}
                      />
                    ))}
                  </section>
                );
              }

              const isActive = chapter.chapter_id === activeChapterId;
              const stream = streamingChapters[chapter.chapter_id];
              if (stream && stream.text) {
                return (
                  <ChapterStreaming
                    key={chapter.chapter_id}
                    number={index + 1}
                    title={chapter.title}
                    phase={stream.phase}
                    text={stream.text}
                  />
                );
              }
              return (
                <ChapterSkeleton
                  key={chapter.chapter_id}
                  number={index + 1}
                  title={chapter.title}
                  active={isActive}
                  failed={Boolean(chapter.error)}
                />
              );
            })}
          </article>
        )}
      </div>
    </div>
  );
}

function ChapterSkeleton({
  number,
  title,
  active,
  failed,
}: {
  number: number;
  title: string;
  active: boolean;
  failed: boolean;
}) {
  return (
    <div className="mt-8">
      <div className="flex items-center gap-2">
        {active ? <span className="gen-active-dot" aria-hidden /> : null}
        <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
          Chapter {number}
        </p>
      </div>
      <p className="mt-1 text-[18px] font-semibold text-ink-300">
        {title}
        {active && !failed ? <span className="gen-cursor" aria-hidden /> : null}
      </p>
      {failed ? (
        <p className="mt-2 text-[12px] text-red-500">This chapter hit an error and will retry.</p>
      ) : (
        <div className="mt-3 space-y-2.5">
          <div className="gen-skeleton-line h-3 w-full" />
          <div className="gen-skeleton-line h-3 w-[92%]" />
          <div className="gen-skeleton-line h-3 w-[75%]" />
        </div>
      )}
    </div>
  );
}

/** The real model output for a chapter still in flight, growing paragraph by
 * paragraph as the writer/reviser actually streams it (see GenerationProgress'
 * EventSource). Replaces ChapterSkeleton for that chapter the moment any real
 * text has arrived; the real formatted `FlowBlock`s take back over once the
 * chapter's actual document blocks land. */
function ChapterStreaming({
  number,
  title,
  phase,
  text,
}: {
  number: number;
  title: string;
  phase: "writing" | "reviewing";
  text: string;
}) {
  const paragraphs = text.split("\n\n").filter((p) => p.trim());
  return (
    <div className="mt-8">
      <div className="flex items-center gap-2">
        <span className="gen-active-dot" aria-hidden />
        <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
          Chapter {number} · {phase === "reviewing" ? "Reviewing" : "Writing"}
        </p>
      </div>
      <p className="mt-1 text-[18px] font-semibold text-ink">{title}</p>
      <div className="mt-3 space-y-3">
        {paragraphs.map((paragraph, index) => {
          const isLast = index === paragraphs.length - 1;
          return (
            <p key={index} className="gen-block-in text-[13.5px] leading-[1.7] text-ink-700">
              {paragraph}
              {isLast && phase === "writing" ? <span className="gen-cursor" aria-hidden /> : null}
            </p>
          );
        })}
      </div>
    </div>
  );
}

function FlowBlock({ block, documentId }: { block: Block; documentId: string }) {
  const content = block.content;

  switch (block.type) {
    case "heading": {
      const level = Number(content.level) || 2;
      const text = asString(content.text);
      const className =
        level <= 1
          ? "gen-block-in mt-6 text-[22px] font-bold text-ink"
          : level === 2
            ? "gen-block-in mt-6 text-[18px] font-bold text-ink"
            : "gen-block-in mt-4 text-[15px] font-semibold text-ink";
      if (level <= 1) return <h2 className={className}>{text}</h2>;
      if (level === 2) return <h3 className={className}>{text}</h3>;
      return <h4 className={className}>{text}</h4>;
    }

    case "paragraph":
    case "story":
      return (
        <p className="gen-block-in mt-3 text-[13.5px] leading-[1.7] text-ink-700">
          {asString(content.text)}
        </p>
      );

    case "quote":
      return (
        <blockquote className="gen-block-in mt-4 border-l-2 border-brand-300 pl-4 text-[13.5px] italic leading-[1.6] text-ink-600">
          {asString(content.text)}
          {content.attribution ? (
            <footer className="mt-1 text-[12px] not-italic text-ink-400">
              — {asString(content.attribution)}
            </footer>
          ) : null}
        </blockquote>
      );

    case "callout":
    case "tip":
    case "warning": {
      const tone =
        block.type === "warning"
          ? "border-red-200 bg-red-50 text-red-800"
          : block.type === "tip"
            ? "border-emerald-200 bg-emerald-50 text-emerald-800"
            : "border-brand-100 bg-brand-50 text-ink-700";
      return (
        <div className={`gen-block-in mt-4 rounded-[10px] border px-4 py-3 text-[13px] ${tone}`}>
          {content.title ? <p className="font-semibold">{asString(content.title)}</p> : null}
          <p className="mt-1 leading-[1.6]">{asString(content.text)}</p>
        </div>
      );
    }

    case "summary":
    case "learning_objectives":
    case "reflection": {
      const items = asStringArray(content.items ?? content.key_takeaways);
      return (
        <div className="gen-block-in mt-4">
          {content.title ? (
            <p className="text-[13.5px] font-semibold text-ink">{asString(content.title)}</p>
          ) : null}
          <ul className="mt-2 list-disc space-y-1 pl-5 text-[13px] leading-[1.6] text-ink-700">
            {items.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      );
    }

    case "code":
      return (
        <pre className="gen-block-in mt-4 overflow-x-auto rounded-[10px] bg-[#1f2937] px-4 py-3 text-[12px] leading-[1.6] text-[#f3f4f6]">
          <code>{asString(content.code)}</code>
        </pre>
      );

    case "table": {
      const columns = asStringArray(content.columns);
      const rows = Array.isArray(content.rows) ? (content.rows as { cells: string[] }[]) : [];
      return (
        <div className="gen-block-in mt-4 overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr>
                {columns.map((col, index) => (
                  <th
                    key={index}
                    className="border-b border-line px-2 py-1.5 text-left font-semibold text-ink"
                  >
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {asStringArray(row.cells).map((cell, cellIndex) => (
                    <td key={cellIndex} className="border-b border-line px-2 py-1.5 text-ink-700">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    case "image":
      return <ImagePreviewBlock content={content} documentId={documentId} />;

    case "divider":
      return <hr className="gen-block-in my-5 border-line" />;

    case "quiz":
    case "exercise":
    case "challenge":
    case "case_study":
      return (
        <div className="gen-block-in mt-4 rounded-[10px] border border-line bg-canvas px-4 py-3">
          <p className="text-[13px] font-semibold text-ink">
            {asString(content.title) || "Activity"}
          </p>
          {content.instructions ? (
            <p className="mt-1 text-[12.5px] leading-[1.6] text-ink-600">
              {asString(content.instructions)}
            </p>
          ) : null}
        </div>
      );

    default:
      return null;
  }
}

/** A diagram is inlined (hover/focus tooltips need real DOM, not `<img
 * src>`) and never cropped - `object-cover` would cut off diagram labels.
 * A raster illustration keeps the original cropped, fill-the-box look. */
function ImagePreviewBlock({
  content,
  documentId,
}: {
  content: BlockContent;
  documentId: string;
}) {
  const path = typeof content.path === "string" ? content.path : null;
  const src = path ? assetUrl(documentId, path) : null;
  const isDiagram = asString(content.kind) === "diagram" && isSvgPath(path);
  const inlineMarkup = useInlineDiagramSvg(documentId, path, isDiagram);

  return (
    <div className="gen-block-in mt-4 overflow-hidden rounded-[10px] border border-line bg-canvas">
      {inlineMarkup ? (
        <div
          className="flex items-center justify-center p-2 [&>svg]:h-auto [&>svg]:max-h-[360px] [&>svg]:w-full"
          role="img"
          aria-label={asString(content.alt)}
          // Trusted source: our own backend-rendered SVG, not user HTML.
          dangerouslySetInnerHTML={{ __html: inlineMarkup }}
        />
      ) : src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={asString(content.alt)}
          className={isDiagram ? "w-full object-contain" : "w-full object-cover"}
        />
      ) : (
        <div className="flex h-40 items-center justify-center text-[12px] text-ink-400">
          Image generating…
        </div>
      )}
      {content.caption ? (
        <p className="px-3 py-2 text-[11.5px] text-ink-400">{asString(content.caption)}</p>
      ) : null}
    </div>
  );
}
