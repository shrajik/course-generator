"use client";

import type { BlockViewProps } from "./types";

export function DividerBlock({ block }: BlockViewProps) {
  return (
    <div
      className="h-full min-h-[3px] w-full rounded-[3px]"
      style={{ background: block.style.border_color ?? "#E2E8F0" }}
      aria-hidden
    />
  );
}
