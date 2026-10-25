"use client";

import { useContext, useEffect, useMemo, useState } from "react";
import { BookOpen, ChevronDown, ChevronRight, FileText, Loader2 } from "lucide-react";
import { getPassagesByRefs, type Passage } from "@/lib/api/review";
import { collectCitations, groupBySource, originLabel } from "@/lib/editor/citations";
import { useEditor } from "@/lib/editor/store";
import { CitationContext } from "@/components/blocks/CitationBadge";
import { cn } from "@/lib/utils/cn";

const BATCH = 50; // the passages endpoint takes at most 50 references at once

function chapterLabel(chapterId: string): string {
  const match = /chapter_(\d+)/.exec(chapterId);
  return match ? `Ch. ${match[1]}` : chapterId;
}

/**
 * Every document this course cites, read from its own text: the knowledge
 * library documents and the creator's uploads, how often each is cited and
 * where. Selecting a citation shows its passage and jumps to where it is used.
 */
export function CitedSourcesPanel() {
  const editor = useEditor();
  const citationContext = useContext(CitationContext);
  const document = editor.document;
  const courseId = document?.course_id ?? null;
  const citations = useMemo(() => (document ? collectCitations(document) : new Map()), [document]);
  const refsKey = [...citations.keys()].sort().join(",");
  const [passages, setPassages] = useState<Passage[]>([]);
  const [loading, setLoading] = useState(false);
  const [openKey, setOpenKey] = useState<string | null>(null);

  useEffect(() => {
    if (!courseId || !refsKey) {
      setPassages([]);
      return;
    }
    let cancelled = false;
    const refs = refsKey.split(",");
    setLoading(true);
    const batches: Promise<Passage[]>[] = [];
    for (let i = 0; i < refs.length; i += BATCH) {
      batches.push(getPassagesByRefs(courseId, refs.slice(i, i + BATCH)).catch(() => []));
    }
    Promise.all(batches)
      .then((results) => !cancelled && setPassages(results.flat()))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [courseId, refsKey]);

  const groups = useMemo(() => groupBySource(citations, passages), [citations, passages]);
  const total = groups.reduce((sum, group) => sum + group.citations, 0);
  const library = groups.filter((group) => group.origin === "knowledge" && group.filename).length;
  const uploads = groups.filter((group) => group.origin === "upload" && group.filename).length;

  const goTo = (ref: string) => {
    const place = citations.get(ref)?.[0];
    if (place) {
      editor.setPage(place.pageIndex);
      editor.select([place.blockId]);
    }
    citationContext?.open(ref);
  };

  return (
    <div className="flex h-full flex-col">
      <div className="px-3 pb-2 pt-3">
        <p className="text-[12.5px] font-semibold text-ink">Sources</p>
        <p className="mt-0.5 text-[11px] leading-snug text-ink-400">
          {total
            ? `${total} citation${total === 1 ? "" : "s"}: ${library} knowledge-library document${library === 1 ? "" : "s"}, ${uploads} uploaded document${uploads === 1 ? "" : "s"}.`
            : "Where this course's statements come from."}
        </p>
        <div className="mt-2 flex flex-wrap gap-2 text-[10.5px] text-ink-500">
          <span className="inline-flex items-center gap-1">
            <span className="h-2.5 w-2.5 rounded-sm bg-teal-100 ring-1 ring-teal-300" /> Knowledge library
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2.5 w-2.5 rounded-sm bg-brand-50 ring-1 ring-brand-200" /> Your upload
          </span>
        </div>
      </div>

      <div className="subtle-scroll flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
        {!total ? (
          <p className="px-1 text-[11.5px] leading-4 text-ink-400">
            This course does not cite the knowledge library or any uploaded document. Its text is based on web research
            and the AI&apos;s general knowledge.
          </p>
        ) : null}
        {loading && !passages.length ? (
          <p className="flex items-center gap-1.5 px-1 text-[11.5px] text-ink-400">
            <Loader2 size={12} className="animate-spin" /> Looking up the documents…
          </p>
        ) : null}
        {groups.map((group) => {
          const key = `${group.origin}:${group.sourceId || group.refs[0]}`;
          const open = openKey === key;
          const Icon = group.origin === "knowledge" ? BookOpen : FileText;
          return (
            <div key={key} className="rounded-[8px] border border-line bg-white">
              <button
                type="button"
                onClick={() => setOpenKey(open ? null : key)}
                className="flex w-full items-start gap-2 px-2.5 py-2 text-left"
              >
                <span
                  className={cn(
                    "mt-0.5 rounded p-1",
                    group.origin === "knowledge" ? "bg-teal-50 text-teal-700" : "bg-brand-50 text-brand-700",
                  )}
                >
                  <Icon size={12} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12px] font-medium text-ink" title={group.filename}>
                    {group.filename || (loading ? "Loading…" : "Passage not found")}
                  </span>
                  <span className="block text-[10.5px] text-ink-400">
                    {originLabel(group.origin)} · cited {group.citations}×
                    {group.chapterIds.length ? ` · ${group.chapterIds.map(chapterLabel).join(", ")}` : ""}
                  </span>
                </span>
                {open ? <ChevronDown size={13} className="mt-1 text-ink-400" /> : <ChevronRight size={13} className="mt-1 text-ink-400" />}
              </button>
              {open ? (
                <ul className="space-y-0.5 border-t border-line px-2.5 py-1.5">
                  {group.refs.map((ref) => {
                    const passage = passages.find((item) => item.ref === ref);
                    return (
                      <li key={ref}>
                        <button
                          type="button"
                          onClick={() => goTo(ref)}
                          className="w-full rounded px-1 py-1 text-left text-[11px] text-ink-500 hover:bg-brand-50 hover:text-ink"
                          title="Show the passage and go to where it is cited"
                        >
                          <span className="font-semibold text-ink">[{ref}]</span>{" "}
                          {passage?.page ? `p. ${passage.page} · ` : ""}
                          {passage?.heading || (passage ? passage.text.slice(0, 60) + "…" : "")}
                          <span className="text-ink-400"> · {citations.get(ref)?.length ?? 0}×</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
