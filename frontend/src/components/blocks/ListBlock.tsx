"use client";

import { EditableText } from "./EditableText";
import { BlockLabel, EditableList, readText } from "./parts";
import { accentColor } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

/** Serves `learning_objectives` and `reflection`. */
export function ListBlock({ block, editable, onEdit }: BlockViewProps) {
  const intro = readText(block.content, "intro");
  return (
    <div>
      <BlockLabel
        text={readText(block.content, "title")}
        color={accentColor(block)}
        editable={editable}
        onCommit={(value) => onEdit("title", value)}
      />
      {intro ? (
        <EditableText
          as="div"
          value={intro}
          editable={editable}
          onCommit={(value) => onEdit("intro", value)}
          className="mb-1.5"
        />
      ) : null}
      <EditableList content={block.content} path="items" editable={editable} onEdit={onEdit} />
    </div>
  );
}
