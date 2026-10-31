"use client";

/**
 * Picks metadata settings for a course: a default schema (LOM or Dublin
 * Core), an organisation's own tagging convention, and an explicit
 * owner/requester distinct from whoever is signed in.
 *
 * Optional by design - `metadata` stays null until the designer opens the
 * panel and changes something, so a course made without touching it keeps
 * app.course.compliance's existing "lom" default and no org-tagging/owner
 * override behaviour exactly. A `?schema=` request to the compliance
 * endpoint still overrides this per-call, unchanged.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { MetadataSchema, MetadataSettings } from "@/lib/types/course";

const BLANK: MetadataSettings = {
  schema_preference: null,
  tagging_conventions: "",
  owner_override: "",
};

const fieldClass =
  "w-full rounded-lg border border-line bg-white px-3 py-2 text-[13px] text-ink-800 outline-none " +
  "transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";
const labelClass = "mb-1.5 block text-[12.5px] font-medium text-ink-700";

interface Props {
  value: MetadataSettings | null;
  onChange: (value: MetadataSettings | null) => void;
}

/** Back to "not chosen" when nothing differs from the defaults. */
function isDefault(settings: MetadataSettings): boolean {
  return (
    settings.schema_preference === null &&
    settings.tagging_conventions.trim() === "" &&
    settings.owner_override.trim() === ""
  );
}

export function MetadataPanel({ value, onChange }: Props) {
  const [open, setOpen] = useState(value !== null);
  const settings = value ?? BLANK;

  const patch = (changes: Partial<MetadataSettings>) => {
    const next = { ...settings, ...changes };
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
          <span className="text-[12.5px] font-medium text-ink-700">Metadata</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {value
              ? settings.schema_preference === "dublin_core"
                ? "Dublin Core"
                : "LOM"
              : "Optional — schema default, org tagging, owner"}
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
            <label htmlFor="meta-schema" className={labelClass}>
              Schema
            </label>
            <select
              id="meta-schema"
              className={fieldClass}
              value={settings.schema_preference ?? "lom"}
              onChange={(event) => patch({ schema_preference: event.target.value as MetadataSchema })}
            >
              <option value="lom">LOM — default for learning content</option>
              <option value="dublin_core">Dublin Core</option>
            </select>
          </div>

          <div>
            <label htmlFor="meta-tagging" className={labelClass}>
              Organisation tagging conventions (optional)
            </label>
            <input
              id="meta-tagging"
              className={fieldClass}
              value={settings.tagging_conventions}
              onChange={(event) => patch({ tagging_conventions: event.target.value })}
              placeholder="Internal taxonomy, product-line codes"
            />
          </div>

          <div>
            <label htmlFor="meta-owner" className={labelClass}>
              Owner / requester (optional)
            </label>
            <input
              id="meta-owner"
              className={fieldClass}
              value={settings.owner_override}
              onChange={(event) => patch({ owner_override: event.target.value })}
              placeholder="Defaults to whoever is signed in when the report is generated"
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}
