"use client";

import { Fragment, createContext, useContext } from "react";
import { originLabel, originOf } from "@/lib/editor/citations";
import { splitMarkers } from "@/lib/review/highlight";
import { cn } from "@/lib/utils/cn";

/**
 * Lets a citation badge anywhere in the course open its passage. Provided by
 * the editor; where nothing provides it (a read-only preview) the badge is
 * still shown and labelled, just not clickable.
 */
export const CitationContext = createContext<{ open: (ref: string) => void } | null>(null);

export function CitationBadge({ refId, label }: { refId: string; label: string }) {
  const context = useContext(CitationContext);
  const origin = originOf(refId);
  const title = `${originLabel(origin)}, passage ${refId}${context ? " - click to see where this comes from" : ""}`;
  const className = cn(
    "mx-[1px] inline-flex items-center rounded-[4px] px-1 align-baseline text-[0.72em] font-semibold leading-[1.5]",
    origin === "knowledge" ? "bg-teal-50 text-teal-700 ring-1 ring-teal-200" : "bg-brand-50 text-brand-700 ring-1 ring-brand-200",
  );
  if (!context) {
    return (
      <span className={className} title={title} data-citation={refId}>
        {label}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={cn(className, "cursor-pointer hover:brightness-95")}
      title={title}
      data-citation={refId}
      // Opening a source must not select or start dragging the block.
      onPointerDown={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        context.open(refId);
      }}
    >
      {label}
    </button>
  );
}

/** Plain text with any source markers shown as citation badges. */
export function CitedText({ value }: { value: string }) {
  if (!value.includes("[")) return <>{value}</>;
  return (
    <>
      {splitMarkers(value).map((part, index) =>
        part.kind === "ref" ? (
          <CitationBadge key={index} refId={part.ref} label={part.label} />
        ) : (
          <Fragment key={index}>{part.text}</Fragment>
        ),
      )}
    </>
  );
}
