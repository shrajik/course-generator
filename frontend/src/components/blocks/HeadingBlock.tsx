"use client";

import { EditableText } from "./EditableText";
import { readText } from "./parts";
import type { BlockViewProps } from "./types";

export function HeadingBlock({ block, editable, onEdit }: BlockViewProps) {
  const level = Number(block.content.level) || 2;
  const tag = level <= 1 ? "h1" : level === 2 ? "h2" : level === 3 ? "h3" : "h4";
  return (
    <EditableText
      as={tag}
      value={readText(block.content, "text")}
      editable={editable}
      multiline={false}
      onCommit={(value) => onEdit("text", value)}
      placeholder="Heading"
      className="font-bold tracking-[-0.01em]"
      style={{ lineHeight: block.style.line_height ?? 1.25 }}
    />
  );
}
