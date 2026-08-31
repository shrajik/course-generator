"use client";

import { useMemo, useState } from "react";
import {
  ChevronDown,
  Download,
  Eye,
  Loader2,
  MoreHorizontal,
  RefreshCw,
  Redo2,
  Undo2,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { useEditor } from "@/lib/editor/store";
import { asString } from "@/lib/editor/blocks";
import { cn } from "@/lib/utils/cn";

interface EditorToolbarProps {
  onPreview: () => void;
  onExport: () => void;
  onReload: () => void;
  exporting: boolean;
  reloading: boolean;
}

interface ChapterEntry {
  id: string;
  number: number;
  title: string;
  pageIndex: number;
}

export function EditorToolbar({
  onPreview,
  onExport,
  onReload,
  exporting,
  reloading,
}: EditorToolbarProps) {
  const editor = useEditor();
  const { document: doc, activePageIndex } = editor;
  const [menuOpen, setMenuOpen] = useState(false);
  const [chapterOpen, setChapterOpen] = useState(false);

  /** Chapter list derived from the document itself, not hardcoded. */
  const chapters = useMemo<ChapterEntry[]>(() => {
    if (!doc) return [];
    const found = new Map<string, ChapterEntry>();
    doc.pages.forEach((page, pageIndex) => {
      page.blocks.forEach((block) => {
        const id = block.meta.chapter_id;
        if (!id || found.has(id)) return;
        if (block.type !== "heading") return;
        found.set(id, {
          id,
          number: block.meta.chapter_number ?? found.size + 1,
          title: asString(block.content.text),
          pageIndex,
        });
      });
    });
    return [...found.values()].sort((a, b) => a.number - b.number);
  }, [doc]);

  const current =
    [...chapters].reverse().find((chapter) => chapter.pageIndex <= activePageIndex) ?? null;

  return (
    <header className="flex items-center gap-3 border-b border-line bg-white px-4 py-2.5">
      <div className="flex items-center gap-2.5">
        <span className="flex h-6 w-6 items-center justify-center rounded-[7px] bg-brand-600 text-[12px] font-semibold text-white">
          4
        </span>
        <h1 className="text-[14px] font-bold tracking-[-0.01em] text-ink">Course Editor</h1>
      </div>

      {chapters.length > 0 ? (
        <div className="relative ml-3">
          <button
            type="button"
            onClick={() => setChapterOpen((open) => !open)}
            className="flex items-center gap-2 rounded-[8px] px-2 py-1 text-left transition-colors hover:bg-brand-50"
          >
            <span className="text-[11.5px] font-medium text-ink-400">
              Chapter {String(current?.number ?? 1).padStart(2, "0")}
            </span>
            <span className="max-w-[240px] truncate text-[12.5px] font-semibold text-ink">
              {current?.title ?? doc?.course_title}
            </span>
            <ChevronDown size={13} className="text-ink-400" />
          </button>
          {chapterOpen ? (
            <div className="absolute left-0 top-9 z-30 w-72 overflow-hidden rounded-[10px] border border-line bg-white py-1 shadow-pop">
              {chapters.map((chapter) => (
                <button
                  key={chapter.id}
                  type="button"
                  onClick={() => {
                    editor.setPage(chapter.pageIndex);
                    setChapterOpen(false);
                  }}
                  className={cn(
                    "flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] hover:bg-brand-50",
                    chapter.id === current?.id ? "text-brand-700" : "text-ink-700",
                  )}
                >
                  <span className="w-6 shrink-0 tabular-nums text-ink-400">
                    {String(chapter.number).padStart(2, "0")}
                  </span>
                  <span className="truncate">{chapter.title}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="ml-auto flex items-center gap-2">
        {editor.dirty ? (
          <span
            className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10.5px] font-medium text-amber-800"
            title="Manual edits live in this browser session. The backend POC has no document-save endpoint, so Export PDF renders the last version the backend stored."
          >
            Local changes
          </span>
        ) : null}

        <div className="flex items-center rounded-[8px] border border-line">
          <IconButton
            aria-label="Undo"
            disabled={!editor.canUndo}
            onClick={editor.undo}
            className="rounded-r-none"
          >
            <Undo2 size={14} />
          </IconButton>
          <span className="h-5 w-px bg-line" aria-hidden />
          <IconButton
            aria-label="Redo"
            disabled={!editor.canRedo}
            onClick={editor.redo}
            className="rounded-l-none"
          >
            <Redo2 size={14} />
          </IconButton>
        </div>

        <Button variant="outline" size="sm" onClick={onPreview}>
          <Eye size={13} />
          Preview
        </Button>

        <Button size="sm" onClick={onExport} disabled={exporting}>
          {exporting ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
          Export PDF
        </Button>

        <div className="relative">
          <IconButton
            aria-label="More options"
            active={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <MoreHorizontal size={15} />
          </IconButton>
          {menuOpen ? (
            <div className="absolute right-0 top-8 z-30 w-60 overflow-hidden rounded-[10px] border border-line bg-white shadow-pop">
              <button
                type="button"
                onClick={() => {
                  onReload();
                  setMenuOpen(false);
                }}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-ink-700 hover:bg-brand-50"
              >
                {reloading ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <RefreshCw size={12} />
                )}
                Reload from backend
              </button>
              <div className="border-t border-line px-3 py-2 text-[11px] text-ink-400">
                <p>Document {doc?.document_id}</p>
                <p className="mt-0.5">
                  Version {doc?.version} · {doc?.pages.length} pages
                </p>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
