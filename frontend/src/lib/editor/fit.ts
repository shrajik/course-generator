/**
 * Making a block exactly as tall as its content, and keeping the page honest.
 *
 * Blocks are absolutely positioned, so a block whose content changes height
 * does not push anything: a taller one is painted over whatever sits below it,
 * and a shorter one leaves a hole. This applies the adjustment the layout
 * engine would have made - move everything that started at or below the block's
 * old bottom edge by the change, and keep the page tall enough to hold it (the
 * PDF draws each page at its own declared height and clips whatever lies past
 * it).
 *
 * Both directions matter. Growing prevents overlap; shrinking is what stops a
 * code cell that once showed output from leaving dead space behind forever
 * after its output is gone, which would also disagree with the height the PDF
 * reserves for it.
 *
 * Pure and mutating only the page it is given, so it can be tested without a
 * React tree.
 */

// Types only, so this module has no runtime dependencies and runs under the
// plain Node test runner.
import type { CourseDocument, Page } from "@/lib/types/document";

/** The default A4 sheet; mirrors PAGE_HEIGHT in lib/types/document.ts. */
const DEFAULT_SHEET_HEIGHT = 1123;

/** Mirrors PAGE_MARGIN_BOTTOM in backend/app/schemas/document.py. */
export const PAGE_BOTTOM_MARGIN = 88;

/** Sub-pixel jitter is not a reason to move anything. */
export const FIT_TOLERANCE = 2;

/**
 * The height of one printed sheet for this document. A template can carry its
 * own page size (an uploaded Letter-size DOCX, say); the built-in templates use
 * the default.
 */
export function sheetHeight(document: Pick<CourseDocument, "meta">): number {
  const page = document.meta?.theme?.page as { height?: unknown } | null | undefined;
  const height = Number(page?.height);
  return Number.isFinite(height) && height > 0 ? height : DEFAULT_SHEET_HEIGHT;
}

/**
 * Resize `page.blocks[index]` to `height` px, shifting what is below it by the
 * difference. `sheet` is the document's sheet height: a page is never made
 * shorter than it. Returns true when anything changed.
 */
export function resizeBlock(page: Page, index: number, height: number, sheet: number): boolean {
  const target = page.blocks[index];
  if (!target) return false;

  const delta = Math.round(height - target.layout.height);
  if (Math.abs(delta) < FIT_TOLERANCE) return false;

  const oldBottom = target.layout.y + target.layout.height;
  target.layout = { ...target.layout, height: target.layout.height + delta };

  for (const other of page.blocks) {
    if (other.id !== target.id && other.layout.y >= oldBottom - FIT_TOLERANCE) {
      other.layout = { ...other.layout, y: other.layout.y + delta };
    }
  }

  // Tall enough for what is on it - and never shorter than the sheet, so a page
  // that was only grown by a cell is returned to normal once the cell shrinks.
  const bottom = Math.max(...page.blocks.map((block) => block.layout.y + block.layout.height));
  const needed = Math.ceil(bottom + PAGE_BOTTOM_MARGIN);
  page.size = { ...page.size, height: Math.max(needed, sheet) };
  return true;
}
