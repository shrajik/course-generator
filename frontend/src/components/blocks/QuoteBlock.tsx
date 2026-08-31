"use client";

import { EditableText } from "./EditableText";
import { readText } from "./parts";
import { accentColor } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

export function QuoteBlock({ block, editable, onEdit }: BlockViewProps) {
  const attribution = readText(block.content, "attribution");
  return (
    <blockquote
      className="pl-3.5"
      style={{ borderLeft: `4px solid ${accentColor(block)}` }}
    >
      <EditableText
        as="div"
        value={readText(block.content, "text")}
        editable={editable}
        onCommit={(value) => onEdit("text", value)}
        placeholder="Quotation"
        className="whitespace-pre-wrap"
      />
      {attribution || editable ? (
        <EditableText
          as="div"
          value={attribution ? `— ${attribution}` : ""}
          editable={editable}
          multiline={false}
          onCommit={(value) => onEdit("attribution", value.replace(/^—\s*/, ""))}
          placeholder="— Attribution"
          className="mt-1.5 text-[12.5px] not-italic text-ink-500"
        />
      ) : null}
    </blockquote>
  );
}
