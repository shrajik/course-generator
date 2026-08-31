"use client";

import { BlockLabel, EditableList, readText } from "./parts";
import { asStringArray } from "@/lib/editor/blocks";
import { accentColor } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

export function SummaryBlock({ block, editable, onEdit }: BlockViewProps) {
  return (
    <div>
      <BlockLabel
        text={readText(block.content, "title") || "Summary"}
        color={accentColor(block)}
        editable={editable}
        onCommit={(value) => onEdit("title", value)}
      />
      <EditableList
        content={block.content}
        path="key_takeaways"
        editable={editable}
        onEdit={onEdit}
      />
      {asStringArray(block.content.next_steps).length > 0 ? (
        <div className="mt-2">
          <p className="font-semibold text-ink-500">Next</p>
          <EditableList
            content={block.content}
            path="next_steps"
            editable={editable}
            onEdit={onEdit}
            muted
          />
        </div>
      ) : null}
    </div>
  );
}
