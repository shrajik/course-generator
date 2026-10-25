/**
 * The editor mirrors a template's design only when the template has one, so
 * built-in courses keep exactly the editor look they always had.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { editorTheme, resolvePlaceholders } from "./theme.ts";

const BUILT_IN = {
  font_family: "Inter, Segoe UI, Helvetica, Arial, sans-serif",
  text_color: "#0f172a",
  accent_color: "#2563eb",
  show_page_numbers: false, // the old stored default, which never hid numbers
};

test("a built-in theme changes nothing in the editor", () => {
  const theme = editorTheme(BUILT_IN, { course_title: "Intro" });
  assert.equal(theme.applied, false);
  assert.deepEqual(theme.style, {});
  assert.equal(theme.pageWidth, 794);
  assert.equal(theme.header, "");
});

test("no theme at all is the normal look too", () => {
  assert.equal(editorTheme(undefined, { course_title: "Intro" }).applied, false);
});

test("an applied template design reaches the editor: fonts, colours, header, footer, page size", () => {
  const theme = editorTheme(
    {
      font_family: "Montserrat, sans-serif",
      heading_font_family: "Playfair Display, serif",
      text_color: "#5a1f7a",
      heading_color: "#e91e63",
      header_text: "{{COURSE_TITLE}} | GLOW",
      footer_text: "",
      show_page_numbers: false,
      page: { width: 816, height: 1056, margin_x: 72 },
    },
    { course_title: "Baking Basics" },
  );
  assert.equal(theme.applied, true);
  const style = theme.style as Record<string, string>;
  assert.equal(style["--doc-font"], "Montserrat, sans-serif");
  assert.equal(style["--doc-heading-font"], "Playfair Display, serif");
  assert.equal(style["--doc-heading-ink"], "#e91e63");
  assert.equal(theme.header, "Baking Basics | GLOW");
  assert.equal(theme.footer, "Baking Basics"); // falls back to the title, like the PDF
  assert.equal(theme.showPageNumbers, false);
  assert.equal(theme.pageWidth, 816);
  assert.equal(theme.marginX, 72);
});

test("page numbers stay on unless the template explicitly turns them off", () => {
  const theme = editorTheme({ font_family: "Lora, serif" }, { course_title: "X" });
  assert.equal(theme.showPageNumbers, true);
});

test("an unknown placeholder stays visible instead of turning blank", () => {
  assert.equal(resolvePlaceholders("{{COURSE_TITLE}} - {{CHAPTER_TITLE}}", { course_title: "A" }), "A - {{CHAPTER_TITLE}}");
});
