"use client";

/**
 * The brief's own task list, business/performance goal, performance gap,
 * and companion deliverables (Content Studio Phase 1/4) - the last of the
 * backend's additive CourseInput fields from Phase 1 that weren't yet
 * exposed anywhere in the UI.
 *
 * Unlike the other optional panels (DesignModelsPanel, StyleGuidePanel,
 * PackagingPanel, ...), these four are separate top-level CourseDraft
 * fields rather than one combined object, so this panel takes each value
 * directly and reports changes via the same `update(patch)` function the
 * rest of the form already uses - `onChange={update}` wires it with no
 * adapter needed.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { CompanionDeliverable } from "@/lib/types/course";

const COMPANIONS: { id: CompanionDeliverable; label: string; help: string }[] = [
  { id: "job_aid", label: "Job aid / checklist", help: "Generated alongside the course, not instead of it" },
  { id: "quick_ref", label: "Quick-reference card", help: "" },
  {
    id: "manager_guide",
    label: "Manager coaching guide",
    help: "For content better delivered on the job (70-20-10)",
  },
];

const fieldClass =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink-800 outline-none " +
  "transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";
const labelClass = "mb-1.5 block text-[12.5px] font-medium text-ink-700";

export interface BriefDetailsChange {
  tasks: string[];
  businessGoal: string;
  performanceGap: string;
  companions: CompanionDeliverable[];
}

interface Props {
  tasks: string[];
  businessGoal: string;
  performanceGap: string;
  companions: CompanionDeliverable[];
  onChange: (changes: Partial<BriefDetailsChange>) => void;
}

function isTouched(props: Pick<Props, "tasks" | "businessGoal" | "performanceGap" | "companions">): boolean {
  return (
    props.tasks.length > 0 ||
    props.businessGoal.trim() !== "" ||
    props.performanceGap.trim() !== "" ||
    props.companions.length > 0
  );
}

export function BriefDetailsPanel({ tasks, businessGoal, performanceGap, companions, onChange }: Props) {
  const [open, setOpen] = useState(isTouched({ tasks, businessGoal, performanceGap, companions }));

  const toggleCompanion = (id: CompanionDeliverable) => {
    onChange({
      companions: companions.includes(id) ? companions.filter((item) => item !== id) : [...companions, id],
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
          <span className="text-[12.5px] font-medium text-ink-700">Brief Details</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {isTouched({ tasks, businessGoal, performanceGap, companions })
              ? `${tasks.length} task(s)${companions.length > 0 ? `, ${companions.length} companion(s)` : ""}`
              : "Optional — tasks, business goal, companion deliverables"}
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
          <div>
            <label htmlFor="brief-tasks" className={labelClass}>
              Tasks the learner must be able to do{" "}
              <span className="font-normal text-ink-400">
                — one per line, each becomes at least one learning objective
              </span>
            </label>
            <textarea
              id="brief-tasks"
              className={fieldClass}
              rows={4}
              value={tasks.join("\n")}
              onChange={(event) =>
                onChange({
                  tasks: event.target.value
                    .split("\n")
                    .map((line) => line.trim())
                    .filter(Boolean),
                })
              }
              placeholder={"Recognise a reportable data incident\nClassify the incident by severity"}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor="brief-goal" className={labelClass}>
                Business / performance goal (optional)
              </label>
              <input
                id="brief-goal"
                className={fieldClass}
                value={businessGoal}
                onChange={(event) => onChange({ businessGoal: event.target.value })}
                placeholder="for example: fewer late or incomplete reports"
              />
            </div>
            <div>
              <label htmlFor="brief-gap" className={labelClass}>
                Performance gap (optional)
              </label>
              <input
                id="brief-gap"
                className={fieldClass}
                value={performanceGap}
                onChange={(event) => onChange({ performanceGap: event.target.value })}
                placeholder="Left blank, the Analysis phase drafts one"
              />
            </div>
          </div>

          <div>
            <span className={labelClass}>Companion deliverables</span>
            <ul className="space-y-1.5">
              {COMPANIONS.map((companion) => (
                <li key={companion.id}>
                  <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-line px-3 py-2 text-[12.5px] text-ink-700 hover:border-brand-200">
                    <input
                      type="checkbox"
                      checked={companions.includes(companion.id)}
                      onChange={() => toggleCompanion(companion.id)}
                      className="mt-0.5"
                    />
                    <span>
                      <span className="block font-medium text-ink-800">{companion.label}</span>
                      {companion.help ? (
                        <span className="block text-[11px] text-ink-400">{companion.help}</span>
                      ) : null}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
