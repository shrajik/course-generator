"use client";

import { EditableText } from "./EditableText";
import { BlockLabel, EditableList, readText } from "./parts";
import { asStringArray } from "@/lib/editor/blocks";
import { accentColor } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

/** Serves `exercise` and `challenge`. */
export function ExerciseBlock({ block, editable, onEdit }: BlockViewProps) {
  const difficulty = readText(block.content, "difficulty");
  const title = readText(block.content, "title") || "Exercise";
  const outcome = readText(block.content, "expected_outcome");

  return (
    <div>
      <BlockLabel
        text={difficulty ? `${title} · ${difficulty}` : title}
        color={accentColor(block, "#15803D")}
        editable={false}
      />
      <EditableText
        as="div"
        value={readText(block.content, "instructions")}
        editable={editable}
        onCommit={(value) => onEdit("instructions", value)}
        placeholder="Instructions"
        className="whitespace-pre-wrap"
      />
      <div className="mt-1.5">
        <EditableList
          content={block.content}
          path="steps"
          editable={editable}
          onEdit={onEdit}
          ordered
        />
      </div>
      {asStringArray(block.content.hints).length > 0 ? (
        <div className="mt-2">
          <p className="font-semibold text-ink-500">Hints:</p>
          <EditableList
            content={block.content}
            path="hints"
            editable={editable}
            onEdit={onEdit}
            muted
          />
        </div>
      ) : null}
      {outcome ? (
        <p className="mt-2 text-[12.5px] text-ink-500">
          <span className="font-semibold">Expected outcome:</span>{" "}
          <EditableText
            as="span"
            value={outcome}
            editable={editable}
            onCommit={(value) => onEdit("expected_outcome", value)}
          />
        </p>
      ) : null}
    </div>
  );
}
