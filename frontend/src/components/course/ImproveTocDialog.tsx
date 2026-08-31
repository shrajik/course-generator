"use client";

import { ArrowRight, Minus, Plus, RefreshCw, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import type { ImproveTocResponse, TocItem } from "@/lib/types/course";
import { cn } from "@/lib/utils/cn";

interface ImproveTocDialogProps {
  open: boolean;
  loading: boolean;
  error: string | null;
  suggestion: ImproveTocResponse | null;
  currentToc: TocItem[];
  onApply: () => void;
  onCancel: () => void;
}

const ACTION_STYLES: Record<string, { className: string; Icon: typeof Plus }> = {
  add: { className: "bg-emerald-50 text-emerald-700 border-emerald-200", Icon: Plus },
  remove: { className: "bg-red-50 text-red-700 border-red-200", Icon: Minus },
  rename: { className: "bg-amber-50 text-amber-800 border-amber-200", Icon: RefreshCw },
  reorder: { className: "bg-brand-50 text-brand-700 border-brand-200", Icon: ArrowRight },
  split: { className: "bg-brand-50 text-brand-700 border-brand-200", Icon: ArrowRight },
  merge: { className: "bg-brand-50 text-brand-700 border-brand-200", Icon: ArrowRight },
};

/**
 * Shows what the AI proposes. The user's TOC is never overwritten automatically -
 * only "Apply Changes" replaces it.
 */
export function ImproveTocDialog({
  open,
  loading,
  error,
  suggestion,
  currentToc,
  onApply,
  onCancel,
}: ImproveTocDialogProps) {
  const currentTitles = new Set(currentToc.map((item) => item.title.trim().toLowerCase()));

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title="AI suggestions for your table of contents"
      subtitle="Nothing changes until you apply these suggestions."
      width="max-w-3xl"
      footer={
        <>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            onClick={onApply}
            disabled={loading || !suggestion || suggestion.suggested_toc.length === 0}
          >
            <Sparkles size={14} />
            Apply Changes
          </Button>
        </>
      }
    >
      {loading ? (
        <div className="flex items-center gap-3 py-10 text-[13px] text-ink-500">
          <RefreshCw size={16} className="animate-spin text-brand-600" />
          Reviewing your outline for gaps, duplicates and ordering problems…
        </div>
      ) : error ? (
        <p className="rounded-[10px] border border-red-200 bg-red-50 px-3 py-2.5 text-[12.5px] text-red-800">
          {error}
        </p>
      ) : suggestion ? (
        <div className="space-y-5">
          {suggestion.reasoning ? (
            <p className="rounded-[10px] border border-brand-100 bg-brand-50 px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-700">
              {suggestion.reasoning}
            </p>
          ) : null}

          {suggestion.changes.length > 0 ? (
            <section>
              <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-ink-500">
                Proposed changes
              </h3>
              <ul className="space-y-2">
                {suggestion.changes.map((change, index) => {
                  const style = ACTION_STYLES[change.action] ?? ACTION_STYLES.reorder;
                  const Icon = style.Icon;
                  return (
                    <li
                      key={index}
                      className="flex items-start gap-2.5 rounded-[10px] border border-line bg-white px-3 py-2.5"
                    >
                      <span
                        className={cn(
                          "mt-[1px] flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold uppercase",
                          style.className,
                        )}
                      >
                        <Icon size={10} />
                        {change.action}
                      </span>
                      <div className="min-w-0 text-[12.5px]">
                        <p className="font-medium text-ink">
                          {change.proposed || change.target || "—"}
                          {change.target && change.proposed && change.target !== change.proposed
                            ? ` (was “${change.target}”)`
                            : ""}
                        </p>
                        {change.reason ? (
                          <p className="mt-0.5 text-ink-500">{change.reason}</p>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </section>
          ) : null}

          <section>
            <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-ink-500">
              Suggested outline ({suggestion.suggested_toc.length} chapters)
            </h3>
            <ol className="divide-y divide-line overflow-hidden rounded-[10px] border border-line">
              {suggestion.suggested_toc.map((item, index) => {
                const isNew = !currentTitles.has(item.title.trim().toLowerCase());
                return (
                  <li
                    key={`${index}-${item.title}`}
                    className={cn(
                      "flex items-start gap-2.5 px-3 py-2.5 text-[12.5px]",
                      isNew ? "bg-emerald-50/60" : "bg-white",
                    )}
                  >
                    <span className="w-5 shrink-0 font-semibold tabular-nums text-ink-400">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0">
                      <p className="font-medium text-ink">
                        {item.title}
                        {isNew ? (
                          <span className="ml-2 rounded-full border border-emerald-200 bg-white px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-700">
                            new
                          </span>
                        ) : null}
                      </p>
                      {item.sections.length > 0 ? (
                        <p className="mt-0.5 text-ink-500">{item.sections.join(" · ")}</p>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ol>
          </section>

          {suggestion.missing_concepts.length > 0 ? (
            <section>
              <h3 className="mb-1.5 text-[12px] font-semibold uppercase tracking-wide text-ink-500">
                Missing concepts
              </h3>
              <ul className="list-inside list-disc space-y-1 text-[12.5px] text-ink-700">
                {suggestion.missing_concepts.map((concept, index) => (
                  <li key={index}>{concept}</li>
                ))}
              </ul>
            </section>
          ) : null}

          {suggestion.duplicate_topics.length > 0 ? (
            <section>
              <h3 className="mb-1.5 text-[12px] font-semibold uppercase tracking-wide text-ink-500">
                Duplicate topics
              </h3>
              <ul className="list-inside list-disc space-y-1 text-[12.5px] text-ink-700">
                {suggestion.duplicate_topics.map((topic, index) => (
                  <li key={index}>{topic}</li>
                ))}
              </ul>
            </section>
          ) : null}

          <p className="text-[11.5px] text-ink-400">
            Applying replaces your current outline with the suggested one. Cancel keeps yours
            exactly as it is.
          </p>
        </div>
      ) : null}
    </Modal>
  );
}
