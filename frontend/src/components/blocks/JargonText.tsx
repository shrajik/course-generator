"use client";

import { Fragment } from "react";
import { parseJargon } from "@/lib/content/jargon";
import { splitMarkers } from "@/lib/review/highlight";
import { CitationBadge } from "./CitationBadge";

/**
 * Renders text that may contain `<jargon term="...">` markup and source
 * markers such as `[G2.5]` (shown as citation badges).
 *
 * Built from parsed segments rather than injected HTML: the term and its
 * definition come from generated course content, so `dangerouslySetInnerHTML`
 * here would be an injection vector. React escapes every segment for us.
 *
 * Display only - the editor swaps this for the raw source the moment a block
 * becomes editable, so what gets saved is always the original markup. See
 * EditableText and lib/content/jargon.ts.
 */
export function JargonText({ value }: { value: string }) {
  const segments = parseJargon(value);

  return (
    <>
      {segments.map((segment, index) =>
        segment.kind === "jargon" ? (
          <span
            key={index}
            title={segment.term}
            // `cursor-help` plus the dotted rule is the conventional signal
            // that hovering reveals a definition.
            className="cursor-help underline decoration-dotted decoration-ink-300 underline-offset-2"
            data-jargon={segment.term}
          >
            {segment.label}
          </span>
        ) : (
          <Fragment key={index}>
            {/* Source markers ([G2.5] knowledge library, [S1.3] the creator's own
                upload) become badges that say where the statement comes from. */}
            {segment.text.includes("[")
              ? splitMarkers(segment.text).map((part, i) =>
                  part.kind === "ref" ? (
                    <CitationBadge key={i} refId={part.ref} label={part.label} />
                  ) : (
                    <Fragment key={i}>{part.text}</Fragment>
                  ),
                )
              : segment.text}
          </Fragment>
        ),
      )}
    </>
  );
}
