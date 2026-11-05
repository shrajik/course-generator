"use client";

/**
 * Content Studio Step 4 of 5 - Review & Generate. A read-only recap of every
 * setting gathered on the previous screens, shown once before the course is
 * actually created, replacing the old behaviour where generation kicked off
 * silently the instant the generation-progress page mounted.
 *
 * Deliberately scoped to settings-only: no pre-flight QA/accessibility
 * checks (the real checker needs a generated document, which doesn't exist
 * yet at this point) and no working "phase-gated vs all-in-one" choice (the
 * backend's generate() always does planning and writing in one call - see
 * PhaseApprovalButton.tsx for the real per-phase approval, which happens
 * from the editor toolbar once each stage is actually ready).
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { StepHeader } from "@/components/ui/StepHeader";
import { ApiError } from "@/lib/api/client";
import { approveOutline, createCourse } from "@/lib/api/courses";
import { useCourseDraft } from "@/lib/state/course-draft";
import {
  CONTENT_LEVEL_LABELS,
  LEVEL_OPTIONS,
  type AccessibilityTarget,
  type CompanionDeliverable,
  type DesignModelId,
  type ExtraAccessibilityRequirement,
  type MetadataSchema,
  type PackagingStandard,
} from "@/lib/types/course";

const MODEL_LABELS: Record<DesignModelId, string> = {
  addie: "ADDIE",
  bloom: "Bloom's Taxonomy",
  sam: "SAM",
  gagne: "Gagné's Nine Events",
  kirkpatrick: "Kirkpatrick's Four Levels",
};

const PACKAGING_LABELS: Record<PackagingStandard, string> = {
  scorm_1_2: "SCORM 1.2",
  scorm_2004: "SCORM 2004",
  xapi: "xAPI",
  cmi5: "cmi5",
  aicc: "AICC",
  lti: "LTI",
};

const ACCESSIBILITY_TARGET_LABELS: Record<AccessibilityTarget, string> = {
  wcag21_aa: "WCAG 2.1 AA",
  wcag22_aa: "WCAG 2.2 AA",
};

const EXTRA_REQUIREMENT_LABELS: Record<ExtraAccessibilityRequirement, string> = {
  section_508: "Section 508",
  en_301_549: "EN 301 549",
  pdf_ua: "PDF/UA",
};

const METADATA_SCHEMA_LABELS: Record<MetadataSchema, string> = {
  lom: "LOM",
  dublin_core: "Dublin Core",
};

const STYLE_BASE_LABELS: Record<"mstp" | "cmos", string> = {
  mstp: "Microsoft (MSTP)",
  cmos: "Chicago (CMOS)",
};

const COMPANION_LABELS: Record<CompanionDeliverable, string> = {
  job_aid: "Job aid / checklist",
  quick_ref: "Quick-reference card",
  manager_guide: "Manager coaching guide",
};

function SettingsCard({
  title,
  headline,
  detail,
}: {
  title: string;
  headline: string;
  detail: string;
}) {
  return (
    <div className="rounded-card border border-line bg-white px-4 py-3.5">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">{title}</p>
      <p className="mt-1 text-[13px] font-semibold text-ink">{headline}</p>
      <p className="mt-0.5 text-[12px] text-ink-500">{detail}</p>
    </div>
  );
}

export function ReviewGenerateScreen() {
  const router = useRouter();
  const { draft, hydrated, update } = useCourseDraft();
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!hydrated) return null;

  const toc = draft.toc;

  const audienceLabel =
    LEVEL_OPTIONS.find((option) => option.value === draft.targetAudience)?.label ??
    (draft.targetAudience.trim() ? "Custom audience" : "Not set");

  const models = draft.designModels?.models ?? [];

  const handleGenerate = async () => {
    if (generating || toc.length === 0) return;
    setGenerating(true);
    setError(null);
    try {
      const record = await createCourse(
        {
          course_title: draft.courseTitle.trim(),
          toc,
          target_audience: draft.targetAudience.trim(),
          dos: draft.dos.filter((item) => item.trim()),
          donts: draft.donts.filter((item) => item.trim()),
          template: draft.template,
          language: "en",
          tone: "",
          learner_profile: draft.learnerProfile,
          style_guide: draft.styleGuide,
          source_ids: draft.sourceIds ?? [],
          design_models:
            draft.designModels && draft.designModels.models.length > 0 ? draft.designModels : null,
          template_id_override: draft.selectedTemplate?.templateId ?? null,
          content_level: draft.contentLevel,
          learning_approaches: draft.learningApproaches,
          packaging: draft.packaging,
          accessibility: draft.accessibility,
          metadata: draft.metadataSettings,
          tasks: draft.tasks,
          business_goal: draft.businessGoal,
          performance_gap: draft.performanceGap,
          companions: draft.companions,
        },
        { runPlanner: false },
      );
      // Clicking "Generate Course" here is the person signing off the
      // outline (Gate 1); the server refuses to generate until it is recorded.
      await approveOutline(record.course_id);
      update({ courseId: record.course_id, documentId: record.document_id });
      router.push(`/generate/${record.course_id}`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The course could not be created.");
      setGenerating(false);
    }
  };

  const deliverables: string[] = ["Course document"];
  if (models.includes("bloom")) deliverables.push("Bloom's objectives table");
  if (models.includes("kirkpatrick")) deliverables.push("Kirkpatrick evaluation plan");
  for (const companion of draft.companions) deliverables.push(COMPANION_LABELS[companion]);
  deliverables.push("Compliance appendix");

  return (
    <section className="panel p-6">
      <StepHeader
        step={3}
        title="Review and generate"
        subtitle="Check your settings below, then generate the course. Nothing is generated until you click Generate."
      >
        <Button variant="outline" size="sm" onClick={() => router.push("/")}>
          Edit settings
        </Button>
      </StepHeader>

      {toc.length === 0 ? (
        <div className="mt-6 rounded-card border border-dashed border-line bg-canvas px-6 py-14 text-center">
          <p className="text-[13px] font-medium text-ink">No chapters yet</p>
          <p className="mx-auto mt-1 max-w-sm text-[12.5px] text-ink-500">
            Go back to the table of contents and add at least one chapter before generating.
          </p>
          <Button className="mt-4" variant="outline" size="sm" onClick={() => router.push("/toc")}>
            <ArrowLeft size={13} />
            Back to table of contents
          </Button>
        </div>
      ) : (
        <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_260px]">
          <div>
            <p className="mb-3 text-[12.5px] font-medium text-ink-700">Your settings</p>
            <div className="grid gap-3 sm:grid-cols-2">
              <SettingsCard
                title="Brief"
                headline={draft.courseTitle.trim() || "Untitled course"}
                detail={
                  `${draft.tasks.length} task(s)` +
                  (draft.businessGoal.trim() ? ` · goal: ${draft.businessGoal.trim()}` : "")
                }
              />
              <SettingsCard
                title="Audience"
                headline={audienceLabel}
                detail={
                  `${CONTENT_LEVEL_LABELS[draft.contentLevel]}` +
                  (draft.companions.length > 0
                    ? ` · ${draft.companions.map((c) => COMPANION_LABELS[c]).join(" + ")}`
                    : "")
                }
              />
              <SettingsCard
                title="Model"
                headline={models.length > 0 ? models.map((m) => MODEL_LABELS[m]).join(" + ") : "Not set"}
                detail={
                  models.length > 0
                    ? [
                        models.includes("bloom")
                          ? `Bloom: ${draft.designModels?.bloom_target_level ?? "Full spread"}`
                          : null,
                        models.includes("kirkpatrick")
                          ? `Kirkpatrick: ${
                              draft.designModels?.data_sources === "lms_and_kpis"
                                ? "LMS + KPIs"
                                : "LMS only"
                            }`
                          : null,
                        models.includes("sam") ? `SAM ${draft.designModels?.sam_loop ?? ""}` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ") || "No additional settings"
                    : "Default structure will be used"
                }
              />
              <SettingsCard
                title="Packaging"
                headline={
                  draft.packaging?.standard ? PACKAGING_LABELS[draft.packaging.standard] : "Not set"
                }
                detail={
                  draft.packaging
                    ? [
                        draft.packaging.target_lms || "LMS unspecified",
                        draft.packaging.track_beyond_lms ? "off-LMS tracking on" : null,
                        draft.packaging.lrs_confirmed ? "LRS confirmed" : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")
                    : "No packaging/tracking standard selected"
                }
              />
              <SettingsCard
                title="Accessibility"
                headline={
                  draft.accessibility
                    ? ACCESSIBILITY_TARGET_LABELS[draft.accessibility.compliance_target]
                    : "WCAG 2.2 AA (default)"
                }
                detail={
                  draft.accessibility
                    ? [
                        ...draft.accessibility.extra_requirements.map(
                          (req) => EXTRA_REQUIREMENT_LABELS[req],
                        ),
                        draft.accessibility.brand_colors.length > 0
                          ? `${draft.accessibility.brand_colors.length} brand colour(s) checked`
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ") || "No extra requirements"
                    : "Defaults apply"
                }
              />
              <SettingsCard
                title="Metadata & Style"
                headline={
                  draft.metadataSettings?.schema_preference
                    ? METADATA_SCHEMA_LABELS[draft.metadataSettings.schema_preference]
                    : "LOM (default)"
                }
                detail={
                  draft.styleGuide
                    ? STYLE_BASE_LABELS[draft.styleGuide.base]
                    : "Microsoft (MSTP) (default)"
                }
              />
            </div>

            {error ? (
              <p className="mt-4 rounded-[10px] border border-red-200 bg-red-50 px-3 py-2.5 text-[12.5px] text-red-800">
                {error}
              </p>
            ) : null}

            <div className="mt-6 flex items-center justify-between">
              <Button variant="outline" onClick={() => router.push("/toc")}>
                <ArrowLeft size={14} />
                Back
              </Button>
              <Button onClick={() => void handleGenerate()} disabled={generating}>
                {generating ? (
                  <>
                    <Loader2 size={14} className="animate-spin" />
                    Starting…
                  </>
                ) : (
                  <>
                    <Sparkles size={14} />
                    Generate Course
                  </>
                )}
              </Button>
            </div>
          </div>

          <aside className="space-y-3">
            <div className="rounded-card border border-line bg-white px-4 py-4">
              <p className="text-[12.5px] font-semibold text-ink">Deliverable bundle</p>
              <ul className="mt-3 space-y-2">
                {deliverables.map((item) => (
                  <li key={item} className="text-[12.5px] text-ink-700">
                    {item}
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-card border border-brand-100 bg-brand-50 px-3.5 py-3">
              <p className="text-[11.5px] leading-[1.5] text-ink-700">
                Settings save as you go. Nothing is generated until you click{" "}
                <span className="font-semibold text-brand-700">Generate Course</span>.
              </p>
            </div>
          </aside>
        </div>
      )}
    </section>
  );
}
