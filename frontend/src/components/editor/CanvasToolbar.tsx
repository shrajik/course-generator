"use client";

import { useState } from "react";
import {
  AlignCenter,
  AlignJustify,
  AlignLeft,
  AlignRight,
  Bold,
  Italic,
  List,
  MoreHorizontal,
  Table as TableIcon,
  Trash2,
  Underline,
} from "lucide-react";
import { IconButton } from "@/components/ui/IconButton";
import { isTextBlock } from "@/lib/editor/blocks";
import { useEditor } from "@/lib/editor/store";
import type { TextAlign } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";

const FONTS = ["Inter", "Georgia", "Segoe UI", "Helvetica", "JetBrains Mono"];

const ALIGNMENTS: Array<{ value: TextAlign; Icon: typeof AlignLeft }> = [
  { value: "left", Icon: AlignLeft },
  { value: "center", Icon: AlignCenter },
  { value: "right", Icon: AlignRight },
  { value: "justify", Icon: AlignJustify },
];

/** The formatting bar directly above the canvas. */
export function CanvasToolbar() {
  const editor = useEditor();
  const block = editor.selectedBlocks[0] ?? null;
  const [menuOpen, setMenuOpen] = useState(false);
  const disabled = !block;
  const textual = block ? isTextBlock(block.type) || block.type === "code" : false;

  const size = block?.style.font_size ?? 16;
  const weight = block?.style.font_weight ?? 400;

  const setStyle = (patch: Record<string, unknown>) => {
    if (!block) return;
    editor.updateStyle(block.id, patch);
  };

  return (
    <div className="flex items-center gap-1.5 border-b border-line bg-white px-3 py-2">
      <select
        aria-label="Font"
        disabled={disabled || !textual}
        value={block?.style.font_family?.split(",")[0] ?? "Inter"}
        onChange={(event) => setStyle({ font_family: event.target.value })}
        className="h-7 w-[104px] rounded-[7px] border border-line bg-white px-2 text-[11.5px] text-ink outline-none disabled:opacity-50"
      >
        {FONTS.map((font) => (
          <option key={font} value={font}>
            {font}
          </option>
        ))}
      </select>

      <div className="flex h-7 items-center rounded-[7px] border border-line">
        <input
          aria-label="Font size"
          type="number"
          min={8}
          max={96}
          step={0.5}
          disabled={disabled || !textual}
          value={size}
          onChange={(event) => setStyle({ font_size: Number(event.target.value) })}
          className="h-full w-[46px] bg-transparent px-2 text-[11.5px] text-ink outline-none disabled:opacity-50"
        />
        <div className="flex h-full flex-col border-l border-line">
          <button
            type="button"
            aria-label="Increase font size"
            disabled={disabled || !textual}
            onClick={() => setStyle({ font_size: Math.min(96, size + 1) })}
            className="flex h-1/2 items-center px-1 text-[7px] text-ink-400 hover:text-brand-600 disabled:opacity-40"
          >
            ▲
          </button>
          <button
            type="button"
            aria-label="Decrease font size"
            disabled={disabled || !textual}
            onClick={() => setStyle({ font_size: Math.max(8, size - 1) })}
            className="flex h-1/2 items-center px-1 text-[7px] text-ink-400 hover:text-brand-600 disabled:opacity-40"
          >
            ▼
          </button>
        </div>
      </div>

      <span className="mx-0.5 h-5 w-px bg-line" aria-hidden />

      <IconButton
        aria-label="Bold"
        active={weight >= 600}
        disabled={disabled || !textual}
        onClick={() => setStyle({ font_weight: weight >= 600 ? 400 : 700 })}
      >
        <Bold size={13} />
      </IconButton>
      <IconButton
        aria-label="Italic"
        active={Boolean(block?.style.italic)}
        disabled={disabled || !textual}
        onClick={() => setStyle({ italic: !block?.style.italic })}
      >
        <Italic size={13} />
      </IconButton>
      <IconButton
        aria-label="Underline"
        disabled
        title="Underline is not supported by the PDF renderer yet"
      >
        <Underline size={13} />
      </IconButton>

      <span className="mx-0.5 h-5 w-px bg-line" aria-hidden />

      <IconButton
        aria-label="Insert list block"
        onClick={() => editor.insertBlock("learning_objectives", block?.id ?? null)}
      >
        <List size={13} />
      </IconButton>
      <IconButton
        aria-label="Insert table block"
        onClick={() => editor.insertBlock("table", block?.id ?? null)}
      >
        <TableIcon size={13} />
      </IconButton>

      <span className="mx-0.5 h-5 w-px bg-line" aria-hidden />

      <label
        className={cn(
          "relative flex h-7 w-7 items-center justify-center rounded-[7px]",
          disabled || !textual ? "opacity-50" : "cursor-pointer hover:bg-brand-50",
        )}
        title="Text colour"
      >
        <span
          className="h-[15px] w-[15px] rounded-full border border-line"
          style={{ background: block?.style.color ?? "#1A1A1A" }}
        />
        <input
          type="color"
          aria-label="Text colour"
          disabled={disabled || !textual}
          value={block?.style.color ?? "#1A1A1A"}
          onChange={(event) => setStyle({ color: event.target.value })}
          className="absolute inset-0 cursor-pointer opacity-0"
        />
      </label>

      <div className="ml-auto flex items-center gap-1.5">
        {ALIGNMENTS.map(({ value, Icon }) => (
          <IconButton
            key={value}
            aria-label={`Align ${value}`}
            active={block?.style.align === value}
            disabled={disabled || !textual}
            onClick={() => setStyle({ align: value })}
          >
            <Icon size={13} />
          </IconButton>
        ))}

        <div className="relative">
          <IconButton
            aria-label="More block actions"
            active={menuOpen}
            disabled={disabled}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <MoreHorizontal size={14} />
          </IconButton>
          {menuOpen && block ? (
            <div className="absolute right-0 top-8 z-30 w-44 overflow-hidden rounded-[10px] border border-line bg-white shadow-pop">
              <button
                type="button"
                onClick={() => {
                  editor.insertBlock("paragraph", block.id);
                  setMenuOpen(false);
                }}
                className="block w-full px-3 py-2 text-left text-[12px] text-ink-700 hover:bg-brand-50"
              >
                Insert paragraph below
              </button>
              <button
                type="button"
                onClick={() => {
                  editor.deleteBlock(block.id);
                  setMenuOpen(false);
                }}
                className="flex w-full items-center gap-2 border-t border-line px-3 py-2 text-left text-[12px] text-danger hover:bg-red-50"
              >
                <Trash2 size={12} />
                Delete block
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
