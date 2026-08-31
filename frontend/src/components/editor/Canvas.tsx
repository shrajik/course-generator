"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getBlockRenderer } from "@/components/blocks/registry";
import { SelectionBox, type HandleId } from "./SelectionBox";
import { blockBoxStyle } from "@/lib/editor/style";
import { useEditor } from "@/lib/editor/store";
import { CONTENT_WIDTH, PAGE_HEIGHT, PAGE_WIDTH, type Block } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";

const MIN_WIDTH = 80;
const MIN_HEIGHT = 24;

interface DragSession {
  mode: "move" | "resize";
  handle?: HandleId;
  startX: number;
  startY: number;
  origin: { x: number; y: number; width: number; height: number };
}

export function Canvas() {
  const editor = useEditor();
  const { document: doc, activePage, selectedIds, editingBlockId } = editor;
  const frameRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  const session = useRef<DragSession | null>(null);

  // Fit the A4 page to the available width.
  useEffect(() => {
    const element = frameRef.current;
    if (!element) return;
    const measure = () => {
      const available = element.clientWidth - 40;
      setScale(Math.max(0.35, Math.min(1, available / PAGE_WIDTH)));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const beginMove = useCallback(
    (block: Block, event: React.PointerEvent) => {
      if (editingBlockId === block.id) return;
      event.stopPropagation();
      (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
      editor.checkpoint();
      session.current = {
        mode: "move",
        startX: event.clientX,
        startY: event.clientY,
        origin: { ...block.layout },
      };
    },
    [editor, editingBlockId],
  );

  const beginResize = useCallback(
    (block: Block, handle: HandleId, event: React.PointerEvent) => {
      event.stopPropagation();
      event.preventDefault();
      (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
      editor.checkpoint();
      session.current = {
        mode: "resize",
        handle,
        startX: event.clientX,
        startY: event.clientY,
        origin: { ...block.layout },
      };
    },
    [editor],
  );

  const handlePointerMove = useCallback(
    (blockId: string, event: React.PointerEvent) => {
      const current = session.current;
      if (!current) return;
      const dx = (event.clientX - current.startX) / scale;
      const dy = (event.clientY - current.startY) / scale;
      const { origin } = current;

      if (current.mode === "move") {
        editor.updateLayout(
          blockId,
          {
            x: Math.round(Math.max(0, Math.min(PAGE_WIDTH - origin.width, origin.x + dx))),
            y: Math.round(Math.max(0, origin.y + dy)),
          },
          true,
        );
        return;
      }

      const handle = current.handle ?? "se";
      let { x, y, width, height } = origin;

      if (handle.includes("e")) width = Math.max(MIN_WIDTH, origin.width + dx);
      if (handle.includes("s")) height = Math.max(MIN_HEIGHT, origin.height + dy);
      if (handle.includes("w")) {
        const next = Math.max(MIN_WIDTH, origin.width - dx);
        x = origin.x + (origin.width - next);
        width = next;
      }
      if (handle.includes("n")) {
        const next = Math.max(MIN_HEIGHT, origin.height - dy);
        y = origin.y + (origin.height - next);
        height = next;
      }

      editor.updateLayout(
        blockId,
        {
          x: Math.round(x),
          y: Math.round(y),
          width: Math.round(width),
          height: Math.round(height),
        },
        true,
      );
    },
    [editor, scale],
  );

  const endSession = useCallback(() => {
    session.current = null;
  }, []);

  if (!doc || !activePage) {
    return (
      <div ref={frameRef} className="flex h-full items-center justify-center text-[13px] text-ink-400">
        No page selected.
      </div>
    );
  }

  return (
    <div
      ref={frameRef}
      className="subtle-scroll h-full overflow-auto bg-canvas px-5 py-5"
      onPointerDown={() => editor.select([])}
    >
      <div
        className="mx-auto"
        style={{ width: PAGE_WIDTH * scale, height: PAGE_HEIGHT * scale }}
      >
        <div
          data-canvas-page={activePage.page_number}
          className="relative rounded-[3px] bg-white shadow-canvas"
          style={{
            width: PAGE_WIDTH,
            height: PAGE_HEIGHT,
            transform: `scale(${scale})`,
            transformOrigin: "top left",
            background: activePage.background ?? "#ffffff",
          }}
        >
          {activePage.blocks.map((block) => {
            const selected = selectedIds.includes(block.id);
            const editing = editingBlockId === block.id;
            const View = getBlockRenderer(block.type);
            return (
              <div
                key={block.id}
                data-block-id={block.id}
                data-block-type={block.type}
                style={blockBoxStyle(block)}
                className={cn(
                  "group/block",
                  editing ? "cursor-text" : "cursor-default",
                  !selected && "hover:outline hover:outline-1 hover:outline-brand-200",
                )}
                onPointerDown={(event) => {
                  event.stopPropagation();
                  if (!selected) {
                    editor.select([block.id]);
                    return;
                  }
                  if (!editing) beginMove(block, event);
                }}
                onPointerMove={(event) => handlePointerMove(block.id, event)}
                onPointerUp={endSession}
                onPointerCancel={endSession}
                onDoubleClick={(event) => {
                  event.stopPropagation();
                  editor.select([block.id]);
                  editor.setEditing(block.id);
                }}
              >
                <View
                  block={block}
                  documentId={doc.document_id}
                  editable={editing}
                  onEdit={(path, value) => editor.updateContentPath(block.id, path, value)}
                />
                {selected ? (
                  <SelectionBox
                    editing={editing}
                    onHandlePointerDown={(handle, event) => beginResize(block, handle, event)}
                  />
                ) : null}
              </div>
            );
          })}

          {activePage.blocks.length === 0 ? (
            <p
              className="absolute text-[13px] text-ink-300"
              style={{ left: 64, top: 72, width: CONTENT_WIDTH }}
            >
              This page is empty. Use “Blocks” in the left rail to add content.
            </p>
          ) : null}
        </div>
      </div>
    </div>
  );
}
