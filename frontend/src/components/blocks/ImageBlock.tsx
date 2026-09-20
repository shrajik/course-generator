"use client";

import { ImageOff } from "lucide-react";
import { Caption, readText } from "./parts";
import { assetUrl } from "@/lib/api/documents";
import { imageBoxHeight } from "@/lib/editor/style";
import { isHtmlPath, isSvgPath, useInlineDiagramSvg } from "@/lib/editor/useInlineDiagramSvg";
import type { BlockViewProps } from "./types";

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
  const kind = readText(block.content, "kind");
  const isFlexibleHeightHtml = _FLEXIBLE_HEIGHT_KINDS.has(kind) && isHtmlPath(path);
  const wantsInline = (kind === "diagram" && isSvgPath(path)) || isFlexibleHeightHtml;
  const inlineMarkup = useInlineDiagramSvg(documentId, path, wantsInline);

  return (
    <div>
      {source ? (
        <div
          className={
            isFlexibleHeightHtml
              ? "flex items-center justify-center"
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
          className="flex flex-col items-center justify-center gap-2 rounded-[8px] border border-dashed border-line bg-canvas px-4 text-center"
          style={{ minHeight: Math.max(boxHeight, 160) }}
        >
          <ImageOff size={18} className="text-ink-300" aria-hidden />
          <p className="text-[12.5px] text-ink-500">
            {error ? `Image failed: ${error}` : "Image not generated yet"}
          </p>
          <p className="max-w-[420px] text-[11.5px] text-ink-400">
            {readText(block.content, "purpose") || readText(block.content, "prompt")}
          </p>
        </div>
      )}
      <Caption
        text={readText(block.content, "caption")}
        editable={editable}
        onCommit={(value) => onEdit("caption", value)}
      />
    </div>
  );
}
