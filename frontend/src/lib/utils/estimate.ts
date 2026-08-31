import type { CourseBlueprint, TocItem } from "@/lib/types/course";

export interface CourseEstimate {
  chapters: number;
  sections: number;
  pages: number;
  hoursLow: number;
  hoursHigh: number;
  source: "blueprint" | "toc" | "document";
}

const PAGES_PER_CHAPTER = 3;
const PAGES_PER_SECTION = 3;
const WORDS_PER_PAGE = 350;
const PAGES_PER_HOUR = 15;

function hours(pages: number): { hoursLow: number; hoursHigh: number } {
  const low = Math.max(1, Math.round(pages / PAGES_PER_HOUR));
  return { hoursLow: low, hoursHigh: low + 2 };
}

/** Derived from the TOC the user is editing (no blueprint exists yet). */
export function estimateFromToc(toc: TocItem[]): CourseEstimate {
  const chapters = toc.length;
  const sections = toc.reduce((total, item) => total + item.sections.length, 0);
  const pages = chapters * PAGES_PER_CHAPTER + sections * PAGES_PER_SECTION;
  return { chapters, sections, pages, ...hours(pages), source: "toc" };
}

/** Preferred once the planner has run: uses the blueprint's own word estimates. */
export function estimateFromBlueprint(blueprint: CourseBlueprint): CourseEstimate {
  const chapters = blueprint.chapters.length;
  const sections = blueprint.chapters.reduce(
    (total, chapter) => total + chapter.sections.length,
    0,
  );
  const words = blueprint.chapters.reduce(
    (total, chapter) => total + (chapter.estimated_words || 0),
    0,
  );
  const pages = words
    ? Math.round(words / WORDS_PER_PAGE)
    : chapters * PAGES_PER_CHAPTER + sections * PAGES_PER_SECTION;
  return { chapters, sections, pages, ...hours(pages), source: "blueprint" };
}

/** Round to the nearest 10 for display, matching "~120 Pages" in the design. */
export function displayPages(pages: number): string {
  if (pages <= 0) return "—";
  const rounded = pages < 20 ? pages : Math.round(pages / 10) * 10;
  return `~${rounded} Pages`;
}
