"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, CheckCircle2, FileText, Layers3, Loader2, Save } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { StepHeader } from "@/components/ui/StepHeader";
import { CoursePreviewArt } from "./CoursePreviewArt";
import { RuleList } from "./RuleList";
import { improveToc } from "@/lib/api/courses";
import { classifyTemplateType } from "@/lib/api/course-templates";
import { ApiError } from "@/lib/api/client";
import { useCourseDraft } from "@/lib/state/course-draft";
import type { TocItem } from "@/lib/types/course";

export function CreateCourseForm() {
  const router = useRouter();
  const { draft, hydrated, update } = useCourseDraft();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), 2200);
    return () => window.clearTimeout(timer);
  }, [saved]);

  const canContinue =
    hydrated && draft.courseTitle.trim().length > 0 && draft.targetAudience.trim().length > 0;

  /**
   * Continue asks the backend to draft a starting table of contents from the
   * title, audience and template, then hands it to the TOC screen for the user
   * to customise. The course itself is created when they press Generate, so the
   * edited TOC is what the backend stores.
   *
   * The draft (including `toc`) is mirrored to sessionStorage so a refresh
   * doesn't lose it - which means it also survives navigating back here to
   * start an entirely different course. Re-drafting only when `toc` happens
   * to be empty isn't enough: it must re-draft whenever the title/audience/
   * template have actually changed since the outline currently in state was
   * generated, so a new course never inherits a previous one's chapters.
   */
  const handleContinue = async () => {
    if (!canContinue || busy) return;
    setBusy(true);
    setError(null);
    const courseTitle = draft.courseTitle.trim();
    const targetAudience = draft.targetAudience.trim();

    // The Default Template has no type of its own - resolve it now that a
    // title (required by canContinue) is guaranteed to exist, rather than
    // leaving "Course Template" stuck on "Detecting type…" forever if the
    // user picked it before typing a title.
    let template = draft.template;
    if (draft.selectedTemplate?.templateType === "auto") {
      try {
        const classification = await classifyTemplateType({ courseTitle, targetAudience });
        template = classification.template_type;
        update({
          template,
          selectedTemplate: { ...draft.selectedTemplate, templateType: classification.template_type },
        });
      } catch {
        // Fall back to the draft's existing template rather than blocking
        // Continue on a classification hiccup.
      }
    }

    const staleOrMissing =
      draft.toc.length === 0 ||
      !draft.tocDraftedFor ||
      draft.tocDraftedFor.courseTitle !== courseTitle ||
      draft.tocDraftedFor.targetAudience !== targetAudience ||
      draft.tocDraftedFor.template !== template;
    try {
      let toc: TocItem[] = draft.toc;
      if (staleOrMissing) {
        const suggestion = await improveToc({
          course_title: courseTitle,
          toc: [],
          audience: targetAudience,
          template,
          dos: draft.dos,
          donts: draft.donts,
        });
        toc = suggestion.suggested_toc.map((item) => ({
          title: item.title,
          sections: item.sections ?? [],
          notes: item.notes ?? "",
        }));
        update({ toc, tocDraftedFor: { courseTitle, targetAudience, template } });
      }
      router.push("/toc");
    } catch (caught) {
      // A failed draft must not block the user - they can build the TOC by hand.
      const message =
        caught instanceof ApiError ? caught.message : "Could not draft a table of contents.";
      setError(`${message} You can add chapters manually on the next step.`);
      if (staleOrMissing) {
        // The stale outline must not be reused for a different course - clear
        // it rather than carrying it forward under the new title.
        update({ toc: [], tocDraftedFor: null });
      }
      router.push("/toc");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel p-6">
      <StepHeader
        step={1}
        title="Create Course"
        subtitle="Provide basic information about your course"
      >
        <Button variant="outline" size="sm" onClick={() => setSaved(true)}>
          <Save size={13} />
          {saved ? "Draft saved" : "Save as Draft"}
        </Button>
      </StepHeader>

      <div className="mt-6 grid gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
        <div className="hidden lg:block">
          <CoursePreviewArt />
        </div>

        <div className="space-y-5">
          <div>
            <label
              htmlFor="course-title"
              className="mb-1.5 block text-[12.5px] font-medium text-ink-700"
            >
              Course Title
            </label>
            <input
              id="course-title"
              className="field"
              value={draft.courseTitle}
              onChange={(event) => update({ courseTitle: event.target.value })}
              placeholder="Mastering Python Programming"
            />
          </div>

          <div>
            <label
              htmlFor="target-audience"
              className="mb-1.5 block text-[12.5px] font-medium text-ink-700"
            >
              Target Audience
            </label>
            <input
              id="target-audience"
              className="field"
              value={draft.targetAudience}
              onChange={(event) => update({ targetAudience: event.target.value })}
              placeholder="Beginners to intermediate developers who want to master Python."
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <RuleList
              label="Do's"
              tone="do"
              items={draft.dos}
              onChange={(dos) => update({ dos })}
              placeholder="Add a do…"
            />
            <RuleList
              label="Don'ts"
              tone="dont"
              items={draft.donts}
              onChange={(donts) => update({ donts })}
              placeholder="Add a don't…"
            />
          </div>

          <div>
            <p className="mb-2.5 text-[12.5px] font-medium text-ink-700">Course Template</p>
            {draft.selectedTemplate ? (
              <div className="flex items-center justify-between gap-3 rounded-card border border-brand-200 bg-brand-50 px-4 py-3.5">
                <div className="flex items-start gap-3">
                  <FileText size={16} className="mt-0.5 shrink-0 text-brand-600" aria-hidden />
                  <div>
                    <p className="text-[11px] font-medium uppercase tracking-wide text-ink-400">
                      Selected Template
                    </p>
                    <p className="mt-0.5 text-[13.5px] font-semibold text-ink">
                      {draft.selectedTemplate.name}
                    </p>
                    <p className="text-[12px] text-ink-500">
                      {draft.selectedTemplate.templateType === "auto"
                        ? "Detecting type…"
                        : draft.selectedTemplate.templateType === "technical"
                          ? "Technical"
                          : "Non-Technical"}
                    </p>
                    <p className="mt-1 flex items-center gap-1 text-[11.5px] text-brand-700">
                      <CheckCircle2 size={12} />
                      {draft.selectedTemplate.templateType === "auto"
                        ? "Type will be detected automatically"
                        : "Template selected"}
                    </p>
                  </div>
                </div>
                <Button variant="outline" size="sm" onClick={() => router.push("/templates?select=1")}>
                  Change Template
                </Button>
              </div>
            ) : (
              <div className="flex items-center justify-between gap-3 rounded-card border border-line bg-cream-50 px-4 py-3.5">
                <div className="flex items-center gap-2.5 text-[12.5px] text-ink-500">
                  <Layers3 size={16} className="shrink-0" aria-hidden />
                  No template selected
                </div>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    className="text-[12px] font-medium text-ink-400 underline-offset-2 hover:text-ink-600 hover:underline"
                    onClick={() => update({ selectedTemplate: null })}
                  >
                    Start from Scratch
                  </button>
                  <Button variant="outline" size="sm" onClick={() => router.push("/templates?select=1")}>
                    Select Template
                  </Button>
                </div>
              </div>
            )}
          </div>

          {error ? (
            <p className="rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-900">
              {error}
            </p>
          ) : null}

          <div className="flex justify-end pt-1">
            <Button onClick={handleContinue} disabled={!canContinue || busy}>
              {busy ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  Drafting outline…
                </>
              ) : (
                <>
                  Continue
                  <ArrowRight size={14} />
                </>
              )}
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}
