"use client";

/**
 * Picks accessibility settings for a course: the WCAG compliance target,
 * additional regulatory requirements (Section 508, EN 301 549, PDF/UA), and
 * extra brand colours to contrast-check beyond the document's own theme.
 *
 * Optional by design - `accessibility` stays null until the designer opens
 * the panel and changes something, so a course made without touching it
 * keeps app.course.compliance's existing fixed-target, theme-colours-only
 * behaviour exactly. The actual contrast math runs server-side in the
 * compliance report (app.course.compliance.contrast_ratio) - this panel
 * only collects which colours to check, so there's one source of truth for
 * the WCAG formula.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import type {
  AccessibilitySettings,
  AccessibilityTarget,
  ExtraAccessibilityRequirement,
} from "@/lib/types/course";

const BLANK: AccessibilitySettings = {
  compliance_target: "wcag22_aa",
  extra_requirements: [],
  brand_colors: [],
};

const TARGETS: { id: AccessibilityTarget; label: string }[] = [
  { id: "wcag21_aa", label: "WCAG 2.1 AA" },
  { id: "wcag22_aa", label: "WCAG 2.2 AA (default)" },
];

const EXTRA_REQUIREMENTS: { id: ExtraAccessibilityRequirement; label: string; help: string }[] = [
  { id: "section_508", label: "Section 508", help: "US federal clients" },
  { id: "en_301_549", label: "EN 301 549", help: "EU public sector" },
  { id: "pdf_ua", label: "PDF/UA", help: "Any PDF deliverable" },
];

const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

const fieldClass =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink-800 outline-none " +
  "transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";
const labelClass = "mb-1.5 block text-[12.5px] font-medium text-ink-700";

interface Props {
  value: AccessibilitySettings | null;
  onChange: (value: AccessibilitySettings | null) => void;
}

/** Back to "not chosen" when nothing differs from the defaults. */
function isDefault(settings: AccessibilitySettings): boolean {
  return (
    settings.compliance_target === "wcag22_aa" &&
    settings.extra_requirements.length === 0 &&
    settings.brand_colors.length === 0
  );
}

export function AccessibilityPanel({ value, onChange }: Props) {
  const [open, setOpen] = useState(value !== null);
  const [colorDraft, setColorDraft] = useState("");
  const settings = value ?? BLANK;

  const patch = (changes: Partial<AccessibilitySettings>) => {
    const next = { ...settings, ...changes };
    onChange(isDefault(next) ? null : next);
  };

  const toggleExtra = (id: ExtraAccessibilityRequirement) => {
    patch({
      extra_requirements: settings.extra_requirements.includes(id)
        ? settings.extra_requirements.filter((item) => item !== id)
        : [...settings.extra_requirements, id],
    });
  };

  const addColor = () => {
    const color = colorDraft.trim();
    setColorDraft("");
    if (!HEX_RE.test(color)) return;
    if (settings.brand_colors.some((existing) => existing.toLowerCase() === color.toLowerCase())) return;
    patch({ brand_colors: [...settings.brand_colors, color] });
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
          <span className="text-[12.5px] font-medium text-ink-700">Accessibility</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {value
              ? TARGETS.find((t) => t.id === settings.compliance_target)?.label
              : "Optional — WCAG target, extra requirements, brand colours"}
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
            <label htmlFor="a11y-target" className={labelClass}>
              Compliance target
            </label>
            <select
              id="a11y-target"
              className={fieldClass}
              value={settings.compliance_target}
              onChange={(event) => patch({ compliance_target: event.target.value as AccessibilityTarget })}
            >
              {TARGETS.map((target) => (
                <option key={target.id} value={target.id}>
                  {target.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <span className={labelClass}>Additional requirements</span>
            <ul className="grid gap-2 sm:grid-cols-3">
              {EXTRA_REQUIREMENTS.map((req) => (
                <li key={req.id}>
                  <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-line px-3 py-2 text-[12.5px] text-ink-700 hover:border-brand-200">
                    <input
                      type="checkbox"
                      checked={settings.extra_requirements.includes(req.id)}
                      onChange={() => toggleExtra(req.id)}
                      className="mt-0.5"
                    />
                    <span>
                      <span className="block font-medium text-ink-800">{req.label}</span>
                      <span className="block text-[11px] text-ink-400">{req.help}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <label htmlFor="a11y-color" className={labelClass}>
              Brand colours to contrast-check{" "}
              <span className="font-normal text-ink-400">— checked against white, 4.5:1 required for text</span>
            </label>
            {settings.brand_colors.length > 0 ? (
              <ul className="mb-2 flex flex-wrap gap-1.5">
                {settings.brand_colors.map((color) => (
                  <li
                    key={color}
                    className="flex items-center gap-1.5 rounded-full border border-line bg-white px-2.5 py-0.5 text-[12px] text-ink-700"
                  >
                    <span
                      className="h-3 w-3 rounded-full border border-line"
                      style={{ backgroundColor: color }}
                      aria-hidden
                    />
                    {color}
                    <button
                      type="button"
                      aria-label={`Remove ${color}`}
                      onClick={() => patch({ brand_colors: settings.brand_colors.filter((c) => c !== color) })}
                      className="text-ink-400 hover:text-red-600"
                    >
                      <X size={12} />
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            <input
              id="a11y-color"
              className={fieldClass}
              value={colorDraft}
              onChange={(event) => setColorDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addColor();
                }
              }}
              onBlur={addColor}
              placeholder="#0E6E66 — press Enter to add"
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}
