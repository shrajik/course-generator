/**
 * Small, pure helpers for the Edit Visuals image text-overlay editor
 * (ImageBlock.tsx's drag handling, PropertiesPanel.tsx's add/delete) - kept
 * separate so they're independently testable, same convention as
 * flowchartSpec.ts (Content Studio / Edit Visuals Part 1).
 */

import type { OverlayLabel } from "@/lib/types/document";

export function nextLabelId(labels: OverlayLabel[]): string {
  const used = new Set(labels.map((l) => l.id));
  let index = labels.length;
  let id = `ol${index}`;
  while (used.has(id)) {
    index += 1;
    id = `ol${index}`;
  }
  return id;
}

export function blankLabel(id: string): OverlayLabel {
  return { id, text: "New label", x_pct: 50, y_pct: 50, style: "label", color: "" };
}

/** Keeps a percentage position on the image, never past its edges. */
export function clampPct(value: number): number {
  return Math.max(0, Math.min(100, value));
}

/** The (x_pct, y_pct) a pointer at (clientX, clientY) corresponds to,
 * relative to `rect` (the image's own rendered rect, e.g. from
 * `getBoundingClientRect()` on the aspect-ratio wrapper - never the outer,
 * possibly letterboxed block box). */
export function pctFromPointer(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
): { x_pct: number; y_pct: number } {
  if (rect.width <= 0 || rect.height <= 0) return { x_pct: 50, y_pct: 50 };
  return {
    x_pct: clampPct(((clientX - rect.left) / rect.width) * 100),
    y_pct: clampPct(((clientY - rect.top) / rect.height) * 100),
  };
}
