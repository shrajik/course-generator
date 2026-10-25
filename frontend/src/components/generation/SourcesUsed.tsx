"use client";

import { BookOpen, FileText, Globe } from "lucide-react";
import type { ChapterProgress, SourceUse } from "@/lib/types/course";

/**
 * Where each chapter's content comes from, shown while the course is being
 * generated: the knowledge-library documents and the creator's own uploads it
 * was given passages from, or web research only when nothing matched.
 */
export function SourcesUsed({ chapters }: { chapters: ChapterProgress[] }) {
  const checked = chapters.filter((chapter) => chapter.sources_checked);
  if (!checked.length) return null;

  const library = new Set<string>();
  const uploads = new Set<string>();
  for (const chapter of checked) {
    for (const use of chapter.sources_offered ?? []) {
      (use.origin === "knowledge" ? library : uploads).add(use.source_id);
    }
  }

  return (
    <div className="border-t border-line pt-5">
      <p className="text-[12.5px] font-semibold text-ink">Where the content comes from</p>
      <p className="mt-0.5 text-[11.5px] leading-4 text-ink-500">
        {library.size || uploads.size
          ? `${library.size} knowledge-library document${library.size === 1 ? "" : "s"} and ${uploads.size} uploaded document${uploads.size === 1 ? "" : "s"} matched. Statements taken from them are marked in the text, and you can open each one in the editor.`
          : "No knowledge-library document or upload matched these chapters, so they use web research."}
      </p>
      <ul className="mt-3 space-y-2.5">
        {chapters.map((chapter, index) => (
          <li key={chapter.chapter_id}>
            <p className="truncate text-[12px] font-medium text-ink" title={chapter.title}>
              {index + 1}. {chapter.title}
            </p>
            {!chapter.sources_checked ? (
              <p className="text-[11px] text-ink-400">Not searched yet</p>
            ) : (chapter.sources_offered ?? []).length === 0 ? (
              <p className="flex items-center gap-1 text-[11px] text-ink-500">
                <Globe size={11} className="text-ink-400" /> Web research only
              </p>
            ) : (
              <ul className="mt-0.5 space-y-0.5">
                {(chapter.sources_offered ?? []).map((use) => (
                  <SourceLine key={`${use.origin}:${use.source_id}`} use={use} />
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SourceLine({ use }: { use: SourceUse }) {
  const library = use.origin === "knowledge";
  const Icon = library ? BookOpen : FileText;
  return (
    <li className="flex items-center gap-1.5 text-[11px] text-ink-500" title={use.refs.join(", ")}>
      <Icon size={11} className={library ? "shrink-0 text-teal-600" : "shrink-0 text-brand-600"} />
      <span className="truncate">
        <span className={library ? "font-medium text-teal-700" : "font-medium text-brand-700"}>
          {library ? "Knowledge library" : "Your upload"}
        </span>
        : {use.filename}
      </span>
      <span className="shrink-0 text-ink-400">
        · {use.passages} passage{use.passages === 1 ? "" : "s"}
      </span>
    </li>
  );
}
