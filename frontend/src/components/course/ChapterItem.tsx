"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp, GripVertical, Plus, Trash2 } from "lucide-react";
import { IconButton } from "@/components/ui/IconButton";
import { cn } from "@/lib/utils/cn";
import type { TocItem } from "@/lib/types/course";
import { SectionItem } from "./SectionItem";

export interface DragState {
  kind: "chapter" | "section";
  chapterIndex: number;
  index: number;
}

interface ChapterItemProps {
  chapter: TocItem;
  index: number;
  expanded: boolean;
  drag: DragState | null;
  dropTarget: DragState | null;
  onToggle: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
  onAddSection: () => void;
  onRenameSection: (sectionIndex: number, title: string) => void;
  onDeleteSection: (sectionIndex: number) => void;
  onDragStart: (state: DragState) => void;
  onDragOver: (state: DragState) => void;
  onDrop: () => void;
  onDragEnd: () => void;
}

export function ChapterItem({
  chapter,
  index,
  expanded,
  drag,
  dropTarget,
  onToggle,
  onRename,
  onDelete,
  onAddSection,
  onRenameSection,
  onDeleteSection,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: ChapterItemProps) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(chapter.title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => setValue(chapter.title), [chapter.title]);
  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const commit = () => {
    const next = value.trim();
    onRename(next || chapter.title);
    if (!next) setValue(chapter.title);
    setEditing(false);
  };

  const isDragging = drag?.kind === "chapter" && drag.index === index;
  const isChapterDropTarget =
    dropTarget?.kind === "chapter" && dropTarget.index === index && !isDragging;

  return (
    <div
      className={cn(
        "overflow-hidden rounded-card border bg-white transition-colors",
        isChapterDropTarget ? "border-brand-500" : "border-line",
        isDragging && "dragging",
      )}
      onDragOver={(event) => {
        event.preventDefault();
        if (drag?.kind === "chapter") onDragOver({ kind: "chapter", chapterIndex: index, index });
      }}
      onDrop={(event) => {
        event.preventDefault();
        onDrop();
      }}
    >
      <div
        draggable={!editing}
        onDragStart={(event) => {
          event.dataTransfer.effectAllowed = "move";
          onDragStart({ kind: "chapter", chapterIndex: index, index });
        }}
        onDragEnd={onDragEnd}
        className="flex items-center gap-2.5 px-2.5 py-2.5"
      >
        <GripVertical
          size={14}
          className="shrink-0 cursor-grab text-ink-300 active:cursor-grabbing"
          aria-hidden
        />
        <span className="w-5 shrink-0 text-[12px] font-semibold tabular-nums text-ink-400">
          {String(index + 1).padStart(2, "0")}
        </span>

        {editing ? (
          <input
            ref={inputRef}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onBlur={commit}
            onKeyDown={(event) => {
              if (event.key === "Enter") commit();
              if (event.key === "Escape") {
                setValue(chapter.title);
                setEditing(false);
              }
            }}
            aria-label="Chapter title"
            className="min-w-0 flex-1 rounded-[6px] border border-brand-300 bg-white px-1.5 py-0.5 text-[13px] font-semibold text-ink outline-none ring-2 ring-brand-100"
          />
        ) : (
          <button
            type="button"
            onClick={onToggle}
            onDoubleClick={() => setEditing(true)}
            className="min-w-0 flex-1 truncate text-left text-[13px] font-semibold text-ink"
            title={`${chapter.title} — click to expand, double-click to rename`}
          >
            {chapter.title}
          </button>
        )}

        <div className="flex shrink-0 items-center gap-0.5">
          <IconButton
            aria-label={expanded ? "Collapse chapter" : "Expand chapter"}
            onClick={onToggle}
          >
            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </IconButton>
          <IconButton
            tone="danger"
            aria-label={`Delete ${chapter.title}`}
            onClick={onDelete}
          >
            <Trash2 size={13} />
          </IconButton>
        </div>
      </div>

      {expanded ? (
        <div>
          {chapter.sections.map((section, sectionIndex) => (
            <SectionItem
              key={`${sectionIndex}-${section}`}
              label={`${index + 1}.${sectionIndex + 1}`}
              title={section}
              dragging={
                drag?.kind === "section" &&
                drag.chapterIndex === index &&
                drag.index === sectionIndex
              }
              dropBefore={
                dropTarget?.kind === "section" &&
                dropTarget.chapterIndex === index &&
                dropTarget.index === sectionIndex
              }
              onRename={(title) => onRenameSection(sectionIndex, title)}
              onDelete={() => onDeleteSection(sectionIndex)}
              onDragStart={() =>
                onDragStart({ kind: "section", chapterIndex: index, index: sectionIndex })
              }
              onDragOver={(event) => {
                event.preventDefault();
                event.stopPropagation();
                if (drag?.kind === "section" && drag.chapterIndex === index) {
                  onDragOver({ kind: "section", chapterIndex: index, index: sectionIndex });
                }
              }}
              onDrop={onDrop}
              onDragEnd={onDragEnd}
            />
          ))}

          <button
            type="button"
            onClick={onAddSection}
            className="flex w-full items-center gap-1.5 border-t border-line px-2.5 py-2 pl-9 text-[12px] font-medium text-brand-600 transition-colors hover:bg-brand-50"
          >
            <Plus size={12} />
            Add section
          </button>
        </div>
      ) : null}
    </div>
  );
}
