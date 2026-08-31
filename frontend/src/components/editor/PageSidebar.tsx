"use client";

import { Plus } from "lucide-react";
import { BlockRenderer } from "@/components/blocks/BlockRenderer";
import { useEditor } from "@/lib/editor/store";
import { PAGE_HEIGHT, PAGE_WIDTH, type CourseDocument, type Page } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";

const THUMB_WIDTH = 92;
const SCALE = THUMB_WIDTH / PAGE_WIDTH;

function Thumbnail({ page, document }: { page: Page; document: CourseDocument }) {
  return (
    <div
      className="overflow-hidden bg-white"
      style={{ width: THUMB_WIDTH, height: PAGE_HEIGHT * SCALE }}
    >
      <div
        className="relative"
        style={{
          width: PAGE_WIDTH,
          height: PAGE_HEIGHT,
          transform: `scale(${SCALE})`,
          transformOrigin: "top left",
          background: page.background ?? "#ffffff",
        }}
      >
        {page.blocks.map((block) => (
          <BlockRenderer
            key={block.id}
            block={block}
            documentId={document.document_id}
            editable={false}
          />
        ))}
      </div>
    </div>
  );
}

export function PageSidebar() {
  const editor = useEditor();
  const { document: doc, activePageIndex } = editor;
  if (!doc) return null;

  return (
    <div className="flex h-full flex-col">
      <p className="px-3 pb-2 pt-3 text-[12.5px] font-semibold text-ink">Pages</p>

      <div className="subtle-scroll flex-1 space-y-2.5 overflow-y-auto px-3 pb-3">
        {doc.pages.map((page, index) => {
          const active = index === activePageIndex;
          return (
            <button
              key={page.id}
              type="button"
              onClick={() => editor.setPage(index)}
              aria-current={active}
              className={cn(
                "relative block w-full overflow-hidden rounded-[7px] border transition-all",
                active
                  ? "border-brand-600 ring-2 ring-brand-200"
                  : "border-line hover:border-brand-300",
              )}
            >
              <Thumbnail page={page} document={doc} />
              <span
                className={cn(
                  "absolute right-1 top-1 rounded-[4px] px-1 text-[9px] font-semibold tabular-nums",
                  active ? "bg-brand-600 text-white" : "bg-white/85 text-ink-500",
                )}
              >
                {page.page_number}
              </span>
            </button>
          );
        })}
      </div>

      <div className="border-t border-line p-2.5">
        <button
          type="button"
          onClick={editor.addPage}
          className="flex w-full items-center justify-center gap-1.5 rounded-[8px] border border-line bg-white py-2 text-[12px] font-medium text-ink-700 transition-colors hover:border-brand-300 hover:bg-brand-50 hover:text-brand-700"
        >
          <Plus size={13} />
          Add Page
        </button>
      </div>
    </div>
  );
}
