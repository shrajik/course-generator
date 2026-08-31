/** Mirrors backend/app/schemas/document.py, blocks.py and patch.py. */

export const BLOCK_TYPES = [
  "heading",
  "paragraph",
  "image",
  "quote",
  "callout",
  "code",
  "table",
  "quiz",
  "exercise",
  "case_study",
  "story",
  "tip",
  "warning",
  "summary",
  "divider",
  "learning_objectives",
  "challenge",
  "reflection",
] as const;

export type BlockType = (typeof BLOCK_TYPES)[number];

/** A4 at 96 DPI - identical to the backend layout engine. */
export const PAGE_WIDTH = 794;
export const PAGE_HEIGHT = 1123;
export const PAGE_MARGIN_X = 64;
export const CONTENT_WIDTH = PAGE_WIDTH - 2 * PAGE_MARGIN_X;

export type TextAlign = "left" | "center" | "right" | "justify";

export interface BlockStyle {
  font_family?: string | null;
  font_size?: number | null;
  font_weight?: number | null;
  line_height?: number | null;
  color?: string | null;
  background?: string | null;
  border_color?: string | null;
  border_radius?: number | null;
  border_width?: number | null;
  padding?: number | null;
  align?: TextAlign | null;
  italic?: boolean | null;
  letter_spacing?: number | null;
  accent_color?: string | null;
  [key: string]: unknown;
}

export interface BlockLayout {
  x: number;
  y: number;
  width: number;
  height: number;
  z_index: number;
  [key: string]: unknown;
}

export interface BlockMeta {
  chapter_id?: string | null;
  chapter_number?: number | null;
  section_key?: string | null;
  origin?: string;
  continued?: boolean;
  updated_at?: string | null;
  [key: string]: unknown;
}

/** Content is intentionally loose: the shape depends on `type`. */
export type BlockContent = Record<string, unknown>;

export interface Block {
  id: string;
  type: BlockType;
  content: BlockContent;
  style: BlockStyle;
  layout: BlockLayout;
  meta: BlockMeta;
}

export interface PageSize {
  width: number;
  height: number;
}

export interface Page {
  id: string;
  page_number: number;
  size: PageSize;
  background: string | null;
  kind: "cover" | "toc" | "content" | string;
  blocks: Block[];
}

export interface DocumentMeta {
  audience: string;
  template_name: string;
  chapter_ids: string[];
  theme: Record<string, unknown>;
  generated_with: string;
}

export interface CourseDocument {
  document_id: string;
  course_id: string;
  course_title: string;
  template_id: string;
  version: number;
  created_at: string;
  updated_at: string;
  meta: DocumentMeta;
  pages: Page[];
}

// --- typed views over `content` --------------------------------------------

export interface QuizQuestion {
  question: string;
  kind: string;
  options: string[];
  answer: string;
  explanation: string;
}

export interface TableRow {
  cells: string[];
}

// --- patches ---------------------------------------------------------------

export type PatchOperationType =
  | "update_block"
  | "delete_block"
  | "insert_block"
  | "replace_block"
  | "replace_image"
  | "update_style";

export interface NewBlockPayload {
  type: BlockType;
  content: BlockContent;
  style: BlockStyle;
}

export type PatchOperation =
  | {
      type: "update_block";
      block_id: string;
      content?: BlockContent | null;
      style?: BlockStyle | null;
      layout?: Partial<BlockLayout> | null;
    }
  | { type: "delete_block"; block_id: string }
  | {
      type: "insert_block";
      block: NewBlockPayload;
      after_block_id?: string | null;
      before_block_id?: string | null;
      page_number?: number | null;
    }
  | { type: "replace_block"; block_id: string; block: NewBlockPayload }
  | {
      type: "replace_image";
      block_id: string;
      prompt: string;
      caption?: string | null;
      alt?: string | null;
    }
  | { type: "update_style"; block_id: string; style: BlockStyle };

export interface DocumentPatch {
  operations: PatchOperation[];
  reasoning: string;
  notes: string;
}

export interface AiEditRequest {
  selected_block_ids: string[];
  instruction: string;
  apply?: boolean;
  regenerate_images?: boolean;
}

export interface AiEditResponse {
  document_id: string;
  version: number;
  applied: boolean;
  patch: DocumentPatch;
  applied_operations: string[];
  rejected_operations: Array<{ type: string; block_id: string | null; reason: string }>;
  pages: number;
}
