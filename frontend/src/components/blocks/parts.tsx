"use client";

import { EditableText } from "./EditableText";
import { asString, asStringArray } from "@/lib/editor/blocks";
import type { BlockContent } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";

/** Small uppercase accent label used by callouts, quizzes, summaries, etc. */
export function BlockLabel({
  text,
  color,
  editable,
  onCommit,
  className,
}: {
  text: string;
  color: string;
  editable: boolean;
  onCommit?: (value: string) => void;
  className?: string;
}) {
  if (!text) return null;
  const classes = cn(
    "mb-2 text-[11px] font-bold uppercase tracking-[0.09em]",
    className,
  );
  if (!editable || !onCommit) {
    return (
      <div className={classes} style={{ color }}>
        {text}
      </div>
    );
  }
  return (
    <EditableText
      as="div"
      value={text}
      editable
      multiline={false}
      onCommit={onCommit}
      className={classes}
      style={{ color }}
    />
  );
}

export function Caption({
  text,
  editable,
  onCommit,
}: {
  text: string;
  editable: boolean;
  onCommit: (value: string) => void;
}) {
  if (!text && !editable) return null;
  return (
    <EditableText
      as="div"
      value={text}
      editable={editable}
      multiline={false}
      onCommit={onCommit}
      placeholder="Caption"
      className="mt-1.5 text-[12.5px] text-ink-500"
    />
  );
}

/** An editable bulleted or numbered list backed by a string array. */
export function EditableList({
  content,
  path,
  editable,
  onEdit,
  ordered = false,
  muted = false,
}: {
  content: BlockContent;
  path: string;
  editable: boolean;
  onEdit: (path: string, value: unknown) => void;
  ordered?: boolean;
  muted?: boolean;
}) {
  const items = asStringArray(content[path]);
  if (items.length === 0) return null;
  const Tag = ordered ? "ol" : "ul";
  return (
    <Tag
      className={cn(
        "space-y-1 pl-5",
        ordered ? "list-decimal" : "list-disc",
        muted && "text-ink-500",
      )}
    >
      {items.map((item, index) => (
        <li key={index}>
          <EditableText
            as="span"
            value={item}
            editable={editable}
            multiline={false}
            onCommit={(value) => {
              if (value.trim() === "") {
                onEdit(
                  path,
                  items.filter((_, i) => i !== index),
                );
              } else {
                onEdit(`${path}.${index}`, value);
              }
            }}
          />
        </li>
      ))}
    </Tag>
  );
}

export function SubHeading({ text }: { text: string }) {
  return <p className="mb-1 mt-2 text-[13px] font-semibold text-ink">{text}</p>;
}

export function readText(content: BlockContent, key: string): string {
  return asString(content[key]);
}
