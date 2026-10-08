"use client";

import type { WrapAround } from "@/lib/editor/pairing";
import { blockBoxStyle } from "@/lib/editor/style";
import type { Block } from "@/lib/types/document";
import { getBlockRenderer } from "./registry";

interface BlockRendererProps {
  block: Block;
  documentId: string;
  editable: boolean;
  onEdit?: (path: string, value: unknown) => void;
  wrapAround?: WrapAround | null;
}

/** Positions a block on the page and delegates its inner markup to the registry. */
export function BlockRenderer({
  block,
  documentId,
  editable,
  onEdit,
  wrapAround,
}: BlockRendererProps) {
  const View = getBlockRenderer(block.type);
  return (
    <div style={blockBoxStyle(block)} data-block-type={block.type}>
      <View
        block={block}
        documentId={documentId}
        editable={editable}
        onEdit={onEdit ?? (() => undefined)}
        wrapAround={wrapAround}
      />
    </div>
  );
}
