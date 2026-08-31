"use client";

import { EditableText } from "./EditableText";
import { readText } from "./parts";
import type { BlockViewProps } from "./types";

export function ParagraphBlock({ block, editable, onEdit }: BlockViewProps) {
  return (
    <EditableText
      as="div"
      value={readText(block.content, "text")}
      editable={editable}
      onCommit={(value) => onEdit("text", value)}
      placeholder="Paragraph text"
      className="whitespace-pre-wrap"
    />
  );
}
