"use client";

import { useEffect, useRef, useState } from "react";
import { GripVertical, Pencil, Trash2 } from "lucide-react";
import { IconButton } from "@/components/ui/IconButton";
import { cn } from "@/lib/utils/cn";

interface SectionItemProps {
  label: string;
  title: string;
  dragging: boolean;
  dropBefore: boolean;
  onRename: (title: string) => void;
  onDelete: () => void;
  onDragStart: () => void;
  onDragOver: (event: React.DragEvent) => void;
  onDrop: () => void;
  onDragEnd: () => void;
}

export function SectionItem({
  label,
  title,
  dragging,
  dropBefore,
  onRename,
  onDelete,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: SectionItemProps) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => setValue(title), [title]);
  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const commit = () => {
    const next = value.trim();
    onRename(next || title);
    if (!next) setValue(title);
    setEditing(false);
  };

  return (
    <div
      draggable={!editing}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onDrop();
      }}
      onDragEnd={onDragEnd}
      className={cn(
        "group flex items-center gap-2 border-t border-line bg-white py-2 pl-9 pr-2.5",
        dragging && "dragging",
        dropBefore && "border-t-2 border-t-brand-500",
      )}
    >
      <GripVertical
        size={13}
        className="shrink-0 cursor-grab text-ink-300 active:cursor-grabbing"
        aria-hidden
      />
      <span className="w-7 shrink-0 text-[11.5px] font-medium tabular-nums text-ink-400">
        {label}
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
              setValue(title);
              setEditing(false);
            }
          }}
          aria-label="Section title"
          className="min-w-0 flex-1 rounded-[6px] border border-brand-300 bg-white px-1.5 py-0.5 text-[12.5px] text-ink outline-none ring-2 ring-brand-100"
        />
      ) : (
        <button
          type="button"
          onDoubleClick={() => setEditing(true)}
          className="min-w-0 flex-1 truncate text-left text-[12.5px] text-ink-700"
          title={title}
        >
          {title}
        </button>
      )}

      <div className="flex shrink-0 items-center gap-0.5">
        <IconButton
          aria-label={`Rename ${title}`}
          onClick={() => setEditing(true)}
          className="h-6 w-6"
        >
          <Pencil size={12} />
        </IconButton>
        <IconButton
          tone="danger"
          aria-label={`Delete ${title}`}
          onClick={onDelete}
          className="h-6 w-6"
        >
          <Trash2 size={12} />
        </IconButton>
      </div>
    </div>
  );
}
