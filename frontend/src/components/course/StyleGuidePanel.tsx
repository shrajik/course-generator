"use client";

/**
 * Picks the writing style guide for a course: Microsoft Writing Style Guide
 * (MSTP, the default for instructional content) or Chicago Manual of Style
 * (CMOS, for long-form, cited, academic work), plus client overrides - brand
 * terms to write exactly as given and words that must never appear.
 *
 * Optional by design: `styleGuide` stays null until the panel is opened and
 * something is changed from the defaults, so a course made without touching it
 * generates exactly as before. Overrides take precedence over the base guide.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import type { StyleGuide } from "@/lib/types/course";

const BLANK: StyleGuide = {
  base: "mstp",
  citation_system: "author_date",
  brand_terms: [],
  banned_words: [],
  flag_voice_conflicts: true,
};

const fieldClass =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink-800 outline-none " +
  "transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";
const labelClass = "mb-1.5 block text-[12.5px] font-medium text-ink-700";

interface Props {
  value: StyleGuide | null;
  onChange: (value: StyleGuide | null) => void;
}

/** Back to "not chosen" when nothing differs from the defaults. */
function isDefault(style: StyleGuide): boolean {
  return (
    style.base === "mstp" &&
    style.brand_terms.length === 0 &&
    style.banned_words.length === 0 &&
    style.flag_voice_conflicts === true
  );
}

export function StyleGuidePanel({ value, onChange }: Props) {
  const [open, setOpen] = useState(value !== null);
  const style = value ?? BLANK;

  const patch = (changes: Partial<StyleGuide>) => {
    const next = { ...style, ...changes };
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
          <span className="text-[12.5px] font-medium text-ink-700">Writing Style</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {value
              ? `${value.base === "cmos" ? "Chicago (CMOS)" : "Microsoft (MSTP)"}` +
                (value.brand_terms.length + value.banned_words.length > 0 ? " + client overrides" : "")
              : "Optional — Microsoft or Chicago style, brand terms and banned words"}
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
              <label htmlFor="style-base" className={labelClass}>
                Base style guide
              </label>
              <select
                id="style-base"
                className={fieldClass}
                value={style.base}
                onChange={(event) => patch({ base: event.target.value as StyleGuide["base"] })}
              >
                <option value="mstp">Microsoft Writing Style Guide — courses, job aids, help text</option>
                <option value="cmos">Chicago Manual of Style — long-form, cited, academic</option>
              </select>
            </div>
            {style.base === "cmos" ? (
              <div>
                <label htmlFor="style-citation" className={labelClass}>
                  Citation system
                </label>
                <select
                  id="style-citation"
                  className={fieldClass}
                  value={style.citation_system}
                  onChange={(event) =>
                    patch({ citation_system: event.target.value as StyleGuide["citation_system"] })
                  }
                >
                  <option value="author_date">Author-date (default for business and research)</option>
                  <option value="notes_bibliography">Notes-bibliography</option>
                </select>
              </div>
            ) : null}
          </div>

          <TermList
            label="Brand terms and product names"
            hint="Written exactly as shown, including capitalisation."
            placeholder="for example: AcmeCloud"
            terms={style.brand_terms}
            onChange={(brand_terms) => patch({ brand_terms })}
          />
          <TermList
            label="Banned words and phrases"
            hint="Never used in the course."
            placeholder="for example: synergy"
            terms={style.banned_words}
            onChange={(banned_words) => patch({ banned_words })}
          />
          <div>
            <p className="text-[11.5px] text-ink-400">
              Client overrides take precedence over the base guide.
            </p>
            <label className="mt-2 flex items-center gap-2 text-[12.5px] text-ink-700">
              <input
                type="checkbox"
                checked={style.flag_voice_conflicts}
                onChange={(event) => patch({ flag_voice_conflicts: event.target.checked })}
              />
              Flag brand-voice conflicts — where client voice and the base guide disagree, ask the
              writer to say so rather than resolve it silently
            </label>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function TermList({
  label,
  hint,
  placeholder,
  terms,
  onChange,
}: {
  label: string;
  hint: string;
  placeholder: string;
  terms: string[];
  onChange: (terms: string[]) => void;
}) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const term = draft.trim().replace(/\s+/g, " ");
    setDraft("");
    if (!term || term.length > 80) return;
    if (terms.some((existing) => existing.toLowerCase() === term.toLowerCase())) return;
    onChange([...terms, term]);
  };

  return (
    <div>
      <label className={labelClass}>
        {label} <span className="font-normal text-ink-400">— {hint}</span>
      </label>
      {terms.length > 0 ? (
        <ul className="mb-2 flex flex-wrap gap-1.5">
          {terms.map((term) => (
            <li
              key={term}
              className="flex items-center gap-1 rounded-full border border-line bg-white px-2.5 py-0.5 text-[12px] text-ink-700"
            >
              {term}
              <button
                type="button"
                aria-label={`Remove ${term}`}
                onClick={() => onChange(terms.filter((item) => item !== term))}
                className="text-ink-400 hover:text-red-600"
              >
                <X size={12} />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <input
        className={fieldClass}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            add();
          }
        }}
        onBlur={add}
        placeholder={`${placeholder} — press Enter to add`}
      />
    </div>
  );
}
