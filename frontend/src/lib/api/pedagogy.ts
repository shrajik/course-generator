/**
 * Learner-archetype baselines for the Create Course form's sliders.
 *
 * Fetched rather than hardcoded so the positions the designer sees are the
 * same ones the writing agents are given - backend/app/course/pedagogy.py is
 * the single source of truth for both.
 */

import type { AgeGroup, EduLevel } from "@/lib/types/course";

import { request } from "./client";

export interface ArchetypeDefaults {
  age_group: AgeGroup;
  label: string;
  instructional_model: string;
  session_length: string;
  jargon_density: number;
  scaffolding_depth: number;
  gamification_index: number;
  chunk_word_cap: number;
}

export interface PedagogyDefaults {
  age_groups: ArchetypeDefaults[];
  edu_levels: Record<EduLevel, string>;
}

export async function getPedagogyDefaults(): Promise<PedagogyDefaults> {
  return request<PedagogyDefaults>("/api/pedagogy/defaults");
}
