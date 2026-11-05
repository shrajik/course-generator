"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight, Plus, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { StepHeader } from "@/components/ui/StepHeader";
import { ChapterItem, type DragState } from "./ChapterItem";
import { ImproveTocDialog } from "./ImproveTocDialog";
import { TocSummary } from "./TocSummary";
import { improveToc } from "@/lib/api/courses";
import { ApiError } from "@/lib/api/client";
import { useCourseDraft } from "@/lib/state/course-draft";
import type { ImproveTocResponse, TocItem } from "@/lib/types/course";
import { estimateFromToc } from "@/lib/utils/estimate";

function move<T>(items: T[], from: number, to: number): T[] {
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

export function TocEditor() {
  const router = useRouter();
  const { draft, hydrated, update } = useCourseDraft();

  const [expanded, setExpanded] = useState<Set<number>>(() => new Set([1]));
  const [drag, setDrag] = useState<DragState | null>(null);
  const [dropTarget, setDropTarget] = useState<DragState | null>(null);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [suggestion, setSuggestion] = useState<ImproveTocResponse | null>(null);
  const [improving, setImproving] = useState(false);
  const [improveError, setImproveError] = useState<string | null>(null);

  const toc = draft.toc;
  const estimate = useMemo(() => estimateFromToc(toc), [toc]);

  const setToc = (next: TocItem[]) => update({ toc: next });

  // --- chapter / section mutations ----------------------------------------
  const addChapter = () => {
    setToc([...toc, { title: `New Chapter ${toc.length + 1}`, sections: [], notes: "" }]);
    setExpanded((current) => new Set(current).add(toc.length));
  };

  const renameChapter = (index: number, title: string) =>
    setToc(toc.map((item, i) => (i === index ? { ...item, title } : item)));

  const deleteChapter = (index: number) => setToc(toc.filter((_, i) => i !== index));

  const addSection = (index: number) =>
    setToc(
      toc.map((item, i) =>
        i === index
          ? { ...item, sections: [...item.sections, `New Section ${item.sections.length + 1}`] }
          : item,
      ),
    );

  const renameSection = (index: number, sectionIndex: number, title: string) =>
    setToc(
      toc.map((item, i) =>
        i === index
          ? { ...item, sections: item.sections.map((s, j) => (j === sectionIndex ? title : s)) }
          : item,
      ),
    );

  const deleteSection = (index: number, sectionIndex: number) =>
    setToc(
      toc.map((item, i) =>
        i === index
          ? { ...item, sections: item.sections.filter((_, j) => j !== sectionIndex) }
          : item,
      ),
    );

  const toggle = (index: number) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  // --- drag and drop -------------------------------------------------------
  const handleDrop = () => {
    if (!drag || !dropTarget) {
      setDrag(null);
      setDropTarget(null);
      return;
    }
    if (drag.kind === "chapter" && dropTarget.kind === "chapter") {
      if (drag.index !== dropTarget.index) setToc(move(toc, drag.index, dropTarget.index));
    } else if (
      drag.kind === "section" &&
      dropTarget.kind === "section" &&
      drag.chapterIndex === dropTarget.chapterIndex &&
      drag.index !== dropTarget.index
    ) {
      const chapter = toc[drag.chapterIndex];
      setToc(
        toc.map((item, i) =>
          i === drag.chapterIndex
            ? { ...item, sections: move(chapter.sections, drag.index, dropTarget.index) }
            : item,
        ),
      );
    }
    setDrag(null);
    setDropTarget(null);
  };

  // --- Improve with AI -----------------------------------------------------
  const handleImprove = async () => {
    setDialogOpen(true);
    setImproving(true);
    setImproveError(null);
    setSuggestion(null);
    try {
      const response = await improveToc({
        course_title: draft.courseTitle,
        toc,
        audience: draft.targetAudience,
        template: draft.template,
        dos: draft.dos,
        donts: draft.donts,
      });
      setSuggestion(response);
    } catch (caught) {
      setImproveError(
        caught instanceof ApiError ? caught.message : "The AI review could not be completed.",
      );
    } finally {
      setImproving(false);
    }
  };

  const applySuggestion = () => {
    if (!suggestion) return;
    setToc(
      suggestion.suggested_toc.map((item) => ({
        title: item.title,
        sections: item.sections ?? [],
        notes: item.notes ?? "",
      })),
    );
    setDialogOpen(false);
    setSuggestion(null);
  };

  // --- Continue to Review & Generate ---------------------------------------
  /**
   * The course itself isn't created yet - that happens on the Review &
   * Generate screen, once the person has had a chance to see a recap of
   * everything they've set up. `draft` is already persisted to
   * sessionStorage, so the next screen picks it up as-is.
   */
  const handleContinue = () => {
    if (toc.length === 0) return;
    router.push("/review-generate");
  };

  if (!hydrated) return null;

  return (
    <section className="panel p-6">
      <StepHeader
        step={2}
        title="Customize Table of Contents"
        subtitle="Organize your course structure"
      >
        <Button variant="outline" size="sm" onClick={handleImprove}>
          <Sparkles size={13} className="text-brand-600" />
          Improve with AI
        </Button>
        <Button size="sm" onClick={addChapter}>
          <Plus size={13} />
          Add Chapter
        </Button>
      </StepHeader>

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_212px]">
        <div>
          {toc.length === 0 ? (
            <div className="rounded-card border border-dashed border-line bg-canvas px-6 py-14 text-center">
              <p className="text-[13px] font-medium text-ink">No chapters yet</p>
              <p className="mx-auto mt-1 max-w-sm text-[12.5px] text-ink-500">
                Add chapters manually, or let the AI propose an outline for “
                {draft.courseTitle || "your course"}”.
              </p>
              <div className="mt-4 flex justify-center gap-2">
                <Button size="sm" variant="outline" onClick={handleImprove}>
                  <Sparkles size={13} className="text-brand-600" />
                  Improve with AI
                </Button>
                <Button size="sm" onClick={addChapter}>
                  <Plus size={13} />
                  Add Chapter
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-2.5" onDragEnd={() => setDrag(null)}>
              {toc.map((chapter, index) => (
                <ChapterItem
                  key={index}
                  chapter={chapter}
                  index={index}
                  expanded={expanded.has(index)}
                  drag={drag}
                  dropTarget={dropTarget}
                  onToggle={() => toggle(index)}
                  onRename={(title) => renameChapter(index, title)}
                  onDelete={() => deleteChapter(index)}
                  onAddSection={() => addSection(index)}
                  onRenameSection={(sectionIndex, title) =>
                    renameSection(index, sectionIndex, title)
                  }
                  onDeleteSection={(sectionIndex) => deleteSection(index, sectionIndex)}
                  onDragStart={setDrag}
                  onDragOver={setDropTarget}
                  onDrop={handleDrop}
                  onDragEnd={() => {
                    setDrag(null);
                    setDropTarget(null);
                  }}
                />
              ))}
            </div>
          )}

          <div className="mt-6 flex items-center justify-between">
            <Button variant="outline" onClick={() => router.push("/")}>
              <ArrowLeft size={14} />
              Back
            </Button>
            <Button onClick={handleContinue} disabled={toc.length === 0}>
              <ArrowRight size={14} />
              Next: Review
            </Button>
          </div>
        </div>

        <TocSummary estimate={estimate} />
      </div>

      <ImproveTocDialog
        open={dialogOpen}
        loading={improving}
        error={improveError}
        suggestion={suggestion}
        currentToc={toc}
        onApply={applySuggestion}
        onCancel={() => {
          setDialogOpen(false);
          setSuggestion(null);
        }}
      />
    </section>
  );
}
