"use client";

import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import {
  BookOpen,
  Briefcase,
  Dumbbell,
  FileText,
  Flame,
  HelpCircle,
  Languages,
  Lightbulb,
  ListChecks,
  Loader2,
  PenLine,
  Rows3,
  Search,
  Send,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { blockSummary } from "@/lib/editor/blocks";
import { useEditor } from "@/lib/editor/store";
import { cn } from "@/lib/utils/cn";

/**
 * Data-driven so new slash actions only mean adding a row here - nothing
 * else in this file needs to change. `keywords` widen what `/<query>` can
 * match beyond the visible label (e.g. "/example" should also surface
 * "Rewrite this section" if it were tagged "example", though today it
 * isn't - keywords are opt-in per action).
 */
interface SlashCommand {
  id: string;
  label: string;
  icon: LucideIcon;
  keywords?: string[];
}

const SLASH_COMMANDS: SlashCommand[] = [
  { id: "simplify", label: "Make it simpler", icon: Sparkles },
  { id: "example", label: "Add a real-world example", icon: Lightbulb, keywords: ["example", "real-world", "case study"] },
  { id: "expand", label: "Expand explanation", icon: BookOpen },
  { id: "improve", label: "Improve this section", icon: PenLine },
  { id: "professional", label: "Make it more professional", icon: Briefcase, keywords: ["formal", "tone"] },
  { id: "exercise", label: "Add an exercise", icon: Dumbbell, keywords: ["practice", "activity"] },
  { id: "quiz-generate", label: "Generate quiz", icon: HelpCircle, keywords: ["quiz", "question", "test"] },
  { id: "quiz-harder", label: "Make quiz harder", icon: Flame, keywords: ["quiz", "difficult", "harder"] },
  { id: "key-points", label: "Add key points", icon: ListChecks, keywords: ["takeaways", "bullets"] },
  { id: "summary", label: "Create summary", icon: Rows3, keywords: ["summarize", "tldr", "recap"] },
  { id: "rewrite", label: "Rewrite this section", icon: FileText, keywords: ["redo", "reword"] },
  { id: "translate", label: "Translate", icon: Languages, keywords: ["language", "locale"] },
];

/** Attaches one DOM node to several refs (the parent's forwarded ref, used
 * to programmatically focus this input from CourseEditor, plus this
 * component's own ref used for click-outside detection and refocusing
 * after picking a slash action). */
function mergeRefs<T>(...refs: Array<React.Ref<T> | undefined>) {
  return (node: T | null) => {
    for (const candidate of refs) {
      if (!candidate) continue;
      if (typeof candidate === "function") candidate(node);
      else (candidate as React.MutableRefObject<T | null>).current = node;
    }
  };
}

interface AiAssistantProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (instruction: string) => void;
  busy: boolean;
  message: string | null;
  error: string | null;
}

export const AiAssistant = forwardRef<HTMLInputElement, AiAssistantProps>(
  function AiAssistant({ value, onChange, onSubmit, busy, message, error }, forwardedRef) {
    const editor = useEditor();
    const selected = editor.selectedBlocks;
    const hasSelection = selected.length > 0;

    const inputRef = useRef<HTMLInputElement>(null);
    const containerRef = useRef<HTMLDivElement>(null);
    const [menuOpen, setMenuOpen] = useState(false);
    const [highlighted, setHighlighted] = useState(0);

    const query = value.startsWith("/") ? value.slice(1) : null;

    useEffect(() => {
      setMenuOpen(query !== null);
      setHighlighted(0);
    }, [query]);

    const filtered = useMemo(() => {
      if (query === null) return [];
      const term = query.trim().toLowerCase();
      if (!term) return SLASH_COMMANDS;
      return SLASH_COMMANDS.filter(
        (command) =>
          command.label.toLowerCase().includes(term) ||
          command.keywords?.some((keyword) => keyword.toLowerCase().includes(term)),
      );
    }, [query]);

    // Click outside the input+menu closes the menu, same as any dropdown.
    useEffect(() => {
      if (!menuOpen) return;
      const handler = (event: MouseEvent) => {
        if (!containerRef.current?.contains(event.target as Node)) setMenuOpen(false);
      };
      document.addEventListener("mousedown", handler);
      return () => document.removeEventListener("mousedown", handler);
    }, [menuOpen]);

    const applyCommand = (command: SlashCommand) => {
      onChange(`${command.label} `);
      setMenuOpen(false);
      inputRef.current?.focus();
    };

    const submit = (instruction: string) => {
      const text = instruction.trim();
      if (!text || !hasSelection || busy) return;
      onSubmit(text);
    };

    const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
      if (!menuOpen || filtered.length === 0) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setHighlighted((current) => Math.min(current + 1, filtered.length - 1));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setHighlighted((current) => Math.max(current - 1, 0));
      } else if (event.key === "Enter") {
        event.preventDefault();
        applyCommand(filtered[highlighted]);
      } else if (event.key === "Escape") {
        event.preventDefault();
        setMenuOpen(false);
      }
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

        <div ref={containerRef} className="relative mt-2.5">
          {/* Opens upward, anchored to the input - see `bottom-full`. Always
             mounted (not conditionally rendered) so opening/closing animates
             via the transition below instead of popping in/out instantly. */}
          <div
            id="ai-assistant-slash-menu"
            role="listbox"
            aria-hidden={!menuOpen}
            className={cn(
              "subtle-scroll absolute inset-x-0 bottom-full z-30 mb-2 max-h-64 origin-bottom overflow-y-auto rounded-[10px] border border-line bg-white shadow-pop transition-all duration-150 ease-out",
              menuOpen
                ? "translate-y-0 scale-100 opacity-100"
                : "pointer-events-none translate-y-1 scale-95 opacity-0",
            )}
          >
            <div className="flex items-center gap-1.5 border-b border-line px-3 py-2 text-[11px] text-ink-400">
              <Search size={12} className="shrink-0" />
              {query ? <span className="text-ink-600">{query}</span> : "Search actions…"}
            </div>
            {filtered.length === 0 ? (
              <p className="px-3 py-3 text-[11.5px] text-ink-400">No matching actions.</p>
            ) : (
              <ul className="py-1">
                {filtered.map((command, index) => {
                  const Icon = command.icon;
                  const active = index === highlighted;
                  return (
                    <li key={command.id}>
                      <button
                        type="button"
                        onMouseEnter={() => setHighlighted(index)}
                        onClick={() => applyCommand(command)}
                        className={cn(
                          "flex w-full items-center gap-2.5 px-3 py-2 text-left text-[12px] transition-colors",
                          active ? "bg-brand-50 text-brand-700" : "text-ink-700 hover:bg-brand-50",
                        )}
                      >
                        <Icon size={14} className={active ? "text-brand-600" : "text-ink-400"} />
                        {command.label}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <form
            className="flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (menuOpen && filtered.length > 0) {
                applyCommand(filtered[highlighted]);
                return;
              }
              submit(value);
            }}
          >
            <input
              ref={mergeRefs(forwardedRef, inputRef)}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={!hasSelection || busy}
              placeholder={
                hasSelection ? "Type / to see actions…" : "Select a block on the page to start"
              }
              role="combobox"
              aria-expanded={menuOpen}
              aria-controls="ai-assistant-slash-menu"
              autoComplete="off"
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
        </div>

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
