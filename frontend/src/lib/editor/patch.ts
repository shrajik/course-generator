/**
 * Applies a backend DocumentPatch to the local editor state.
 *
 * Semantics deliberately mirror backend/app/course/document/patcher.py:
 * `update_block.content` is merged shallowly, `replace_block` keeps the block id,
 * and an operation that cannot be applied is reported rather than thrown.
 */

import type {
  Block,
  CourseDocument,
  DocumentPatch,
  NewBlockPayload,
  PatchOperation,
} from "@/lib/types/document";
import { createBlock, locateBlock } from "./blocks";

export interface PatchResult {
  document: CourseDocument;
  applied: string[];
  rejected: Array<{ type: string; block_id: string | null; reason: string }>;
  imagesToGenerate: string[];
}

function materialise(payload: NewBlockPayload, reference: Block | null): Block {
  const base = createBlock(payload.type);
  return {
    ...base,
    content: { ...base.content, ...(payload.content ?? {}) },
    style: { ...(payload.style ?? {}) },
    layout: reference ? { ...reference.layout, y: reference.layout.y } : base.layout,
    meta: {
      origin: "inserted",
      chapter_id: reference?.meta.chapter_id ?? null,
      chapter_number: reference?.meta.chapter_number ?? null,
      section_key: reference?.meta.section_key ?? null,
    },
  };
}

export function applyPatch(
  input: CourseDocument,
  patch: DocumentPatch,
): PatchResult {
  const document: CourseDocument = structuredClone(input);
  const applied: string[] = [];
  const rejected: PatchResult["rejected"] = [];
  const imagesToGenerate: string[] = [];

  const reject = (operation: PatchOperation, reason: string) =>
    rejected.push({
      type: operation.type,
      block_id: "block_id" in operation ? operation.block_id : null,
      reason,
    });

  for (const operation of patch.operations) {
    switch (operation.type) {
      case "update_block": {
        const found = locateBlock(document, operation.block_id);
        if (!found) {
          reject(operation, "block not found");
          break;
        }
        const block = document.pages[found.pageIndex].blocks[found.blockIndex];
        if (operation.content) block.content = { ...block.content, ...operation.content };
        if (operation.style) block.style = { ...block.style, ...operation.style };
        if (operation.layout) block.layout = { ...block.layout, ...operation.layout };
        block.meta = { ...block.meta, origin: "edited" };
        applied.push(`update_block:${block.id}`);
        break;
      }

      case "update_style": {
        const found = locateBlock(document, operation.block_id);
        if (!found) {
          reject(operation, "block not found");
          break;
        }
        const block = document.pages[found.pageIndex].blocks[found.blockIndex];
        block.style = { ...block.style, ...operation.style };
        applied.push(`update_style:${block.id}`);
        break;
      }

      case "delete_block": {
        const found = locateBlock(document, operation.block_id);
        if (!found) {
          reject(operation, "block not found");
          break;
        }
        document.pages[found.pageIndex].blocks.splice(found.blockIndex, 1);
        applied.push(`delete_block:${operation.block_id}`);
        break;
      }

      case "replace_block": {
        const found = locateBlock(document, operation.block_id);
        if (!found) {
          reject(operation, "block not found");
          break;
        }
        const old = document.pages[found.pageIndex].blocks[found.blockIndex];
        const replacement = materialise(operation.block, old);
        replacement.id = old.id; // keep the id stable for the editor UI
        replacement.meta = { ...replacement.meta, origin: "edited" };
        document.pages[found.pageIndex].blocks[found.blockIndex] = replacement;
        if (replacement.type === "image" && !replacement.content.path) {
          imagesToGenerate.push(replacement.id);
        }
        applied.push(`replace_block:${replacement.id}`);
        break;
      }

      case "insert_block": {
        const anchorId = operation.after_block_id ?? operation.before_block_id ?? null;
        let pageIndex: number;
        let position: number;
        let reference: Block | null = null;

        if (anchorId) {
          const found = locateBlock(document, anchorId);
          if (!found) {
            reject(operation, "anchor block not found");
            break;
          }
          pageIndex = found.pageIndex;
          reference = found.block;
          position = operation.after_block_id ? found.blockIndex + 1 : found.blockIndex;
        } else {
          const target = document.pages.findIndex(
            (page) => page.page_number === operation.page_number,
          );
          if (target === -1) {
            reject(operation, "page_number not found");
            break;
          }
          pageIndex = target;
          const blocks = document.pages[target].blocks;
          reference = blocks.length ? blocks[blocks.length - 1] : null;
          position = blocks.length;
        }

        const block = materialise(operation.block, reference);
        if (reference) {
          block.layout = {
            ...block.layout,
            y: reference.layout.y + reference.layout.height + 18,
          };
        }
        document.pages[pageIndex].blocks.splice(position, 0, block);
        if (block.type === "image" && !block.content.path) imagesToGenerate.push(block.id);
        applied.push(`insert_block:${block.id}`);
        break;
      }

      case "replace_image": {
        const found = locateBlock(document, operation.block_id);
        if (!found) {
          reject(operation, "block not found");
          break;
        }
        const block = document.pages[found.pageIndex].blocks[found.blockIndex];
        if (block.type !== "image") {
          reject(operation, `block is ${block.type}, not an image`);
          break;
        }
        block.content = {
          ...block.content,
          prompt: operation.prompt,
          path: null,
          generated: false,
          error: null,
          ...(operation.caption !== undefined && operation.caption !== null
            ? { caption: operation.caption }
            : {}),
          ...(operation.alt !== undefined && operation.alt !== null
            ? { alt: operation.alt }
            : {}),
        };
        block.meta = { ...block.meta, origin: "edited" };
        imagesToGenerate.push(block.id);
        applied.push(`replace_image:${block.id}`);
        break;
      }

      default: {
        reject(operation, "unsupported operation");
      }
    }
  }

  if (applied.length > 0) {
    document.version += 1;
    document.updated_at = new Date().toISOString();
  }

  return { document, applied, rejected, imagesToGenerate };
}
