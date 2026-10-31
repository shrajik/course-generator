"use client";

import { Fragment } from "react";
import { parseJargon } from "@/lib/content/jargon";
import { parseLinks } from "@/lib/content/links";

/**
 * Renders text that may contain `<jargon term="...">` markup and/or plain
 * `http(s)://` URLs (e.g. a Sources-page "{title} — {url}" line - see
 * backend/app/course/document/builder.py's `_source_blocks`).
 *
 * Built from parsed segments rather than injected HTML: the term/URL come
 * from generated course content, so `dangerouslySetInnerHTML` here would be
 * an injection vector. React escapes every segment for us. Link detection
 * runs only on the plain-text runs `parseJargon` leaves behind - a jargon
 * term's own label is never also scanned for URLs, keeping the two concerns
 * independent.
 *
 * Display only - the editor swaps this for the raw source the moment a block
 * becomes editable, so what gets saved is always the original markup. See
 * EditableText, lib/content/jargon.ts and lib/content/links.ts.
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
          <LinkedText key={index} text={segment.text} />
        ),
      )}
    </>
  );
}

function LinkedText({ text }: { text: string }) {
  const parts = parseLinks(text);
  return (
    <>
      {parts.map((part, index) =>
        part.kind === "link" ? (
          <a
            key={index}
            href={part.url}
            target="_blank"
            rel="noreferrer"
            // Matches the existing external-link convention (see
            // components/editor/QualityPanel.tsx) so a source link looks
            // and behaves the same wherever it appears.
            className="break-all text-brand-700 underline decoration-brand-300 underline-offset-2 hover:decoration-brand-600"
          >
            {part.url}
          </a>
        ) : (
          <Fragment key={index}>{part.text}</Fragment>
        ),
      )}
    </>
  );
}
