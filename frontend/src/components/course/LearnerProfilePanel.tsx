"use client";

/**
 * Panels A-C of the creator interface: learner demographics, the cognitive
 * bridge, and the style sliders.
 *
 * Optional by design - the panel starts collapsed and `learnerProfile` stays
 * null until the designer opens it, so a course created without touching it
 * generates exactly as it did before this feature existed.
 *
 * Picking an age group snaps the sliders to that archetype's baseline
 * (client UAT 5.1). Moving a slider by hand sets `manual_override`, after
 * which changing the age group stops overwriting the designer's positions.
 * The baselines come from the backend so they cannot drift away from the
 * rules the writing agents are actually given.
 */

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, ChevronDown, ChevronRight, RotateCcw } from "lucide-react";
import { getPedagogyDefaults } from "@/lib/api/pedagogy";
import type { ArchetypeDefaults, PedagogyDefaults } from "@/lib/api/pedagogy";
import { useAuth } from "@/lib/auth/auth-provider";
import type { AgeGroup, EduLevel, LearnerProfile } from "@/lib/types/course";

/** Matches the backend field defaults in app/schemas/learner.py. Used only
 * until the archetype baselines arrive, and as the shape for a fresh profile. */
const BLANK_PROFILE: LearnerProfile = {
  age_group: "PROFESSIONAL",
  edu_level: "BACHELORS",
  source_domain: "",
  target_domain: "",
  jargon_density: 0.5,
  scaffolding_depth: 0.5,
  gamification_index: 0.2,
  chunk_word_cap: 450,
  manual_override: false,
};

const EDU_LEVELS: EduLevel[] = ["PRIMARY", "SECONDARY", "BACHELORS", "MASTERS_PHD"];

const selectClass =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink-800 " +
  "outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";

const labelClass = "mb-1.5 block text-[12.5px] font-medium text-ink-700";

interface Props {
  value: LearnerProfile | null;
  onChange: (profile: LearnerProfile | null) => void;
}

