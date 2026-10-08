import type { WrapAround } from "@/lib/editor/pairing";
import type { Block } from "@/lib/types/document";

export interface BlockViewProps {
  block: Block;
  /** Needed to resolve `assets/…` image paths through the backend. */
  documentId: string;
  /** False in Preview and for thumbnails. */
  editable: boolean;
  /** Commit a value at a content path, e.g. `"text"` or `"items.2"`. */
  onEdit: (path: string, value: unknown) => void;
  /** True only on the editing canvas. Thumbnails and Preview are read-only
   * views, so a block that offers actions (a code cell's Run button) must
   * not show them there. */
  interactive?: boolean;
  /** Tell the editor how tall this block now is, so it can make exactly that
   * much room (moving the blocks below it, up or down). Used when content
   * changes size after layout. */
  onResize?: (height: number) => void;
  /** A paragraph sharing its position with a section-intro illustration
   * wraps around that corner, as the PDF's CSS float does. See
   * lib/editor/pairing.ts. */
  wrapAround?: WrapAround | null;
}
