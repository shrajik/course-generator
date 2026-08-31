"use client";

import { EditableText } from "./EditableText";
import { BlockLabel, readText } from "./parts";
import { accentColor } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

/** Serves `callout`, `tip` and `warning` - the same shape with different chrome. */
export function CalloutBlock({ block, editable, onEdit }: BlockViewProps) {
  const title = readText(block.content, "title");
  return (
    <div>
      <BlockLabel
        text={title}
        color={accentColor(block)}
        editable={editable}
        onCommit={(value) => onEdit("title", value)}
      />
      <EditableText
        as="div"
        value={readText(block.content, "text")}
        editable={editable}
        onCommit={(value) => onEdit("text", value)}
        placeholder="Callout text"
        className="whitespace-pre-wrap"
      />
    </div>
  );
}
