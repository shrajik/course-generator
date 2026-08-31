"use client";

import { EditableText } from "./EditableText";
import { BlockLabel, EditableList, readText } from "./parts";
import { asStringArray } from "@/lib/editor/blocks";
import { accentColor } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

const SEGMENTS: Array<{ key: string; label: string }> = [
  { key: "context", label: "Context." },
  { key: "challenge", label: "Challenge." },
];

export function CaseStudyBlock({ block, editable, onEdit }: BlockViewProps) {
  const outcome = readText(block.content, "outcome");

  return (
    <div className="space-y-1.5">
      <BlockLabel
        text={readText(block.content, "title") || "Case Study"}
        color={accentColor(block)}
        editable={editable}
        onCommit={(value) => onEdit("title", value)}
      />
      {SEGMENTS.map(({ key, label }) => {
        const value = readText(block.content, key);
        if (!value) return null;
        return (
          <p key={key}>
            <span className="font-semibold">{label}</span>{" "}
            <EditableText
              as="span"
              value={value}
              editable={editable}
              onCommit={(next) => onEdit(key, next)}
            />
          </p>
        );
      })}
      {asStringArray(block.content.actions).length > 0 ? (
        <div>
          <p className="font-semibold">What they did</p>
          <EditableList
            content={block.content}
            path="actions"
            editable={editable}
            onEdit={onEdit}
          />
        </div>
      ) : null}
      {outcome ? (
        <p>
          <span className="font-semibold">Outcome.</span>{" "}
          <EditableText
            as="span"
            value={outcome}
            editable={editable}
            onCommit={(next) => onEdit("outcome", next)}
          />
        </p>
      ) : null}
      {asStringArray(block.content.lessons).length > 0 ? (
        <div>
          <p className="font-semibold">Lessons</p>
          <EditableList
            content={block.content}
            path="lessons"
            editable={editable}
            onEdit={onEdit}
          />
        </div>
      ) : null}
    </div>
  );
}
