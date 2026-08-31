"use client";

import { Plus } from "lucide-react";
import { BLOCK_LABELS, INSERTABLE_TYPES } from "@/lib/editor/blocks";
import { useEditor } from "@/lib/editor/store";

/** "Blocks" rail panel: insert any supported block type after the selection. */
export function BlocksPanel() {
  const editor = useEditor();
  const anchor = editor.selectedIds[0] ?? null;

  return (
    <div className="flex h-full flex-col">
      <div className="px-3 pb-2 pt-3">
        <p className="text-[12.5px] font-semibold text-ink">Blocks</p>
        <p className="mt-0.5 text-[11px] leading-snug text-ink-400">
          {anchor ? "Inserted after the selected block." : "Added to the end of this page."}
        </p>
      </div>
      <div className="subtle-scroll flex-1 space-y-1 overflow-y-auto px-2.5 pb-3">
        {INSERTABLE_TYPES.map((type) => (
          <button
            key={type}
            type="button"
            onClick={() => editor.insertBlock(type, anchor)}
            className="flex w-full items-center justify-between gap-2 rounded-[7px] border border-transparent px-2 py-1.5 text-left text-[12px] text-ink-700 transition-colors hover:border-brand-200 hover:bg-brand-50 hover:text-brand-700"
          >
            {BLOCK_LABELS[type]}
            <Plus size={12} className="text-ink-300" />
          </button>
        ))}
      </div>
    </div>
  );
}
