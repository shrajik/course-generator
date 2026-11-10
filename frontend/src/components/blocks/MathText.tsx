"use client";

import { useMemo } from "react";
import katex from "katex";

/**
 * One `$...$`/`$$...$$` math span, typeset with real KaTeX - the same
 * engine and the same delimiters `course.html.j2`'s inline script already
 * uses for the exported PDF (see lib/content/math.ts's own docstring), so
 * the editor/preview now show the identical rendering the PDF always did.
 *
 * `katex.renderToString` is the only place in this component that builds
 * raw HTML: it is a trusted library (not arbitrary user HTML), and
 * `trust: false` (the default) keeps it from honouring anything like a
 * `\href` even if an AI-authored source string tried to smuggle one in.
 * `throwOnError: false` renders KaTeX's own inline `.katex-error` span
 * instead of crashing the block - app.services.math_qa's generation-time QA
 * is what actually catches an unparsceable formula; this is just the
 * display-time fallback so one bad formula never blanks a whole page.
 */
export function MathText({ tex, display }: { tex: string; display: boolean }) {
  const html = useMemo(
    () =>
      katex.renderToString(tex, {
        throwOnError: false,
        displayMode: display,
        trust: false,
        strict: "ignore",
      }),
    [tex, display],
  );

  // Always a <span>: KaTeX's own output already wraps display-mode math in
  // its own `class="katex-display"` span (block-level via its stylesheet),
  // so this never needs to be a <div> itself - which matters because this
  // component renders inside arbitrary inline/text contexts (a <p>, a
  // <li>, a table cell) where a real <div> would be invalid HTML.
  return <span dangerouslySetInnerHTML={{ __html: html }} />;
}