export function LearnerProfilePanel({ value, onChange }: Props) {
  const [open, setOpen] = useState(value !== null);
  const [defaults, setDefaults] = useState<PedagogyDefaults | null>(null);
  const [failed, setFailed] = useState(false);
  // The archetype baselines are an authenticated read, and AuthProvider may
  // still be refreshing an expired token on first paint. Firing before that
  // settles returns 401, which previously left the age-bracket dropdown
  // permanently empty - so wait for a session rather than racing it.
  const { isAuthenticated, isLoading: authLoading } = useAuth();

  const loadDefaults = useCallback(async () => {
    setFailed(false);
    try {
      setDefaults(await getPedagogyDefaults());
    } catch {
      // Surfaced in the panel with a Retry - never swallowed, because
      // without these the sliders and dropdown cannot work at all.
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    if (!open || defaults || failed || !isAuthenticated) return;
    void loadDefaults();
  }, [open, defaults, failed, isAuthenticated, loadDefaults]);

  const profile = value ?? BLANK_PROFILE;
  const pending = !defaults && (authLoading || isAuthenticated) && !failed;
  const archetype = defaults?.age_groups.find((item) => item.age_group === profile.age_group);

  const patch = (changes: Partial<LearnerProfile>) => onChange({ ...profile, ...changes });

  const handleToggle = () => {
    const next = !open;
    setOpen(next);
    // Opening the panel is what opts this course into the pedagogy rules;
    // closing it opts back out rather than leaving a hidden profile applied.
    onChange(next ? profile : null);
  };

  const handleAgeGroup = (ageGroup: AgeGroup) => {
    const baseline = defaults?.age_groups.find((item) => item.age_group === ageGroup);
    if (!baseline || profile.manual_override) {
      patch({ age_group: ageGroup });
      return;
    }
    patch({
      age_group: ageGroup,
      jargon_density: baseline.jargon_density,
      scaffolding_depth: baseline.scaffolding_depth,
      gamification_index: baseline.gamification_index,
      chunk_word_cap: baseline.chunk_word_cap,
    });
  };

  const resetToBaseline = (baseline: ArchetypeDefaults) =>
    patch({
      jargon_density: baseline.jargon_density,
      scaffolding_depth: baseline.scaffolding_depth,
      gamification_index: baseline.gamification_index,
      chunk_word_cap: baseline.chunk_word_cap,
      manual_override: false,
    });

  return (
    <div className="rounded-card border border-line">
      <button
        type="button"
        onClick={handleToggle}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <span>
          <span className="text-[12.5px] font-medium text-ink-700">Learner Profile</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {open
              ? "Applies instructional-model rules to this course"
              : "Optional — adds age-group and domain-aware teaching rules"}
          </span>
        </span>
        {open ? (
          <ChevronDown size={16} className="shrink-0 text-ink-400" aria-hidden />
        ) : (
          <ChevronRight size={16} className="shrink-0 text-ink-400" aria-hidden />
        )}
      </button>

      {open ? (
        <div className="space-y-5 border-t border-line px-4 py-4">
          {failed ? (
            <div className="flex items-start gap-2.5 rounded-lg bg-amber-50 px-3 py-2.5 text-[11.5px] text-amber-800">
              <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden />
              <p>
                Could not load the learner-archetype settings, so the age bracket and sliders
                are unavailable.{" "}
                <button
                  type="button"
                  onClick={() => void loadDefaults()}
                  className="font-medium underline"
                >
                  Retry
                </button>
              </p>
            </div>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="age-group" className={labelClass}>
                Age Bracket
              </label>
              <select
                id="age-group"
                className={selectClass}
                value={profile.age_group}
                disabled={!defaults}
                onChange={(event) => handleAgeGroup(event.target.value as AgeGroup)}
              >
                {(defaults?.age_groups ?? []).map((item) => (
                  <option key={item.age_group} value={item.age_group}>
                    {item.label}
                  </option>
                ))}
                {/* Disabled above, so this placeholder can never be mistaken
                    for a working control that simply has one option. */}
                {defaults ? null : (
                  <option value={profile.age_group}>
                    {pending ? "Loading…" : "Unavailable"}
                  </option>
                )}
              </select>
              {archetype ? (
                <p className="mt-1.5 text-[11.5px] text-ink-400">
                  {archetype.instructional_model} · {archetype.session_length}
                </p>
              ) : null}
            </div>

            <div>
              <label htmlFor="edu-level" className={labelClass}>
                Educational Attainment
              </label>
              <select
                id="edu-level"
                className={selectClass}
                value={profile.edu_level}
                onChange={(event) => patch({ edu_level: event.target.value as EduLevel })}
              >
                {EDU_LEVELS.map((level) => (
                  <option key={level} value={level}>
                    {defaults?.edu_levels[level] ?? level}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="source-domain" className={labelClass}>
                Learner&rsquo;s Background Domain
              </label>
              <input
                id="source-domain"
                className="field"
                value={profile.source_domain}
                onChange={(event) => patch({ source_domain: event.target.value })}
                placeholder="STEM, Commerce, Humanities…"
              />
            </div>
            <div>
              <label htmlFor="target-domain" className={labelClass}>
                Course Domain
              </label>
              <input
                id="target-domain"
                className="field"
                value={profile.target_domain}
                onChange={(event) => patch({ target_domain: event.target.value })}
                placeholder="Finance, Software Engineering…"
              />
            </div>
          </div>

          <BridgeNotice profile={profile} />

          <div className="space-y-4">
            <div className="flex items-center justify-between gap-3">
              <p className="text-[12.5px] font-medium text-ink-700">Methodology &amp; Style</p>
              {profile.manual_override && archetype ? (
                <button
                  type="button"
                  onClick={() => resetToBaseline(archetype)}
                  className="flex items-center gap-1.5 text-[11.5px] text-brand-600 hover:underline"
                >
                  <RotateCcw size={12} aria-hidden />
                  Reset to baseline
                </button>
              ) : null}
            </div>

            <Slider
              id="jargon-density"
              label="Jargon Density"
              low="Plain language"
              high="Industry native"
              value={profile.jargon_density}
              disabled={!defaults}
              onChange={(jargon_density) => patch({ jargon_density, manual_override: true })}
            />
            <Slider
              id="scaffolding-depth"
              label="Scaffolding Depth"
              low="Independent case studies"
              high="Step-by-step guidance"
              value={profile.scaffolding_depth}
              disabled={!defaults}
              onChange={(scaffolding_depth) => patch({ scaffolding_depth, manual_override: true })}
            />
            <Slider
              id="gamification-index"
              label="Gamification"
              low="Strictly academic"
              high="Deep story-driven"
              value={profile.gamification_index}
              disabled={!defaults}
              onChange={(gamification_index) =>
                patch({ gamification_index, manual_override: true })
              }
            />

            <div>
              <label htmlFor="chunk-word-cap" className={labelClass}>
                Delivery Chunk Size
                <span className="ml-2 font-normal text-ink-400">
                  {profile.chunk_word_cap} words max per block
                </span>
              </label>
              <input
                id="chunk-word-cap"
                type="range"
                min={50}
                max={1200}
                step={25}
                value={profile.chunk_word_cap}
                disabled={!defaults}
                onChange={(event) =>
                  patch({ chunk_word_cap: Number(event.target.value), manual_override: true })
                }
                className="w-full accent-brand-600 disabled:opacity-40"
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Mirrors `domain_match_index` in backend/app/course/pedagogy.py: a blank or
 * "none" source domain means there is nothing to bridge from. */
function BridgeNotice({ profile }: { profile: LearnerProfile }) {
  const source = profile.source_domain.trim().toLowerCase();
  const target = profile.target_domain.trim().toLowerCase();
  if (!source || !target || source === "none" || source === "n/a") return null;

  const crossDomain = source !== target;
  return (
    <p
      className={
        crossDomain
          ? "rounded-lg bg-brand-50 px-3 py-2 text-[11.5px] text-brand-700"
          : "rounded-lg bg-cream-100 px-3 py-2 text-[11.5px] text-ink-500"
      }
    >
      {crossDomain
        ? "Cross-Domain Bridge Track: new concepts are introduced through analogies from the learner's own field, and technical terms are tagged for on-demand definitions."
        : "Expert Track: foundational summaries and introductory analogies are skipped in favour of direct application."}
    </p>
  );
}

function Slider({
  id,
  label,
  low,
  high,
  value,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  low: string;
  high: string;
  value: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <div>
      <label htmlFor={id} className={labelClass}>
        {label}
        <span className="ml-2 font-normal text-ink-400">{value.toFixed(2)}</span>
      </label>
      <input
        id={id}
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
        className="w-full accent-brand-600 disabled:opacity-40"
      />
      <div className="mt-1 flex justify-between text-[11px] text-ink-400">
        <span>{low}</span>
        <span>{high}</span>
      </div>
    </div>
  );
}
