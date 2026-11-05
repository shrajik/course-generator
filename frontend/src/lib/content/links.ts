/**
 * Plain `http(s)://` URLs embedded in course text - specifically the
 * Sources page's "{title} — {url}" paragraphs (see
 * backend/app/course/document/builder.py's `_source_blocks`), which today
 * render as inert text with nothing to click.
 *
 * Mirrors `jargon.ts`'s own shape and discipline: a pure parser returning
 * typed segments, never raw HTML - the caller (LinkText) builds real React
 * elements from the segments, so nothing here is an injection vector even
 * though the source text is AI-generated.
 */

import type { TextSegment } from "./jargon.ts";

export interface LinkSegment {
  kind: "link";
  url: string;
}

export type LinkedSegment = TextSegment | LinkSegment;

// Scheme-required on purpose (never a bare "example.com" or "www.foo") -
// matches backend/app/render/html_renderer.py's `_LINK_URL_PATTERN` exactly,
// so both surfaces recognise the same set of strings as links.
const URL_PATTERN = /https?:\/\/[^\s<>"')\]]+/g;

// Trailing characters that read as sentence punctuation rather than part of
// the URL itself - stripped from the link and kept as plain text after it.
const TRAILING_PUNCTUATION = ".,;:!?";

function stripTrailingPunctuation(url: string): { url: string; trail: string } {
  let end = url.length;
  while (end > 0 && TRAILING_PUNCTUATION.includes(url[end - 1])) {
    end -= 1;
  }
  return { url: url.slice(0, end), trail: url.slice(end) };
}

/**
 * Split `text` into plain runs and recognised URLs, in order. Always
 * round-trips losslessly: concatenating every segment's text/url text
 * equals the original `text`.
 */
export function parseLinks(text: string): LinkedSegment[] {
  const source = text ?? "";
  const segments: LinkedSegment[] = [];
  let cursor = 0;

  URL_PATTERN.lastIndex = 0;
  for (let match = URL_PATTERN.exec(source); match !== null; match = URL_PATTERN.exec(source)) {
    if (match.index > cursor) {
      pushText(segments, source.slice(cursor, match.index));
    }
    const { url, trail } = stripTrailingPunctuation(match[0]);
    if (url) {
      segments.push({ kind: "link", url });
    } else {
      pushText(segments, match[0]); // pure punctuation matched - nothing to link
    }
    if (trail) pushText(segments, trail);
    cursor = match.index + match[0].length;
  }

  if (cursor < source.length) {
    pushText(segments, source.slice(cursor));
  }
  return segments;
}

function pushText(segments: LinkedSegment[], text: string): void {
  if (!text) return;
  const previous = segments[segments.length - 1];
  if (previous?.kind === "text") {
    previous.text += text;
    return;
  }
  segments.push({ kind: "text", text });
}
