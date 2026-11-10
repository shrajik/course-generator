"use client";

import { useEffect, type RefObject } from "react";
import renderMathInElement from "katex/contrib/auto-render";

// The exact same four delimiter forms, in the exact same order, as
// `course.html.j2`'s own inline script (app/render/templates/course.html.j2)
// uses for the exported PDF, and `lib/content/math.ts`'s `MATH_PATTERN` uses
// for ordinary text blocks - a concept_experience/toc/diagram fragment is
// neither (it's raw backend-rendered HTML set via `dangerouslySetInnerHTML`,
// see useInlineDiagramSvg.ts), so nothing already typesets math inside it.
const MATH_DELIMITERS = [
  { left: "$$", right: "$$", display: true },
  { left: "$", right: "$", display: false },
  { left: "\\[", right: "\\]", display: true },
  { left: "\\(", right: "\\)", display: false },
];

/**
 * Typesets any `$...$`/`$$...$$`/`\(...\)`/`\[...\]` LaTeX inside `ref`'s
 * current DOM node with real KaTeX, mirroring exactly what the exported PDF
 * already does for the whole page (see this file's own `MATH_DELIMITERS`
 * comment) - closes a real, confirmed gap: a concept_experience field that
 * contains a genuine formula (e.g. a physics/math "Separation (spherical
 * coordinates)" diagram's own equation) renders correctly in the PDF but,
 * without this, showed the raw, un-typeset `$R_\ell(r)$` source text in the
 * live editor/preview, since `dangerouslySetInnerHTML`'d markup is never
 * passed through `<MathText>` (that component only wraps ordinary parsed
 * text segments, never arbitrary pre-rendered HTML).
 *
 * A no-op when `markup` is null/has no math delimiters (katex/contrib/auto-
 * render's own cheap scan already short-circuits that) or while `ref` isn't
 * mounted yet. Re-runs whenever `markup` changes (a freshly (re)generated
 * fragment), not on every render.
 */
export function useMathTypeset(ref: RefObject<HTMLElement | null>, markup: string | null): void {
  useEffect(() => {
    if (!markup || !ref.current) return;
    try {
      renderMathInElement(ref.current, {
        delimiters: MATH_DELIMITERS,
        throwOnError: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      });
    } catch {
      // A malformed fragment must never break the surrounding page - the
      // same "never crash the block" contract MathText.tsx's own
      // `throwOnError: false` already keeps for ordinary text math.
    }
  }, [ref, markup]);
}
