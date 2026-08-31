/**
 * Turns a block's `style` into CSS.
 *
 * The rules mirror backend/app/render/html_renderer.py so the canvas is a
 * faithful preview of the exported PDF: panel-like blocks paint their
 * background/border chrome, text blocks only carry typography, and code puts its
 * chrome on an inner panel so the caption stays readable.
 */

import type { CSSProperties } from "react";
import type { Block, BlockStyle, BlockType } from "@/lib/types/document";

export const PANEL_TYPES: BlockType[] = [
  "callout",
  "tip",
  "warning",
  "quiz",
  "exercise",
  "challenge",
  "case_study",
  "summary",
  "learning_objectives",
  "reflection",
  "story",
  "image",
];

export function isPanel(type: BlockType): boolean {
  return PANEL_TYPES.includes(type);
}

export function typography(style: BlockStyle): CSSProperties {
  const css: CSSProperties = {};
  if (style.font_family) css.fontFamily = style.font_family;
  if (style.font_size) css.fontSize = `${style.font_size}px`;
  if (style.font_weight) css.fontWeight = style.font_weight;
  if (style.line_height) css.lineHeight = style.line_height;
  if (style.color) css.color = style.color;
  if (style.align) css.textAlign = style.align;
  if (style.italic) css.fontStyle = "italic";
  if (style.letter_spacing) css.letterSpacing = `${style.letter_spacing}px`;
  return css;
}

/** Style for the absolutely positioned box on the canvas. */
export function blockBoxStyle(block: Block): CSSProperties {
  const { layout, style, type } = block;
  const css: CSSProperties = {
    position: "absolute",
    left: layout.x,
    top: layout.y,
    width: layout.width,
    minHeight: layout.height,
    zIndex: layout.z_index || undefined,
  };

  if (type === "code") return css; // chrome lives on the inner panel

  Object.assign(css, typography(style));

  if (isPanel(type)) {
    if (style.background) css.background = style.background;
    if (style.border_color) {
      css.border = `${style.border_width ?? 1}px solid ${style.border_color}`;
    }
    if (style.border_radius) css.borderRadius = `${style.border_radius}px`;
    if (style.padding) css.padding = `${style.padding}px`;
  } else if (style.background) {
    css.background = style.background;
  }

  if (type === "image") css.overflow = "hidden";
  return css;
}

/** Chrome for the dark code panel. */
export function codePanelStyle(style: BlockStyle): CSSProperties {
  return {
    background: style.background ?? "#0F172A",
    color: style.color ?? "#E2E8F0",
    fontFamily: style.font_family ?? "var(--font-mono, JetBrains Mono, monospace)",
    fontSize: `${style.font_size ?? 12.5}px`,
    lineHeight: style.line_height ?? 1.5,
    borderRadius: `${style.border_radius ?? 10}px`,
    padding: `${style.padding ?? 16}px`,
    border: style.border_color
      ? `${style.border_width ?? 1}px solid ${style.border_color}`
      : undefined,
  };
}

export function accentColor(block: Block, fallback = "#6D3BEB"): string {
  return block.style.accent_color ?? fallback;
}

/** Reserved height for the picture itself, matching the backend estimator. */
export const MAX_IMAGE_HEIGHT = 430;
export const DEFAULT_IMAGE_ASPECT = 0.5625;

export function imageBoxHeight(block: Block): number {
  const padding = block.style.padding ?? 0;
  const width = (block.layout.width || 666) - 2 * padding;
  const intrinsicWidth = Number(block.content.width) || 0;
  const intrinsicHeight = Number(block.content.height) || 0;
  const aspect =
    intrinsicWidth > 0 && intrinsicHeight > 0
      ? intrinsicHeight / intrinsicWidth
      : DEFAULT_IMAGE_ASPECT;
  return Math.min(width * aspect, MAX_IMAGE_HEIGHT);
}
