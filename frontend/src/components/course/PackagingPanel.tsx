"use client";

/**
 * Picks a packaging/delivery-tracking standard for a course: SCORM 1.2/2004,
 * xAPI, cmi5, AICC or LTI, plus the target LMS and authoring tool.
 *
 * Optional by design - `packaging` stays null until the designer opens the
 * panel and picks a standard, so a course made without touching it keeps
 * app.course.compliance's existing "no packaging standard selected"
 * behaviour exactly. This panel only records the choice for the compliance
 * report; it does not export an actual SCORM/xAPI package.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { AuthoringTool, PackagingSettings, PackagingStandard } from "@/lib/types/course";

const BLANK: PackagingSettings = {
  standard: null,
  target_lms: "",
  authoring_tool: null,
  track_beyond_lms: false,
  lrs_confirmed: false,
};

const STANDARDS: { id: PackagingStandard; label: string }[] = [
  { id: "scorm_1_2", label: "SCORM 1.2" },
  { id: "scorm_2004", label: "SCORM 2004" },
  { id: "xapi", label: "xAPI" },
  { id: "cmi5", label: "cmi5" },
  { id: "aicc", label: "AICC" },
  { id: "lti", label: "LTI" },
];

const AUTHORING_TOOLS: { id: AuthoringTool; label: string }[] = [
  { id: "articulate_storyline", label: "Articulate Storyline" },
  { id: "rise_360", label: "Rise 360" },
  { id: "captivate", label: "Captivate" },
  { id: "custom_html5", label: "Custom HTML5" },
];

const STANDARD_LABELS: Record<PackagingStandard, string> = Object.fromEntries(
  STANDARDS.map((s) => [s.id, s.label]),
) as Record<PackagingStandard, string>;

const fieldClass =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink-800 outline-none " +
  "transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";
const labelClass = "mb-1.5 block text-[12.5px] font-medium text-ink-700";

interface Props {
  value: PackagingSettings | null;
  onChange: (value: PackagingSettings | null) => void;
}

/** Back to "not chosen" when nothing differs from the defaults. */
function isDefault(packaging: PackagingSettings): boolean {
  return (
    packaging.standard === null &&
    packaging.target_lms.trim() === "" &&
    packaging.authoring_tool === null &&
    !packaging.track_beyond_lms &&
    !packaging.lrs_confirmed
  );
}

export function PackagingPanel({ value, onChange }: Props) {
  const [open, setOpen] = useState(value !== null);
  const packaging = value ?? BLANK;

  const patch = (changes: Partial<PackagingSettings>) => {
    const next = { ...packaging, ...changes };
    onChange(isDefault(next) ? null : next);
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
          <span className="text-[12.5px] font-medium text-ink-700">Packaging &amp; Tracking</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {packaging.standard
              ? STANDARD_LABELS[packaging.standard]
              : "Optional — SCORM, xAPI, cmi5, AICC or LTI"}
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
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor="pkg-standard" className={labelClass}>
                Packaging standard
              </label>
              <select
                id="pkg-standard"
                className={fieldClass}
                value={packaging.standard ?? ""}
                onChange={(event) =>
                  patch({ standard: (event.target.value || null) as PackagingStandard | null })
                }
              >
                <option value="">Not selected</option>
                {STANDARDS.map((standard) => (
                  <option key={standard.id} value={standard.id}>
                    {standard.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="pkg-tool" className={labelClass}>
                Authoring tool
              </label>
              <select
                id="pkg-tool"
                className={fieldClass}
                value={packaging.authoring_tool ?? ""}
                onChange={(event) =>
                  patch({ authoring_tool: (event.target.value || null) as AuthoringTool | null })
                }
              >
                <option value="">Not selected</option>
                {AUTHORING_TOOLS.map((tool) => (
                  <option key={tool.id} value={tool.id}>
                    {tool.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label htmlFor="pkg-lms" className={labelClass}>
              Target LMS or delivery platform (optional)
            </label>
            <input
              id="pkg-lms"
              className={fieldClass}
              value={packaging.target_lms}
              onChange={(event) => patch({ target_lms: event.target.value })}
              placeholder="for example: Cornerstone, Moodle, Docebo"
            />
          </div>

          <label className="flex items-center gap-2 text-[12.5px] text-ink-700">
            <input
              type="checkbox"
              checked={packaging.track_beyond_lms}
              onChange={(event) => patch({ track_beyond_lms: event.target.checked })}
            />
            Track beyond the LMS session (mobile, simulations, real-world practice)
          </label>
          {packaging.track_beyond_lms ? (
            <div className="ml-6 space-y-2">
              <p className="text-[11.5px] text-amber-700">
                Off-LMS tracking needs xAPI or cmi5 — SCORM alone can&apos;t record it.
              </p>
              <label className="flex items-center gap-2 text-[12.5px] text-ink-700">
                <input
                  type="checkbox"
                  checked={packaging.lrs_confirmed}
                  onChange={(event) => patch({ lrs_confirmed: event.target.checked })}
                />
                Learning Record Store confirmed
              </label>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
