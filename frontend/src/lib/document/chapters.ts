/**
 * Groups a document's blocks by chapter (Content Studio Phase 8, Output
 * Workspace). The backend already tags every block with the chapter it
 * belongs to (`Block.meta.chapter_id`/`chapter_number`, set for every block
 * in a chapter, not only its heading - see EditorToolbar.tsx's own existing
 * read of the same field). This walks every content page once and collects
 * every block per chapter, split back out per page so a chapter spanning
 * several pages renders as several page-sized boxes, same as the document
 * itself is paginated.
 */

import type { Block, CourseDocument, Page } from "@/lib/types/document";

export interface DocumentChapter {
  id: string;
  number: number;
  title: string;
  pages: { page: Page; blocks: Block[] }[];
}

export function chaptersFromDocument(document: CourseDocument): DocumentChapter[] {
  const order: string[] = [];
  const byId = new Map<string, DocumentChapter>();

  for (const page of document.pages) {
    if (page.kind !== "content") continue;
    const blocksByChapter = new Map<string, Block[]>();

    for (const block of page.blocks) {
      const id = block.meta.chapter_id;
      if (!id) continue;

      let chapter = byId.get(id);
      if (!chapter) {
        chapter = { id, number: block.meta.chapter_number ?? byId.size + 1, title: "Untitled", pages: [] };
        byId.set(id, chapter);
        order.push(id);
      }
      if (chapter.title === "Untitled" && block.type === "heading") {
        const text = String(block.content.text ?? "").trim();
        if (text) chapter.title = text;
      }

      const bucket = blocksByChapter.get(id);
      if (bucket) bucket.push(block);
      else blocksByChapter.set(id, [block]);
    }

    for (const [id, blocks] of blocksByChapter) {
      byId.get(id)!.pages.push({ page, blocks });
    }
  }

  return order.map((id) => byId.get(id)!).sort((a, b) => a.number - b.number);
}
