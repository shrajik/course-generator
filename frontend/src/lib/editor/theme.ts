/**
 * The template design the editor mirrors from the PDF renderer
 * (backend/app/render/templates/course.html.j2): body and heading fonts,
 * text and heading colours, page background, page width, and the running
 * header/footer.
 *
 * Applied only when the document's template actually carries a design (an
 * uploaded template whose design was applied, or an admin template that sets
 * one). The built-in templates and uploads used for their rules only keep the
 * editor's normal look, exactly as before.
 */

import type { CSSProperties } from "react";
import { PAGE_MARGIN_X, PAGE_WIDTH } from "../types/document.ts";

const DEFAULT_FONT = "Inter, Segoe UI, Helvetica, Arial, sans-serif";

export interface EditorTheme {
  applied: boolean;
  /** CSS custom properties consumed by `.doc-themed` in globals.css. */
  style: CSSProperties;
  pageWidth: number;
  marginX: number;
  header: string;
  footer: string;
  showPageNumbers: boolean;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** `{{COURSE_TITLE}}` and friends, resolved the way the PDF does; anything
 * unknown stays visible as written, never silently blank. */
export function resolvePlaceholders(value: string, context: Record<string, string>): string {
  return value.replace(/\{\{\s*([A-Z0-9_]+)\s*\}\}/g, (match, name: string) => {
    const resolved = context[name.toLowerCase()];
    return resolved ? resolved : match;
  });
}

export function editorTheme(
  theme: Record<string, unknown> | null | undefined,
  context: { course_title: string; audience?: string },
): EditorTheme {
  const source = theme ?? {};
  const font = text(source.font_family);
  const headingFont = text(source.heading_font_family);
  const header = text(source.header_text);
  const footer = text(source.footer_text);
  const page = (source.page ?? null) as { width?: unknown; margin_x?: unknown } | null;
  const pageWidth = typeof page?.width === "number" && page.width > 200 ? page.width : PAGE_WIDTH;
  const marginX = typeof page?.margin_x === "number" && page.margin_x >= 0 ? page.margin_x : PAGE_MARGIN_X;

  const applied = Boolean(headingFont || header || footer || pageWidth !== PAGE_WIDTH || (font && font !== DEFAULT_FONT));
  if (!applied) {
    return { applied, style: {}, pageWidth: PAGE_WIDTH, marginX: PAGE_MARGIN_X, header: "", footer: "", showPageNumbers: false };
  }

  const resolveContext = { course_title: context.course_title, audience: context.audience ?? "" };
  const vars: Record<string, string> = {
    "--doc-font": font || DEFAULT_FONT,
    "--doc-heading-font": headingFont || font || DEFAULT_FONT,
  };
  const ink = text(source.text_color);
  const headingInk = text(source.heading_color);
  const muted = text(source.muted_color);
  const border = text(source.border_color);
  if (ink) vars["--doc-ink"] = ink;
  vars["--doc-heading-ink"] = headingInk || ink || "inherit";
  if (muted) vars["--doc-muted"] = muted;
  if (border) vars["--doc-border"] = border;

  return {
    applied,
    style: vars as CSSProperties,
    pageWidth,
    marginX,
    header: resolvePlaceholders(header, resolveContext),
    footer: resolvePlaceholders(footer, resolveContext) || context.course_title,
    // Unset keeps the page number, as the PDF does.
    showPageNumbers: source.show_page_numbers !== false,
  };
}
