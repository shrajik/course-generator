"use client";

import { useEffect, useRef } from "react";
import { CELL, CELL_MONO_FONT, codeLines } from "@/lib/content/code-cell";
import { EditableText } from "./EditableText";
import { Caption, readText } from "./parts";
import type { BlockViewProps } from "./types";

/**
 * A static code sample: the same light, bordered box a code cell uses, without
 * a Run button, status or output.
 *
 * It takes its sizes from CELL, which mirrors backend/app/course/document/
 * code_cell.py. The layout engine reserves head row + one row per rendered
 * line, and the PDF draws these same numbers, so the block is exactly as tall
 * as its code - never a fixed size.
 */
export function CodeBlock({ block, editable, onEdit, onResize }: BlockViewProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  // Only after an edit: measuring on mount would mark the course as changed
  // just by opening it.
  const touched = useRef(false);

  const code = readText(block.content, "code");
  const language = readText(block.content, "language");

  // More (or fewer) lines than the room reserved: ask the editor to move what
  // sits below, the way a code cell does.
  useEffect(() => {
    const node = rootRef.current;
    if (!node || !onResize || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (touched.current) onResize(Math.ceil(node.offsetHeight));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [onResize]);

  return (
    <div ref={rootRef} style={{ fontFamily: CELL_MONO_FONT }}>
      <div
        className="flex items-center justify-between font-sans text-[11px] font-bold uppercase tracking-[0.09em] text-[#5f6368]"
        style={{ height: CELL.headH }}
      >
        <span>{language || "Code"}</span>
        <span className="font-medium normal-case tracking-normal text-[#9aa0a6]">Code</span>
      </div>

      <div
        style={{
          border: `${CELL.border}px solid #E5E7EB`,
          borderRadius: 6,
          background: "#f8f9fa",
          padding: `${CELL.codePadY}px 0`,
          fontSize: CELL.font,
          lineHeight: `${CELL.rowH}px`,
          color: "#202124",
        }}
      >
        {editable ? (
          <div className="flex">
            <div aria-hidden style={{ flex: `0 0 ${CELL.gutterW}px` }} />
            <EditableText
              as="pre"
              value={code}
              editable
              onCommit={(value) => {
                touched.current = true;
                onEdit("code", value);
              }}
              placeholder="Code"
              className="m-0 min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]"
              style={{ paddingRight: CELL.textPadR, tabSize: 4, fontFamily: "inherit" }}
            />
          </div>
        ) : (
          codeLines(code).map((line, index) => (
            <div key={index} className="flex" style={{ minHeight: CELL.rowH }}>
              <span
                aria-hidden
                className="select-none text-right text-[#9aa0a6]"
                style={{ flex: `0 0 ${CELL.gutterW}px`, width: CELL.gutterW, paddingRight: 10 }}
              >
                {index + 1}
              </span>
              <span
                className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]"
                style={{ paddingRight: CELL.textPadR, tabSize: 4 }}
              >
                {line}
              </span>
            </div>
          ))
        )}
      </div>

      <Caption
        text={readText(block.content, "caption")}
        editable={editable}
        onCommit={(value) => onEdit("caption", value)}
      />
    </div>
  );
}
