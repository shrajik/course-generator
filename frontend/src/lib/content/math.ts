/**
 * `$...$` / `$$...$$` / `\(...\)` / `\[...\]` LaTeX math spans embedded in
 * course text.
 *
 * Mirrors the PDF export path exactly: `app.render.html_renderer`'s own
 * module docstring and `course.html.j2`'s inline script typeset these same
 * four delimiter forms client-side via KaTeX's `renderMathInElement` - this
 * module recognises the identical delimiters so the editor/preview surfaces
 * stop showing raw `$K = \frac{1}{2} m v^2$` source text where the PDF
 * already rendered real math.
 *
 * Mirrors `jargon.ts`'s/`links.ts`'s own shape and discipline: a pure parser
 * returning typed segments, never raw HTML - the caller (MathText) is the
 * only place that turns a segment into a real DOM node, via KaTeX's own
 * `renderToString` (never `dangerouslySetInnerHTML` of anything else).
 */

import type { TextSegment } from "./jargon.ts";

export interface MathSegment {
  kind: "math";
  tex: string;
  display: boolean;
}

export type MathedSegment = TextSegment | MathSegment;

// Alternation order matters: the two-dollar/display form must be tried
// before the single-dollar/inline form at each position, or `$$x$$` would
// be read as two adjacent empty-ish inline matches instead of one display
// one. Inline forms exclude newlines and a bare `$` from their own body so
// an unmatched stray dollar sign (plain currency text, not math) never
// swallows the rest of a paragraph looking for its closing delimiter.
const MATH_PATTERN =
  /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\$([^\n$]+?)\$|\\\(([^\n]+?)\\\)/g;

/**
 * Split `text` into plain runs and math spans, in order. Always round-trips
 * losslessly: concatenating every segment's visible text/tex equals the
 * original `text` (a math segment's own delimiters are consumed, same as a
 * link's URL consumes nothing extra).
 */
export function parseMath(text: string): MathedSegment[] {
  const source = text ?? "";
  const segments: MathedSegment[] = [];
  let cursor = 0;

  MATH_PATTERN.lastIndex = 0;
  for (let match = MATH_PATTERN.exec(source); match !== null; match = MATH_PATTERN.exec(source)) {
    if (match.index > cursor) {
      pushText(segments, source.slice(cursor, match.index));
    }
    const [, dollarDisplay, bracketDisplay, dollarInline, parenInline] = match;
    const tex = dollarDisplay ?? bracketDisplay ?? dollarInline ?? parenInline ?? "";
    const display = dollarDisplay !== undefined || bracketDisplay !== undefined;
    if (tex.trim()) {
      segments.push({ kind: "math", tex, display });
    } else {
      pushText(segments, match[0]); // an empty "$$" etc. is not real math - keep it as plain text
    }
    cursor = match.index + match[0].length;
  }

  if (cursor < source.length) {
    pushText(segments, source.slice(cursor));
  }
  return segments;
}

function pushText(segments: MathedSegment[], text: string): void {
  if (!text) return;
  const previous = segments[segments.length - 1];
  if (previous?.kind === "text") {
    previous.text += text;
    return;
  }
  segments.push({ kind: "text", text });
}
