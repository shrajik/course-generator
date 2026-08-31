/** Helpers for reading, writing and creating blocks in the Course Document. */

import {
  CONTENT_WIDTH,
  PAGE_MARGIN_X,
  type Block,
  type BlockContent,
  type BlockType,
  type CourseDocument,
  type Page,
} from "@/lib/types/document";

export const BLOCK_LABELS: Record<BlockType, string> = {
  heading: "Heading",
  paragraph: "Paragraph",
  image: "Image",
  quote: "Quote",
  callout: "Callout",
  code: "Code",
  table: "Table",
  quiz: "Quiz",
  exercise: "Exercise",
  case_study: "Case Study",
  story: "Story",
  tip: "Tip",
  warning: "Warning",
  summary: "Summary",
  divider: "Divider",
  learning_objectives: "Learning Objectives",
  challenge: "Challenge",
  reflection: "Reflection",
};

/** Block types whose primary styling is typography rather than a panel. */
export const TEXT_BLOCK_TYPES: BlockType[] = [
  "heading",
  "paragraph",
  "quote",
  "callout",
  "tip",
  "warning",
  "story",
  "summary",
  "learning_objectives",
  "reflection",
  "case_study",
  "exercise",
  "challenge",
  "quiz",
  "table",
];

export function isTextBlock(type: BlockType): boolean {
  return TEXT_BLOCK_TYPES.includes(type);
}

// --- path based content access ---------------------------------------------

/** Read a nested content value: `"text"`, `"items.2"`, `"rows.0.cells.1"`. */
export function getByPath(content: BlockContent, path: string): unknown {
  return path.split(".").reduce<unknown>((value, key) => {
    if (value === null || value === undefined) return undefined;
    if (Array.isArray(value)) return value[Number(key)];
    if (typeof value === "object") return (value as Record<string, unknown>)[key];
    return undefined;
  }, content);
}

/** Immutably set a nested content value using the same path syntax. */
export function setByPath<T extends object>(target: T, path: string, value: unknown): T {
  const keys = path.split(".");

  const walk = (node: unknown, index: number): unknown => {
    const key = keys[index];
    const last = index === keys.length - 1;

    if (Array.isArray(node)) {
      const copy = [...node];
      const position = Number(key);
      copy[position] = last ? value : walk(copy[position] ?? {}, index + 1);
      return copy;
    }

    const source = (node ?? {}) as Record<string, unknown>;
    const copy: Record<string, unknown> = { ...source };
    copy[key] = last ? value : walk(source[key] ?? (Number.isNaN(Number(keys[index + 1])) ? {} : []), index + 1);
    return copy;
  };

  return walk(target, 0) as T;
}

export function asString(value: unknown): string {
  return typeof value === "string" ? value : value === undefined || value === null ? "" : String(value);
}

export function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(asString) : [];
}

// --- document traversal -----------------------------------------------------

export interface BlockLocation {
  pageIndex: number;
  blockIndex: number;
  page: Page;
  block: Block;
}

export function locateBlock(
  document: CourseDocument,
  blockId: string,
): BlockLocation | null {
  for (let pageIndex = 0; pageIndex < document.pages.length; pageIndex += 1) {
    const page = document.pages[pageIndex];
    const blockIndex = page.blocks.findIndex((block) => block.id === blockId);
    if (blockIndex !== -1) {
      return { pageIndex, blockIndex, page, block: page.blocks[blockIndex] };
    }
  }
  return null;
}

export function allBlocks(document: CourseDocument): Block[] {
  return document.pages.flatMap((page) => page.blocks);
}

export function newBlockId(): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
      : Math.random().toString(16).slice(2, 14).padEnd(12, "0");
  return `block_${random}`;
}

// --- creation ---------------------------------------------------------------

const DEFAULT_CONTENT: Record<BlockType, BlockContent> = {
  heading: { text: "New heading", level: 2 },
  paragraph: { text: "New paragraph. Click to edit this text." },
  image: { purpose: "", prompt: "", caption: "", alt: "", path: null },
  quote: { text: "A quotation.", attribution: "" },
  callout: { title: "Did you know?", text: "Something worth noticing.", variant: "info" },
  code: { language: "python", code: "print('hello')", caption: "" },
  table: {
    caption: "",
    columns: ["Column A", "Column B"],
    rows: [{ cells: ["Value", "Value"] }],
  },
  quiz: {
    title: "Knowledge Check",
    questions: [
      {
        question: "New question?",
        kind: "multiple_choice",
        options: ["Option A", "Option B"],
        answer: "Option A",
        explanation: "",
      },
    ],
  },
  exercise: {
    title: "Exercise",
    instructions: "Describe the task here.",
    steps: [],
    hints: [],
    expected_outcome: "",
    difficulty: "medium",
  },
  case_study: {
    title: "Case Study",
    context: "",
    challenge: "",
    actions: [],
    outcome: "",
    lessons: [],
  },
  story: { title: "", text: "A short scenario.", takeaway: "" },
  tip: { title: "Pro Tip", text: "A practical tip." },
  warning: { title: "Common Mistake", text: "Something to avoid." },
  summary: { title: "Summary", key_takeaways: ["First takeaway"], next_steps: [] },
  divider: {},
  learning_objectives: { title: "Learning Objectives", items: ["First objective"], intro: "" },
  challenge: {
    title: "Challenge",
    instructions: "A harder extension task.",
    steps: [],
    hints: [],
    expected_outcome: "",
    difficulty: "hard",
  },
  reflection: { title: "Reflection", items: ["A question to sit with."], intro: "" },
};

const DEFAULT_HEIGHT: Partial<Record<BlockType, number>> = {
  heading: 44,
  paragraph: 80,
  image: 400,
  divider: 24,
  code: 120,
  table: 120,
  quiz: 200,
  exercise: 180,
  summary: 140,
  learning_objectives: 120,
  reflection: 110,
  case_study: 200,
  story: 140,
};

/** Block types the editor lets the user insert manually. */
export const INSERTABLE_TYPES: BlockType[] = [
  "heading",
  "paragraph",
  "image",
  "callout",
  "tip",
  "warning",
  "quote",
  "code",
  "table",
  "quiz",
  "exercise",
  "challenge",
  "summary",
  "learning_objectives",
  "reflection",
  "case_study",
  "story",
  "divider",
];

export function createBlock(type: BlockType, overrides: Partial<Block> = {}): Block {
  return {
    id: newBlockId(),
    type,
    content: structuredClone(DEFAULT_CONTENT[type]),
    style: {},
    layout: {
      x: PAGE_MARGIN_X,
      y: 0,
      width: CONTENT_WIDTH,
      height: DEFAULT_HEIGHT[type] ?? 90,
      z_index: 0,
    },
    meta: { origin: "inserted" },
    ...overrides,
  };
}

/** A short human label for the selection chip and AI Assistant. */
export function blockSummary(block: Block): string {
  const candidates = ["text", "title", "instructions", "caption", "purpose", "code", "context"];
  for (const key of candidates) {
    const value = block.content[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim().slice(0, 70);
    }
  }
  const items = block.content.items ?? block.content.key_takeaways;
  if (Array.isArray(items) && items.length) return asString(items[0]).slice(0, 70);
  return BLOCK_LABELS[block.type];
}
