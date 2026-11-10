"use client";

import { useRef, useState } from "react";
import { ImageOff } from "lucide-react";
import { Caption, readText } from "./parts";
import { assetUrl } from "@/lib/api/documents";
import { isSectionIntroImage } from "@/lib/editor/pairing";
import { nextLabelId, pctFromPointer } from "@/lib/editor/overlayLabels";
import { imageBoxHeight } from "@/lib/editor/style";
import { isHtmlPath, isSvgPath, useInlineDiagramSvg } from "@/lib/editor/useInlineDiagramSvg";
import { useMathTypeset } from "@/lib/editor/useMathTypeset";
import type { OverlayLabel } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";
import type { BlockViewProps } from "./types";

const OVERLAY_STYLE_CLASS: Record<OverlayLabel["style"], string> = {
  title: "text-[13px] font-bold px-2.5 py-1",
  callout: "text-[11.5px] font-medium px-2 py-1",
  label: "text-[11px] font-medium px-1.5 py-0.5",
};

/** Edit Visuals Part 2 - a text-free illustration's own rendered rect
 * (contain-fit, via CSS `aspect-ratio` - never computed in JS, see
 * OverlayLabel's own docstring for why), with the overlay labels drawn
 * directly on top and, when `editable`, draggable to a new position. The
 * exported PDF uses the identical CSS, in real Chromium (Playwright) -
 * see course.html.j2's matching `.figure-frame`. */
