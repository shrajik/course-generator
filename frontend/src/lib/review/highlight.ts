/**
 * Pure helpers for the reviewer's screen: marking the flagged sentences of a
 * block's text in place, and finding the `[S1.3]` source references inside it.
 *
 * Kept free of React so it can be tested on its own - what is highlighted is
 * what a reviewer is told to check, so it has to be right.
 */

export interface Highlightable {
  finding_id: string;
  start?: number | null;
  end?: number | null;
}

export interface Segment<T extends Highlightable> {
  text: string;
  /** The finding this stretch of text belongs to, if it is flagged. */
  finding?: T;
}

/**
 * Cut `text` into plain and flagged stretches, in order. A finding without a
 * usable position (an older report) or one that overlaps an earlier one is left
 * out of the text rather than guessed at - it still appears in the findings
 * list.
 */
export function segmentText<T extends Highlightable>(text: string, findings: readonly T[]): Segment<T>[] {
  const placed = findings
    .filter(
      (f): f is T & { start: number; end: number } =>
        typeof f.start === "number" && typeof f.end === "number" && f.start >= 0 && f.end > f.start && f.end <= text.length,
    )
    .sort((a, b) => a.start - b.start || b.end - a.end);

  const segments: Segment<T>[] = [];
  let cursor = 0;
  for (const finding of placed) {
    if (finding.start < cursor) continue; // overlaps the one before
    if (finding.start > cursor) segments.push({ text: text.slice(cursor, finding.start) });
    segments.push({ text: text.slice(finding.start, finding.end), finding });
    cursor = finding.end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor) });
  return segments.length > 0 ? segments : [{ text }];
}

export type MarkerPart = { kind: "text"; text: string } | { kind: "ref"; ref: string; label: string };

const MARKER = /\[((?:[SG]\d+\.\d+)(?:\s*[,;]\s*[SG]\d+\.\d+)*)\]/g;

/** Split text around source markers; each reference in a marker becomes a part. */
export function splitMarkers(text: string): MarkerPart[] {
  const parts: MarkerPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(MARKER)) {
    const index = match.index ?? 0;
    if (index > cursor) parts.push({ kind: "text", text: text.slice(cursor, index) });
    const refs = match[1].split(/\s*[,;]\s*/);
    for (const ref of refs) parts.push({ kind: "ref", ref, label: `[${ref}]` });
    cursor = index + match[0].length;
  }
  if (cursor < text.length) parts.push({ kind: "text", text: text.slice(cursor) });
  return parts;
}

/** "S2.14" -> 2, the number of the source a passage belongs to. */
export function sourceNumber(ref: string): number | null {
  const match = /^S(\d+)\./.exec(ref);
  return match ? Number(match[1]) : null;
}
