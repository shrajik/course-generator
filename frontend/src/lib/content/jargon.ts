/**
 * `<jargon term="...">term</jargon>` markup.
 *
 * The writer wraps technical keywords in this tag when a course runs the
 * cross-domain track, so a reader can surface a definition on demand (client
 * UAT 5.2). It is **content**, and it stays in the stored Course Document -
 * the tag is only resolved at the edges:
 *
 * | Surface              | Shows                                    |
 * |----------------------|------------------------------------------|
 * | editor, display mode | the term, with a definition on hover     |
 * | editor, edit mode    | the raw source, so saving preserves it   |
 * | PDF export           | the term only (stripped server-side, see |
 * |                      | app/render/html_renderer.py)             |
 *
 * This module is the single definition of what the markup means, shared by
 * the editor, the preview and any future player, so the surfaces cannot
 * disagree about how to read it.
 */

export interface TextSegment {
  kind: "text";
  text: string;
}

export interface JargonSegment {
  kind: "jargon";
  /** The `term` attribute - the definition shown on hover. */
  term: string;
  /** The wrapped text, which is what the reader actually sees. */
  label: string;
}

export type ContentSegment = TextSegment | JargonSegment;

// Attribute-aware rather than `[^>]*`: a term can legitimately contain ">"
// (for example term="Focus > Busyness"), and a naive pattern would stop at
// that character and leave the rest of the tag visible. Mirrors
// `_JARGON_TAG` in backend/app/render/html_renderer.py.
const ATTRIBUTES = String.raw`(?:[^>"']|"[^"]*"|'[^']*')*`;

/** A complete `<jargon ...>...</jargon>` pair. */
const PAIR = new RegExp(
  String.raw`<jargon${ATTRIBUTES}>([\s\S]*?)<\/\s*jargon\s*>`,
  "gi",
);

/** Any jargon tag at all, including an unclosed or orphaned one. */
const ANY_TAG = new RegExp(String.raw`<\/?\s*jargon\b${ATTRIBUTES}>`, "gi");

/** `term="..."` or `term='...'` inside an opening tag. */
const TERM_ATTRIBUTE = /\bterm\s*=\s*(?:"([^"]*)"|'([^']*)')/i;

/** Remove every jargon tag, keeping the text it wrapped. */
export function stripJargon(text: string): string {
  return (text ?? "").replace(ANY_TAG, "");
}

export function hasJargon(text: string): boolean {
  ANY_TAG.lastIndex = 0;
  return ANY_TAG.test(text ?? "");
}

/**
 * Split `text` into plain runs and jargon terms, in order.
 *
 * Always round-trips losslessly through the visible text: concatenating
 * every segment's visible text equals `stripJargon(text)`. Malformed or
 * orphaned tags are dropped rather than shown, so nothing of this shape can
 * reach a reader whatever the writer emitted.
 */
export function parseJargon(text: string): ContentSegment[] {
  const source = text ?? "";
  const segments: ContentSegment[] = [];
  let cursor = 0;

  PAIR.lastIndex = 0;
  for (let match = PAIR.exec(source); match !== null; match = PAIR.exec(source)) {
    if (match.index > cursor) {
      pushText(segments, source.slice(cursor, match.index));
    }
    const attributes = TERM_ATTRIBUTE.exec(match[0]);
    // A nested or stray tag inside the label would otherwise render as text.
    const label = stripJargon(match[1]);
    const term = (attributes?.[1] ?? attributes?.[2] ?? "").trim();
    if (label) {
      // Without a term there is nothing to define, so it is only plain text.
      segments.push(term ? { kind: "jargon", term, label } : { kind: "text", text: label });
    }
    cursor = match.index + match[0].length;
  }

  if (cursor < source.length) {
    pushText(segments, source.slice(cursor));
  }
  return segments;
}

function pushText(segments: ContentSegment[], raw: string): void {
  const text = stripJargon(raw);
  if (!text) return;
  const previous = segments[segments.length - 1];
  if (previous?.kind === "text") {
    previous.text += text;
    return;
  }
  segments.push({ kind: "text", text });
}
