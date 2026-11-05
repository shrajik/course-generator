/**
 * Where a course's content comes from, as cited in its own text.
 *
 * The writer cites a passage it used with a marker: `[G2.5]` is passage 5 of
 * document 2 in the organisation's knowledge library, `[S1.3]` is passage 3
 * of the creator's own uploaded source 1. These helpers find every marker in
 * a document and group them by the document they point at, for the editor's
 * Sources panel and its clickable citation badges.
 *
 * Pure (no React, no fetching) so it can be tested on its own.
 */

import { splitMarkers } from "../review/highlight.ts";

/** knowledge = the library, upload = the creator's own file, memory = approved material from earlier courses. */
export type CitationOrigin = "knowledge" | "upload" | "memory";

/** One colour per origin, so a badge, a legend and a source row always agree. */
export const ORIGIN_CLASSES: Record<CitationOrigin, { badge: string; chip: string; dot: string; text: string }> = {
  knowledge: { badge: "bg-teal-50 text-teal-700 ring-1 ring-teal-200", chip: "bg-teal-50 text-teal-700", dot: "bg-teal-100 ring-1 ring-teal-300", text: "text-teal-700" },
  upload: { badge: "bg-brand-50 text-brand-700 ring-1 ring-brand-200", chip: "bg-brand-50 text-brand-700", dot: "bg-brand-50 ring-1 ring-brand-200", text: "text-brand-700" },
  memory: { badge: "bg-violet-50 text-violet-700 ring-1 ring-violet-200", chip: "bg-violet-50 text-violet-700", dot: "bg-violet-100 ring-1 ring-violet-300", text: "text-violet-700" },
};

export interface CitationPlace {
  blockId: string;
  pageIndex: number;
  chapterId: string | null;
}

export interface PassageInfo {
  ref: string;
  source_id: string;
  filename: string;
  heading: string;
  page: number | null;
  text: string;
}

export interface CitedSource {
  origin: CitationOrigin;
  sourceId: string;
  filename: string;
  /** Every reference to this source, in reading order of first use. */
  refs: string[];
  /** How many times the text cites it. */
  citations: number;
  chapterIds: string[];
}

interface DocumentLike {
  pages: { blocks: { id: string; content: unknown; meta?: { chapter_id?: string | null } }[] }[];
}

/** "G2.5" is the knowledge library; "A7.2" approved earlier material; "S1.3" the creator's own upload. */
export function originOf(ref: string): CitationOrigin {
  if (ref.startsWith("G")) return "knowledge";
  if (ref.startsWith("A")) return "memory";
  return "upload";
}

export function originLabel(origin: CitationOrigin): string {
  if (origin === "knowledge") return "Knowledge library";
  if (origin === "memory") return "Approved earlier material";
  return "Your uploaded source";
}

function strings(value: unknown, out: string[]): void {
  if (typeof value === "string") {
    out.push(value);
  } else if (Array.isArray(value)) {
    for (const item of value) strings(item, out);
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value)) strings(item, out);
  }
}

/** ref -> every place it is cited, in reading order. */
export function collectCitations(document: DocumentLike): Map<string, CitationPlace[]> {
  const found = new Map<string, CitationPlace[]>();
  document.pages.forEach((page, pageIndex) => {
    for (const block of page.blocks) {
      const texts: string[] = [];
      strings(block.content, texts);
      for (const text of texts) {
        if (!text.includes("[")) continue;
        for (const part of splitMarkers(text)) {
          if (part.kind !== "ref") continue;
          const places = found.get(part.ref) ?? [];
          places.push({ blockId: block.id, pageIndex, chapterId: block.meta?.chapter_id ?? null });
          found.set(part.ref, places);
        }
      }
    }
  });
  return found;
}

/**
 * Group the cited references by the document they belong to. A reference
 * whose passage could not be found (the document was removed, or the marker
 * was typed by hand) is grouped under its own reference with no file name,
 * so it is still visible rather than silently dropped.
 */
export function groupBySource(
  citations: Map<string, CitationPlace[]>,
  passages: PassageInfo[],
): CitedSource[] {
  const byRef = new Map(passages.map((passage) => [passage.ref, passage]));
  const groups = new Map<string, CitedSource>();
  for (const [ref, places] of citations) {
    const passage = byRef.get(ref);
    const origin = originOf(ref);
    const key = passage ? `${origin}:${passage.source_id}` : `missing:${ref}`;
    const group = groups.get(key) ?? {
      origin,
      sourceId: passage?.source_id ?? "",
      filename: passage?.filename ?? "",
      refs: [],
      citations: 0,
      chapterIds: [],
    };
    group.refs.push(ref);
    group.citations += places.length;
    for (const place of places) {
      if (place.chapterId && !group.chapterIds.includes(place.chapterId)) group.chapterIds.push(place.chapterId);
    }
    groups.set(key, group);
  }
  // Library first, then approved earlier material, then uploads; within each, the most-cited first.
  const rank: Record<CitationOrigin, number> = { knowledge: 0, memory: 1, upload: 2 };
  return [...groups.values()].sort((a, b) => (a.origin === b.origin ? b.citations - a.citations : rank[a.origin] - rank[b.origin]));
}
