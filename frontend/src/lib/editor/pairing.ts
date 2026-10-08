/**
 * The "section intro" pair: a small illustration and the paragraph beside it.
 *
 * The layout engine places the illustration and the section's first paragraph
 * at the SAME position (backend: layout.flow_blocks, "section_intro"). The PDF
 * then floats the image left and lets the paragraph's text wrap around it. The
 * editor lays blocks out absolutely and has no float concept, so without help
 * the image is simply painted over the start of the paragraph and hides it.
 *
 * `wrapAroundFor` tells a paragraph that an illustration shares its position
 * and how much room to leave, and the paragraph reserves that corner with a
 * floated spacer - the same wrap the PDF produces, using the same gaps.
 */

import type { Block } from "@/lib/types/document";

/** Mirrors `.section-intro-img { margin: 0 16px 10px 0 }` in the PDF template. */
export const FLOAT_GAP_RIGHT = 16;
export const FLOAT_GAP_BOTTOM = 10;

export interface WrapAround {
  /** Width of the corner to keep clear, gap included. */
  width: number;
  /** Height of the corner to keep clear, gap included. */
  height: number;
}

/** True for an illustration that opens a section and sits beside its paragraph. */
export function isSectionIntroImage(block: Block): boolean {
  return (
    block.type === "image" &&
    String(block.content.illustration_style ?? "").trim().toLowerCase() === "section_intro"
  );
}

/**
 * The corner of `block` (a paragraph) that an illustration occupies, or null.
 *
 * "Beside" means exactly what the layout engine produced: an illustration with
 * the same top edge, starting at or left of the paragraph. If the user has
 * since moved either block the pairing no longer holds, so the paragraph goes
 * back to full width and nothing is wrapped around something that has moved.
 */
export function wrapAroundFor(blocks: readonly Block[], block: Block): WrapAround | null {
  if (block.type !== "paragraph") return null;

  const image = blocks.find(
    (candidate) =>
      isSectionIntroImage(candidate) &&
      Math.abs(candidate.layout.y - block.layout.y) < 1 &&
      candidate.layout.x <= block.layout.x + 1 &&
      candidate.layout.x + candidate.layout.width > block.layout.x,
  );
  if (!image) return null;

  return {
    width: image.layout.x + image.layout.width - block.layout.x + FLOAT_GAP_RIGHT,
    height: image.layout.height + FLOAT_GAP_BOTTOM,
  };
}
