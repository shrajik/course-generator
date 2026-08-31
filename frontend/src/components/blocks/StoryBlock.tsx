"use client";

import { EditableText } from "./EditableText";
import { readText } from "./parts";
import type { BlockViewProps } from "./types";

export function StoryBlock({ block, editable, onEdit }: BlockViewProps) {
  const title = readText(block.content, "title");
  const takeaway = readText(block.content, "takeaway");

  return (
    <div>
      {title ? (
        <EditableText
          as="div"
          value={title}
          editable={editable}
          multiline={false}
          onCommit={(value) => onEdit("title", value)}
          className="mb-1.5 text-[1.05em] font-bold"
        />
      ) : null}
      <EditableText
        as="div"
        value={readText(block.content, "text")}
        editable={editable}
        onCommit={(value) => onEdit("text", value)}
        placeholder="Story"
        className="whitespace-pre-wrap"
      />
      {takeaway ? (
        <p className="mt-2 text-[12.5px] text-ink-500">
          <span className="font-semibold">Takeaway:</span>{" "}
          <EditableText
            as="span"
            value={takeaway}
            editable={editable}
            onCommit={(value) => onEdit("takeaway", value)}
          />
        </p>
      ) : null}
    </div>
  );
}
