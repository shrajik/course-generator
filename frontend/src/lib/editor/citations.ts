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

export type CitationOrigin = "knowledge" | "upload";

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

/** "G2.5" is the knowledge library; "S1.3" is the creator's own upload. */
export function originOf(ref: string): CitationOrigin {
  return ref.startsWith("G") ? "knowledge" : "upload";
}

export function originLabel(origin: CitationOrigin): string {
  return origin === "knowledge" ? "Knowledge library" : "Your uploaded source";
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
  // Library first, then uploads; within each, the most-cited first.
  return [...groups.values()].sort(
    (a, b) => (a.origin === b.origin ? b.citations - a.citations : a.origin === "knowledge" ? -1 : 1),
  );
}
