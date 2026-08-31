"use client";

import { ImageOff } from "lucide-react";
import { Caption, readText } from "./parts";
import { assetUrl } from "@/lib/api/documents";
import { imageBoxHeight } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

export function ImageBlock({ block, documentId, editable, onEdit }: BlockViewProps) {
  const path = typeof block.content.path === "string" ? block.content.path : null;
  const source = assetUrl(documentId, path);
  const boxHeight = imageBoxHeight(block);
  const error = readText(block.content, "error");

  return (
    <div>
      {source ? (
        <div
          className="flex items-center justify-center overflow-hidden"
          style={{ height: boxHeight }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={source}
            alt={readText(block.content, "alt")}
            className="block h-auto max-h-full w-auto max-w-full"
            draggable={false}
          />
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
