import type { Block } from "@/lib/types/document";

export interface BlockViewProps {
  block: Block;
  /** Needed to resolve `assets/…` image paths through the backend. */
  documentId: string;
  /** False in Preview and for thumbnails. */
  editable: boolean;
  /** Commit a value at a content path, e.g. `"text"` or `"items.2"`. */
  onEdit: (path: string, value: unknown) => void;
}
