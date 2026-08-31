"use client";

import { cn } from "@/lib/utils/cn";

export type HandleId = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

const HANDLES: Array<{ id: HandleId; className: string; cursor: string }> = [
  { id: "nw", className: "-left-[4px] -top-[4px]", cursor: "nwse-resize" },
  { id: "n", className: "left-1/2 -top-[4px] -translate-x-1/2", cursor: "ns-resize" },
  { id: "ne", className: "-right-[4px] -top-[4px]", cursor: "nesw-resize" },
  { id: "e", className: "-right-[4px] top-1/2 -translate-y-1/2", cursor: "ew-resize" },
  { id: "se", className: "-right-[4px] -bottom-[4px]", cursor: "nwse-resize" },
  { id: "s", className: "left-1/2 -bottom-[4px] -translate-x-1/2", cursor: "ns-resize" },
  { id: "sw", className: "-left-[4px] -bottom-[4px]", cursor: "nesw-resize" },
  { id: "w", className: "-left-[4px] top-1/2 -translate-y-1/2", cursor: "ew-resize" },
];

interface SelectionBoxProps {
  editing: boolean;
  onHandlePointerDown: (handle: HandleId, event: React.PointerEvent) => void;
}

/** The violet selection frame with corner/edge handles from the design. */
export function SelectionBox({ editing, onHandlePointerDown }: SelectionBoxProps) {
  return (
    <>
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute -inset-[3px] rounded-[3px] border",
          editing ? "border-brand-600 border-dashed" : "border-brand-600",
        )}
      />
      {HANDLES.map((handle) => (
        <span
          key={handle.id}
          role="presentation"
          onPointerDown={(event) => onHandlePointerDown(handle.id, event)}
          style={{ cursor: handle.cursor }}
          className={cn(
            "absolute z-10 h-[9px] w-[9px] rounded-[2px] border border-brand-600 bg-white shadow-[0_1px_2px_rgba(0,0,0,0.12)]",
            handle.className,
          )}
        />
      ))}
    </>
  );
}

export { HANDLES };
