"use client";

import { Code2, Users } from "lucide-react";
import type { TemplateKind } from "@/lib/types/course";
import { cn } from "@/lib/utils/cn";

interface TemplateOption {
  kind: TemplateKind;
  name: string;
  description: string;
  Icon: typeof Code2;
}

/** The backend exposes exactly these two templates - nothing else. */
const TEMPLATES: TemplateOption[] = [
  {
    kind: "technical",
    name: "Technical Template",
    description: "Best for programming, AI/ML, engineering and technical courses",
    Icon: Code2,
  },
  {
    kind: "non_technical",
    name: "Non-Technical Template",
    description: "Best for business, marketing, leadership and non-technical courses",
    Icon: Users,
  },
];

interface TemplateSelectorProps {
  value: TemplateKind;
  onChange: (value: TemplateKind) => void;
}

export function TemplateSelector({ value, onChange }: TemplateSelectorProps) {
  return (
    <div
      role="radiogroup"
      aria-label="Choose Template"
      className="grid gap-4 sm:grid-cols-2"
    >
      {TEMPLATES.map(({ kind, name, description, Icon }) => {
        const selected = value === kind;
        return (
          <button
            key={kind}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(kind)}
            className={cn(
              "flex items-start gap-3 rounded-card border p-4 text-left transition-all",
              "focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand-100",
              selected
                ? "border-brand-500 bg-brand-50 ring-1 ring-brand-500"
                : "border-line bg-white hover:border-brand-200 hover:bg-brand-50/40",
            )}
          >
            <span
              className={cn(
                "flex h-9 w-9 shrink-0 items-center justify-center rounded-[9px] border",
                selected
                  ? "border-brand-200 bg-white text-brand-600"
                  : "border-line bg-canvas text-ink-400",
              )}
            >
              <Icon size={16} />
            </span>
            <span className="min-w-0">
              <span className="block text-[13px] font-semibold text-ink">{name}</span>
              <span className="mt-1 block text-[11.5px] leading-[1.45] text-ink-500">
                {description}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
