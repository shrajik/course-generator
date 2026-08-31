"use client";

import { useState } from "react";
import { Check, Plus, X } from "lucide-react";
import { IconButton } from "@/components/ui/IconButton";
import { cn } from "@/lib/utils/cn";

interface RuleListProps {
  label: string;
  tone: "do" | "dont";
  items: string[];
  onChange: (items: string[]) => void;
  placeholder: string;
}

/**
 * The Do's / Don'ts cards. Rows are editable in place and the user can add or
 * remove as many as they like, which is what the backend expects (`dos`/`donts`).
 */
export function RuleList({ label, tone, items, onChange, placeholder }: RuleListProps) {
  const [draft, setDraft] = useState("");
  const isDo = tone === "do";

  const add = () => {
    const value = draft.trim();
    if (!value) return;
    onChange([...items, value]);
    setDraft("");
  };

  return (
    <div className="rounded-card border border-line bg-white p-4">
      <p className="text-[13px] font-semibold text-ink">{label}</p>

      <ul className="mt-3 space-y-2">
        {items.map((item, index) => (
          <li key={`${index}-${item}`} className="group flex items-start gap-2">
            <span
              className={cn(
                "mt-[3px] flex h-4 w-4 shrink-0 items-center justify-center rounded-full",
                isDo ? "text-success" : "text-danger",
              )}
              aria-hidden
            >
              {isDo ? <Check size={13} strokeWidth={3} /> : <X size={13} strokeWidth={3} />}
            </span>
            <input
              value={item}
              onChange={(event) => {
                const next = [...items];
                next[index] = event.target.value;
                onChange(next);
              }}
              aria-label={`${label} item ${index + 1}`}
              className="min-w-0 flex-1 border-b border-transparent bg-transparent text-[12.5px] leading-5 text-ink-700 outline-none transition-colors focus:border-brand-300"
            />
            <IconButton
              tone="danger"
              aria-label={`Remove ${item}`}
              onClick={() => onChange(items.filter((_, i) => i !== index))}
              className="h-5 w-5 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
            >
              <X size={12} />
            </IconButton>
          </li>
        ))}
      </ul>

      <div className="mt-3 flex items-center gap-2 border-t border-dashed border-line pt-3">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
          placeholder={placeholder}
          aria-label={`Add ${label} item`}
          className="min-w-0 flex-1 bg-transparent text-[12.5px] text-ink-700 placeholder:text-ink-300 outline-none"
        />
        <IconButton onClick={add} aria-label={`Add to ${label}`} disabled={!draft.trim()}>
          <Plus size={14} />
        </IconButton>
      </div>
    </div>
  );
}
