"use client";

import { forwardRef } from "react";
import { Loader2, Send, Sparkles } from "lucide-react";
import { blockSummary } from "@/lib/editor/blocks";
import { useEditor } from "@/lib/editor/store";
import { cn } from "@/lib/utils/cn";

const SUGGESTIONS = [
  "Make it simpler",
  "Add a real-world example",
  "Expand explanation",
  "Improve this section",
  "Make it more professional",
  "Add an exercise",
  "Make this quiz harder",
];

interface AiAssistantProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (instruction: string) => void;
  busy: boolean;
  message: string | null;
  error: string | null;
}

export const AiAssistant = forwardRef<HTMLInputElement, AiAssistantProps>(
  function AiAssistant({ value, onChange, onSubmit, busy, message, error }, ref) {
    const editor = useEditor();
    const selected = editor.selectedBlocks;
    const hasSelection = selected.length > 0;

    const submit = (instruction: string) => {
      const text = instruction.trim();
      if (!text || !hasSelection || busy) return;
      onSubmit(text);
    };

    return (
      <div className="border-t border-line bg-white px-4 py-3">
        <div className="flex items-center gap-2">
          <Sparkles size={13} className="text-brand-600" aria-hidden />
          <p className="text-[12px] font-semibold text-ink">AI Assistant</p>
          {hasSelection ? (
            <span className="truncate rounded-full border border-brand-200 bg-brand-50 px-2 py-0.5 text-[10.5px] text-brand-700">
              {selected.length === 1
                ? blockSummary(selected[0])
                : `${selected.length} blocks selected`}
            </span>
          ) : (
            <span className="text-[10.5px] text-ink-400">Select a block to edit it</span>
          )}
        </div>

        <p className="mt-2 text-[11.5px] text-ink-500">What would you like to change?</p>

        <div className="mt-2 flex flex-wrap gap-1.5">
          {SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              disabled={!hasSelection || busy}
              onClick={() => submit(suggestion)}
              className={cn(
                "rounded-full border px-2.5 py-1 text-[11px] transition-colors",
                hasSelection && !busy
                  ? "border-line bg-white text-ink-700 hover:border-brand-300 hover:bg-brand-50 hover:text-brand-700"
                  : "cursor-not-allowed border-line bg-canvas text-ink-300",
              )}
            >
              {suggestion}
            </button>
          ))}
        </div>

        <form
          className="mt-2.5 flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            submit(value);
          }}
        >
          <input
            ref={ref}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            disabled={!hasSelection || busy}
            placeholder={
              hasSelection
                ? "Describe the change…"
                : "Select a block on the page to start"
            }
            className="field h-9 flex-1 text-[12.5px] disabled:bg-canvas disabled:text-ink-300"
          />
          <button
            type="submit"
            aria-label="Send instruction"
            disabled={!hasSelection || busy || !value.trim()}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[9px] bg-brand-600 text-white transition-colors hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
          </button>
        </form>

        {error ? (
          <p className="mt-2 rounded-[8px] border border-red-200 bg-red-50 px-2.5 py-1.5 text-[11.5px] text-red-800">
            {error}
          </p>
        ) : message ? (
          <p className="mt-2 rounded-[8px] border border-brand-100 bg-brand-50 px-2.5 py-1.5 text-[11.5px] text-ink-700">
            {message}
          </p>
        ) : null}
      </div>
    );
  },
);
