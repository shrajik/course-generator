"use client";

/**
 * Block type -> renderer. Adding a block type means adding one entry here and
 * one component file; nothing else in the editor needs to change.
 */

import type { ComponentType } from "react";
import type { BlockType } from "@/lib/types/document";
import type { BlockViewProps } from "./types";

import { CalloutBlock } from "./CalloutBlock";
import { CaseStudyBlock } from "./CaseStudyBlock";
import { CodeBlock } from "./CodeBlock";
import { DividerBlock } from "./DividerBlock";
import { ExerciseBlock } from "./ExerciseBlock";
import { HeadingBlock } from "./HeadingBlock";
import { ImageBlock } from "./ImageBlock";
import { ListBlock } from "./ListBlock";
import { ParagraphBlock } from "./ParagraphBlock";
import { QuizBlock } from "./QuizBlock";
import { QuoteBlock } from "./QuoteBlock";
import { StoryBlock } from "./StoryBlock";
import { SummaryBlock } from "./SummaryBlock";
import { TableBlock } from "./TableBlock";

export const BLOCK_REGISTRY: Record<BlockType, ComponentType<BlockViewProps>> = {
  heading: HeadingBlock,
  paragraph: ParagraphBlock,
  image: ImageBlock,
  quote: QuoteBlock,
  callout: CalloutBlock,
  code: CodeBlock,
  table: TableBlock,
  quiz: QuizBlock,
  exercise: ExerciseBlock,
  case_study: CaseStudyBlock,
  story: StoryBlock,
  tip: CalloutBlock,
  warning: CalloutBlock,
  summary: SummaryBlock,
  divider: DividerBlock,
  learning_objectives: ListBlock,
  challenge: ExerciseBlock,
  reflection: ListBlock,
};

export function getBlockRenderer(type: BlockType): ComponentType<BlockViewProps> {
  return BLOCK_REGISTRY[type] ?? ParagraphBlock;
}
