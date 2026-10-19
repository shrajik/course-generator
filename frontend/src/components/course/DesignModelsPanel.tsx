"use client";

/**
 * Picks the instructional-design models for a course: ADDIE, Bloom's
 * Taxonomy, SAM, Gagne's Nine Events and Kirkpatrick's Four Levels.
 *
 * Optional by design - nothing is selected until the designer opens the panel
 * and ticks a model, and `designModels` stays null until then, so a course made
 * without touching it generates exactly as it did before. Each model asks for
 * the inputs the client's methodology says it needs (SAM: loop type;
 * Kirkpatrick: the business goal), and the backend refuses a selection that is
 * missing them.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { BloomLevel, DesignModelId, DesignModels } from "@/lib/types/course";
import { cn } from "@/lib/utils/cn";

const BLANK: DesignModels = {
  models: [],
  sam_loop: null,
  sam_review_ready: "",
  microlearning: false,
  bloom_target_level: null,
  business_goal: "",
  data_sources: "lms_only",
};

const MODELS: { id: DesignModelId; name: string; description: string }[] = [
  {
    id: "addie",
    name: "ADDIE",
    description: "Analysis, Design, Development, Implementation, Evaluation - in that order.",
  },
  {
    id: "bloom",
    name: "Bloom's Taxonomy",
    description: "Measurable objectives with approved verbs, each matched to the right assessment.",
  },
  {
    id: "sam",
    name: "SAM",
    description: "Iterative: builds one module at a time and waits for review feedback.",
  },
  {
    id: "gagne",
    name: "Gagné's Nine Events",
    description: "Every chapter becomes one lesson with the nine labelled events, in order.",
  },
  {
    id: "kirkpatrick",
    name: "Kirkpatrick's Four Levels",
    description: "Adds an evaluation plan: reaction, learning, behavior and results.",
  },
];

const BLOOM_LEVELS: BloomLevel[] = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"];

const fieldClass =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink-800 outline-none " +
  "transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";
const labelClass = "mb-1.5 block text-[12.5px] font-medium text-ink-700";

interface Props {
  value: DesignModels | null;
  onChange: (value: DesignModels | null) => void;
}

export function DesignModelsPanel({ value, onChange }: Props) {
  const [open, setOpen] = useState((value?.models.length ?? 0) > 0);
  const design = value ?? BLANK;
  const has = (id: DesignModelId) => design.models.includes(id);

  const patch = (changes: Partial<DesignModels>) => {
    const next = { ...design, ...changes };
    // Nothing selected means nothing is sent at all.
    onChange(next.models.length === 0 ? null : next);
  };

  const toggle = (id: DesignModelId) => {
    if (has(id)) {
      patch({ models: design.models.filter((model) => model !== id) });
      return;
    }
    patch({
      models: [...design.models, id],
      // SAM needs a loop type; start on the small-project loop so the course
      // is valid as soon as it is ticked.
      ...(id === "sam" && design.sam_loop === null ? { sam_loop: "SAM1" as const } : {}),
    });
  };

  return (
    <div className="rounded-card border border-line">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <span>
          <span className="text-[12.5px] font-medium text-ink-700">Instructional Design Models</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {design.models.length > 0
              ? `${design.models.length} selected`
              : "Optional — pick ADDIE, Bloom, SAM, Gagné or Kirkpatrick"}
          </span>
        </span>
        {open ? (
          <ChevronDown size={16} className="shrink-0 text-ink-400" aria-hidden />
        ) : (
          <ChevronRight size={16} className="shrink-0 text-ink-400" aria-hidden />
        )}
      </button>

      {open ? (
        <div className="space-y-4 border-t border-line px-4 py-4">
          <ul className="grid gap-2 sm:grid-cols-2">
            {MODELS.map((model) => (
              <li key={model.id}>
                <label
                  className={cn(
                    "flex cursor-pointer gap-2.5 rounded-lg border px-3 py-2.5 transition",
                    has(model.id) ? "border-brand-400 bg-brand-50" : "border-line hover:border-brand-200",
                  )}
                >
                  <input
                    type="checkbox"
                    checked={has(model.id)}
                    onChange={() => toggle(model.id)}
                    className="mt-0.5"
                  />
                  <span>
                    <span className="block text-[12.5px] font-medium text-ink-800">{model.name}</span>
                    <span className="block text-[11.5px] leading-snug text-ink-500">{model.description}</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>

          {has("bloom") ? (
            <div>
              <label htmlFor="bloom-level" className={labelClass}>
                Target cognitive level (optional)
              </label>
              <select
                id="bloom-level"
                className={fieldClass}
                value={design.bloom_target_level ?? ""}
                onChange={(event) =>
                  patch({ bloom_target_level: (event.target.value || null) as BloomLevel | null })
                }
              >
                <option value="">Full spread of levels</option>
                {BLOOM_LEVELS.map((level) => (
                  <option key={level} value={level}>
                    {level}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          {has("gagne") ? (
            <label className="flex items-center gap-2 text-[12.5px] text-ink-700">
              <input
                type="checkbox"
                checked={design.microlearning}
                onChange={(event) => patch({ microlearning: event.target.checked })}
              />
              Microlearning format (keeps events 2, 4, 6 and 8 and notes what was compressed)
            </label>
          ) : null}

          {has("sam") ? (
            <div className="grid gap-3 sm:grid-cols-[180px_1fr]">
              <div>
                <label htmlFor="sam-loop" className={labelClass}>
                  SAM loop
                </label>
                <select
                  id="sam-loop"
                  className={fieldClass}
                  value={design.sam_loop ?? "SAM1"}
                  onChange={(event) => patch({ sam_loop: event.target.value as "SAM1" | "SAM2" })}
                >
                  <option value="SAM1">SAM1 – small project</option>
                  <option value="SAM2">SAM2 – larger project</option>
                </select>
              </div>
              <div>
                <label htmlFor="sam-ready" className={labelClass}>
                  What does “good enough to review” look like? (optional)
                </label>
                <input
                  id="sam-ready"
                  className={fieldClass}
                  value={design.sam_review_ready}
                  onChange={(event) => patch({ sam_review_ready: event.target.value })}
                  placeholder="for example: a clean outline and one worked example"
                />
              </div>
            </div>
          ) : null}

          {has("kirkpatrick") ? (
            <div className="grid gap-3 sm:grid-cols-[1fr_220px]">
              <div>
                <label htmlFor="biz-goal" className={labelClass}>
                  Business or performance goal <span className="text-red-600">*</span>
                </label>
                <input
                  id="biz-goal"
                  className={cn(fieldClass, !design.business_goal.trim() && "border-amber-300")}
                  value={design.business_goal}
                  onChange={(event) => patch({ business_goal: event.target.value })}
                  placeholder="for example: reduce refund-processing errors"
                />
                {!design.business_goal.trim() ? (
                  <p className="mt-1 text-[11.5px] text-amber-700">Required for the evaluation plan.</p>
                ) : null}
              </div>
              <div>
                <label htmlFor="data-sources" className={labelClass}>
                  Data available
                </label>
                <select
                  id="data-sources"
                  className={fieldClass}
                  value={design.data_sources}
                  onChange={(event) =>
                    patch({ data_sources: event.target.value as "lms_only" | "lms_and_kpis" })
                  }
                >
                  <option value="lms_only">LMS data only</option>
                  <option value="lms_and_kpis">LMS + manager observation / KPIs</option>
                </select>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
