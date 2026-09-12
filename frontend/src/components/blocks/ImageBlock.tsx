"use client";

import { ImageOff } from "lucide-react";
import { Caption, readText } from "./parts";
import { assetUrl } from "@/lib/api/documents";
import { imageBoxHeight } from "@/lib/editor/style";
import { isSvgPath, useInlineDiagramSvg } from "@/lib/editor/useInlineDiagramSvg";
import type { BlockViewProps } from "./types";

/** Diagrams are generated as SVG with built-in hover/focus tooltips and
 * highlighting (see backend app/render/diagram_renderer.py) - but that only
 * works once the markup lives in this page's own DOM, not behind `<img
 * src>`. So a diagram is fetched and inlined; every other image (a raster
 * illustration) keeps using a plain `<img>`, unchanged. */
export function ImageBlock({ block, documentId, editable, onEdit }: BlockViewProps) {
  const path = typeof block.content.path === "string" ? block.content.path : null;
  const source = assetUrl(documentId, path);
  const boxHeight = imageBoxHeight(block);
  const error = readText(block.content, "error");
  const wantsInline = readText(block.content, "kind") === "diagram" && isSvgPath(path);
  const inlineMarkup = useInlineDiagramSvg(documentId, path, wantsInline);

  return (
    <div>
      {source ? (
        <div
          className="flex items-center justify-center overflow-hidden"
          style={{ height: boxHeight }}
        >
          {inlineMarkup ? (
            <div
              className="diagram-inline flex h-full w-full items-center justify-center [&>svg]:h-auto [&>svg]:max-h-full [&>svg]:w-auto [&>svg]:max-w-full"
              role="img"
              aria-label={readText(block.content, "alt")}
              // Trusted source: this markup is our own backend-rendered SVG
              // (app/render/diagram_renderer.py), not user-supplied HTML.
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