function IllustrationWithOverlay({
  source,
  alt,
  width,
  height,
  labels,
  editable,
  onLabelsChange,
}: {
  source: string;
  alt: string;
  width: number;
  height: number;
  labels: OverlayLabel[];
  editable: boolean;
  onLabelsChange: (next: OverlayLabel[]) => void;
}) {
  const frameRef = useRef<HTMLDivElement>(null);
  const [dragId, setDragId] = useState<string | null>(null);

  const movePointer = (event: React.PointerEvent) => {
    if (!dragId || !frameRef.current) return;
    const { x_pct, y_pct } = pctFromPointer(event.clientX, event.clientY, frameRef.current.getBoundingClientRect());
    onLabelsChange(labels.map((label) => (label.id === dragId ? { ...label, x_pct, y_pct } : label)));
  };

  return (
    <div
      ref={frameRef}
      className="relative block max-h-full max-w-full"
      style={{ aspectRatio: `${width} / ${height}` }}
      onPointerMove={movePointer}
      onPointerUp={() => setDragId(null)}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={source} alt={alt} className="block h-full w-full" draggable={false} />
      {labels.map((label) => (
        <div
          key={label.id}
          onPointerDown={(event) => {
            if (!editable) return;
            event.stopPropagation();
            (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
            setDragId(label.id);
          }}
          className={cn(
            "absolute -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-full border bg-white/95 shadow-[0_1px_3px_rgba(0,0,0,0.15)]",
            OVERLAY_STYLE_CLASS[label.style],
            editable ? "cursor-move select-none" : "pointer-events-none",
          )}
          style={{
            left: `${label.x_pct}%`,
            top: `${label.y_pct}%`,
            borderColor: label.color || "#e5e7eb",
            color: label.color || undefined,
          }}
        >
          {label.text}
        </div>
      ))}
    </div>
  );
}

/** Diagrams are generated as SVG with built-in hover/focus tooltips (see
 * backend app/render/diagram_renderer.py) - but that only works once the
 * markup lives in this page's own DOM, not behind `<img src>`. So a diagram
 * is fetched and inlined; a concept_experience block (backend
 * app/render/concept_experience_renderer.py) or a toc block (backend
 * app/render/toc_renderer.py) is a self-contained but deliberately static
 * HTML+CSS fragment (no script, no buttons) - an `<img src="foo.html">`
 * can't render it at all, so it's fetched and inlined the same way. Every
 * other image (a raster illustration) keeps using a plain `<img>`,
 * unchanged. */

const _FLEXIBLE_HEIGHT_KINDS = new Set(["concept_experience", "toc"]);

export function ImageBlock({ block, documentId, editable, onEdit }: BlockViewProps) {
  const path = typeof block.content.path === "string" ? block.content.path : null;
  const source = assetUrl(documentId, path);
  const boxHeight = imageBoxHeight(block);
  const error = readText(block.content, "error");
  // Fallback-cascade root-cause fix: a QA-withheld visual (every attempt at
  // the real brief failed its checks and no Wikimedia match existed - see
  // ImageService.generate_for_block) has `error` unset (it is not a
  // technical failure), so without this it fell into the same "Image not
  // generated yet" copy as a block the pipeline simply hasn't reached yet -
  // misleadingly implying generation is still pending rather than already
  // attempted and deliberately withheld.
  const omittedAfterQaFailure = Boolean(block.content.omitted_after_qa_failure);
  const kind = readText(block.content, "kind");
  const isFlexibleHeightHtml = _FLEXIBLE_HEIGHT_KINDS.has(kind) && isHtmlPath(path);
  const wantsInline = (kind === "diagram" && isSvgPath(path)) || isFlexibleHeightHtml;
  const inlineMarkup = useInlineDiagramSvg(documentId, path, wantsInline);
  const inlineRef = useRef<HTMLDivElement | null>(null);
  // A concept_experience/toc fragment can contain genuine `$...$` LaTeX (a
  // physics/math concept's own formula) - see useMathTypeset's own
  // docstring for why this HTML fragment needs its own typesetting pass,
  // distinct from <MathText> which only ever wraps parsed text segments.
  useMathTypeset(inlineRef, inlineMarkup);
  const intrinsicWidth = typeof block.content.width === "number" ? block.content.width : null;
  const intrinsicHeight = typeof block.content.height === "number" ? block.content.height : null;
  const overlayLabels = Array.isArray(block.content.overlay_labels)
    ? (block.content.overlay_labels as OverlayLabel[])
    : [];
  const wantsOverlay = kind === "illustration" && intrinsicWidth && intrinsicHeight;

  return (
    <div>
      {source ? (
        <div
          className={
            isFlexibleHeightHtml
              ? // A concept_experience/toc fragment's own width is computed
                // server-side from its content (entity/satellite count - see
                // app/render/concept_experience_renderer.py), never from the
                // viewport, so it can legitimately be wider than a narrow
                // screen's own column - confirmed via a real Chromium
                // measurement (see tests/test_concept_visuals.py's
                // TestConceptMapRealBrowserGeometry narrow-viewport test:
                // a dense concept map renders ~612px wide regardless of a
                // 375px viewport). `overflow-x-auto` turns that into a
                // horizontal scroll on narrow screens instead of an
                // unbounded bleed past this block's own column - a minimal,
                // backward-compatible fix (a desktop-width container that
                // already fits never shows a scrollbar).
                "flex items-center justify-center overflow-x-auto"
              : "flex items-center justify-center overflow-hidden"
          }
          // imageBoxHeight caps at a fixed picture-sized box - fine for a
          // raster illustration or a diagram SVG (both fit a bounded aspect
          // ratio), but a concept_experience/toc visual's real height varies
          // a lot with its content (entity/chapter count, wrapped rows) and
          // would get silently clipped by a fixed overflow:hidden box; let
          // it size to its own content instead.
          style={isFlexibleHeightHtml ? undefined : { height: boxHeight }}
        >
          {inlineMarkup ? (
            <div
              ref={inlineRef}
              className={
                isFlexibleHeightHtml
                  ? "cev-block-inline w-full"
                  : "diagram-inline flex h-full w-full items-center justify-center [&>svg]:h-auto [&>svg]:max-h-full [&>svg]:w-auto [&>svg]:max-w-full"
              }
              role="img"
              aria-label={readText(block.content, "alt")}
              // Trusted source: this markup is our own backend-rendered SVG
              // (app/render/diagram_renderer.py), concept_experience
              // fragment (app/render/concept_experience_renderer.py) or toc
              // fragment (app/render/toc_renderer.py), not user-supplied HTML.
              dangerouslySetInnerHTML={{ __html: inlineMarkup }}
            />
          ) : wantsOverlay && intrinsicWidth && intrinsicHeight ? (
            <IllustrationWithOverlay
              source={source}
              alt={readText(block.content, "alt")}
              width={intrinsicWidth}
              height={intrinsicHeight}
              labels={overlayLabels}
              editable={editable}
              onLabelsChange={(next) => onEdit("overlay_labels", next)}
            />
          ) : (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={source}
              alt={readText(block.content, "alt")}
              className="block h-auto max-h-full w-auto max-w-full"
              draggable={false}
            />
          )}
        </div>
      ) : (
        <div
          className="flex items-center justify-center gap-2 overflow-hidden rounded-[8px] border border-dashed border-line bg-canvas px-3 text-center"
          // Exactly the reserved height. A failed picture reserves only a
          // one-line strip (see imageGenerationFailed); one not generated yet
          // keeps its full slot until it is.
          style={{ height: boxHeight }}
          title={
            error
              ? `Image failed: ${error}`
              : omittedAfterQaFailure
                ? "Every attempt at this image failed quality checks and no suitable Wikimedia match was " +
                  "found - withheld rather than publishing an unrelated generic picture. See this block's " +
                  "quality findings, then regenerate or replace it."
                : undefined
          }
        >
          <ImageOff size={16} className="shrink-0 text-ink-300" aria-hidden />
          <p className="truncate text-[12.5px] text-ink-500">
            {error
              ? "Visual unavailable"
              : omittedAfterQaFailure
                ? "Visual withheld - needs review"
                : "Image not generated yet"}
          </p>
        </div>
      )}
      {/* A section-intro illustration floats beside its paragraph in the PDF,
          which draws no caption for it; drawing one here would also overflow
          the slot reserved for it. */}
      {isSectionIntroImage(block) ? null : (
        <Caption
          text={readText(block.content, "caption")}
          editable={editable}
          onCommit={(value) => onEdit("caption", value)}
        />
      )}
      {!isSectionIntroImage(block) && readText(block.content, "image_source") === "wikimedia" ? (
        <WikimediaAttribution block={block} />
      ) : null}
    </div>
  );
}

/** A real, properly-licensed Wikimedia Commons image was used instead of an
 * AI-generated one (see backend ImageService._try_wikimedia_image) - shown
 * directly under the existing caption, never changing the image card's own
 * size/layout. `attribution_text` is pre-formatted server-side from
 * Commons' own metadata (see WikimediaCandidate.attribution_text) - never
 * fabricated here. */
function WikimediaAttribution({ block }: { block: BlockViewProps["block"] }) {
  const text = readText(block.content, "attribution_text");
  if (!text) return null;
  const sourceUrl = readText(block.content, "source_url");
  return (
    <p className="mt-1 text-[11px] text-ink-400">
      {sourceUrl ? (
        <a
          href={sourceUrl}
          target="_blank"
          rel="noreferrer"
          className="underline decoration-ink-300 underline-offset-2 hover:text-brand-700 hover:decoration-brand-300"
        >
          {text}
        </a>
      ) : (
        text
      )}
    </p>
  );
}
