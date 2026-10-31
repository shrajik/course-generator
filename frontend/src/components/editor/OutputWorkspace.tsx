"use client";

/**
 * Content Studio Phase 8 - Output Workspace. A new, read-only view over an
 * already-generated course: a left module tree (chapters + companion
 * deliverables), the selected chapter/companion's content in the center, and
 * a QA/accessibility/metadata sidebar on the right - matching the mockup's
 * Step 5 layout.
 *
 * Deliberately NOT a rebuild of content generation: the writer still produces
 * ordinary prose chapters (no PRINCIPLE/CONCEPT/EXAMPLE/etc. chunking), and
 * this screen only composes data that already exists elsewhere in the app
 * (getDocument, getComplianceReport, getCompanionDeliverables, the phase
 * approvals from PhaseApprovalButton) - no backend changes, no editing.
 *
 * Follows CoursePreview.tsx's pattern: its own getDocument() fetch into local
 * state, BlockRenderer in read-only mode - no EditorProvider/useEditor().
 */

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  CheckCircle2,
  Download,
  Loader2,
  RefreshCw,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { BlockRenderer } from "@/components/blocks/BlockRenderer";
import { PhaseApprovalButton } from "./PhaseApprovalButton";
import { ApiError } from "@/lib/api/client";
import {
  exportedPdfDownloadUrl,
  generateExportedPdf,
  getCompanionDeliverables,
  getComplianceReport,
  getDocument,
  type CompanionDeliverables,
  type ComplianceReport,
} from "@/lib/api/documents";
import { generateCourse } from "@/lib/api/generation";
import { chaptersFromDocument, type DocumentChapter } from "@/lib/document/chapters";
import { wrapAroundFor } from "@/lib/editor/pairing";
import { PAGE_HEIGHT, PAGE_WIDTH, type CourseDocument } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";

const STATUS = {
  pass: { label: "Pass", Icon: CheckCircle2, className: "text-emerald-700" },
  fail: { label: "Fail", Icon: XCircle, className: "text-red-700" },
  review: { label: "Needs review", Icon: TriangleAlert, className: "text-amber-700" },
} as const;

type CompanionKey = "job_aid" | "quick_ref" | "manager_guide";
const COMPANION_ORDER: CompanionKey[] = ["job_aid", "quick_ref", "manager_guide"];

type ActiveView = { kind: "chapter"; id: string } | { kind: "companion"; key: CompanionKey };

export function OutputWorkspace({ documentId }: { documentId: string }) {
  const router = useRouter();
  const [doc, setDoc] = useState<CourseDocument | null>(null);
  const [docError, setDocError] = useState<string | null>(null);
  const [report, setReport] = useState<ComplianceReport | null>(null);
  const [companions, setCompanions] = useState<CompanionDeliverables>({});
  const [rightTab, setRightTab] = useState<"qa" | "accessibility" | "metadata">("qa");
  const [active, setActive] = useState<ActiveView | null>(null);
  const [exporting, setExporting] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await getDocument(documentId);
        if (!cancelled) setDoc(loaded);
      } catch (caught) {
        if (!cancelled) {
          setDocError(caught instanceof ApiError ? caught.message : "The workspace failed to load.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  useEffect(() => {
    let cancelled = false;
    getComplianceReport(documentId, "lom")
      .then((loaded) => {
        if (!cancelled) setReport(loaded);
      })
      .catch(() => undefined);
    getCompanionDeliverables(documentId)
      .then((loaded) => {
        if (!cancelled) setCompanions(loaded);
      })
      .catch(() => {
        if (!cancelled) setCompanions({});
      });
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  const chapters = useMemo<DocumentChapter[]>(() => (doc ? chaptersFromDocument(doc) : []), [doc]);
  const companionKeys = COMPANION_ORDER.filter((key) => companions[key]);

  useEffect(() => {
    if (!active && chapters.length > 0) setActive({ kind: "chapter", id: chapters[0].id });
  }, [active, chapters]);

  const activeChapter = active?.kind === "chapter" ? chapters.find((c) => c.id === active.id) : undefined;
  const activeCompanionKey = active?.kind === "companion" ? active.key : null;

  const handleExport = async () => {
    if (!doc || exporting) return;
    setExporting(true);
    setActionError(null);
    try {
      await generateExportedPdf(doc.document_id);
      const link = document.createElement("a");
      link.href = exportedPdfDownloadUrl(doc.document_id);
      link.download = `${doc.course_title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (caught) {
      setActionError(caught instanceof ApiError ? caught.message : "PDF export failed.");
    } finally {
      setExporting(false);
    }
  };

  const handleRegenerate = async () => {
    if (!doc || !activeChapter || regenerating) return;
    setRegenerating(true);
    setActionError(null);
    try {
      await generateCourse(doc.course_id, { chapter_ids: [activeChapter.id], force: true });
      router.push(`/generate/${doc.course_id}`);
    } catch (caught) {
      setActionError(caught instanceof ApiError ? caught.message : "Regeneration could not start.");
      setRegenerating(false);
    }
  };

  if (docError) {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <p className="rounded-[10px] border border-red-200 bg-red-50 px-3 py-2 text-[12.5px] text-red-800">
          {docError}
        </p>
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="flex items-center gap-2 text-[13px] text-ink-500">
          <Loader2 size={15} className="animate-spin text-brand-600" />
          Loading workspace…
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-canvas">
      <header className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 border-b border-line bg-white/95 px-4 py-2.5 backdrop-blur">
        <div className="flex min-w-0 items-center gap-3">
          <Button variant="outline" size="sm" onClick={() => router.push(`/editor/${documentId}`)}>
            <ArrowLeft size={13} />
            Back to editor
          </Button>
          <p className="truncate text-[13px] font-semibold text-ink">{doc.course_title}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <PhaseApprovalButton courseId={doc.course_id} />
          <Button
            variant="outline"
            size="sm"
            onClick={() => void handleRegenerate()}
            disabled={!activeChapter || regenerating}
          >
            {regenerating ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
            Regenerate section
          </Button>
          <Button size="sm" onClick={() => void handleExport()} disabled={exporting}>
            {exporting ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
            Export
          </Button>
        </div>
      </header>

      {actionError ? (
        <p className="border-b border-red-200 bg-red-50 px-4 py-2 text-[12.5px] text-red-800">{actionError}</p>
      ) : null}

      <div className="flex flex-1 items-start gap-0">
        {/* Left nav */}
        <nav className="w-[220px] shrink-0 border-r border-line bg-white px-3 py-4">
          <p className="mb-2 px-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Course</p>
          <ul className="space-y-0.5">
            {chapters.map((chapter) => (
              <li key={chapter.id}>
                <button
                  type="button"
                  onClick={() => setActive({ kind: "chapter", id: chapter.id })}
                  className={cn(
                    "w-full truncate rounded-[7px] px-2 py-1.5 text-left text-[12.5px]",
                    active?.kind === "chapter" && active.id === chapter.id
                      ? "bg-brand-50 font-medium text-brand-700"
                      : "text-ink-700 hover:bg-canvas",
                  )}
                >
                  {chapter.number}. {chapter.title}
                </button>
              </li>
            ))}
          </ul>

          {companionKeys.length > 0 ? (
            <>
              <p className="mb-2 mt-5 px-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                Companions
              </p>
              <ul className="space-y-0.5">
                {companionKeys.map((key) => (
                  <li key={key}>
                    <button
                      type="button"
                      onClick={() => setActive({ kind: "companion", key })}
                      className={cn(
                        "w-full truncate rounded-[7px] px-2 py-1.5 text-left text-[12.5px]",
                        activeCompanionKey === key
                          ? "bg-brand-50 font-medium text-brand-700"
                          : "text-ink-700 hover:bg-canvas",
                      )}
                    >
                      {companions[key]?.title}
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </nav>

        {/* Main panel */}
        <main className="flex min-w-0 flex-1 flex-col items-center gap-6 px-4 py-8">
          {activeChapter
            ? activeChapter.pages.map(({ page, blocks }, index) => (
                <div
                  key={`${page.id}-${index}`}
                  className="relative shrink-0 overflow-hidden rounded-[3px] bg-white shadow-canvas"
                  style={{ width: PAGE_WIDTH, height: PAGE_HEIGHT, background: page.background ?? "#ffffff" }}
                >
                  {blocks.map((block) => (
                    <BlockRenderer
                      key={block.id}
                      block={block}
                      documentId={doc.document_id}
                      editable={false}
                      wrapAround={wrapAroundFor(blocks, block)}
                    />
                  ))}
                </div>
              ))
            : null}

          {activeCompanionKey ? (
            <div className="w-full max-w-[700px] rounded-card border border-line bg-white px-6 py-6 text-[13px]">
              <CompanionPanel kind={activeCompanionKey} companions={companions} />
            </div>
          ) : null}

          {!activeChapter && !activeCompanionKey ? (
            <p className="py-16 text-[13px] text-ink-500">No chapters in this course yet.</p>
          ) : null}
        </main>

        {/* Right sidebar */}
        <aside className="w-[300px] shrink-0 border-l border-line bg-white px-4 py-4">
          <div className="mb-3 flex gap-1">
            {(["qa", "accessibility", "metadata"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => setRightTab(tab)}
                className={cn(
                  "rounded-[6px] border px-2 py-1 text-[11.5px] capitalize",
                  rightTab === tab ? "border-brand-400 bg-brand-50 text-brand-700" : "border-line text-ink-500",
                )}
              >
                {tab === "qa" ? "QA checklist" : tab}
              </button>
            ))}
          </div>

          {!report ? (
            <p className="flex items-center gap-2 text-[12.5px] text-ink-500">
              <Loader2 size={13} className="animate-spin" /> Loading report…
            </p>
          ) : rightTab === "qa" ? (
            <ul className="divide-y divide-line rounded-[8px] border border-line">
              {report.qa_checks.map((check) => {
                const { Icon, label, className } = STATUS[check.status];
                return (
                  <li key={check.id} className="flex gap-2 px-2.5 py-2">
                    <Icon size={14} className={cn("mt-0.5 shrink-0", className)} aria-hidden />
                    <div className="min-w-0">
                      <p className="text-[12px] font-medium text-ink">
                        {check.name} <span className={cn("text-[10.5px] font-semibold", className)}>{label}</span>
                      </p>
                      <p className="text-[11px] text-ink-500">{check.note}</p>
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : rightTab === "accessibility" ? (
            <div>
              <p className="mb-2 text-[11.5px] text-ink-500">{report.accessibility_target}</p>
              <ul className="space-y-1.5">
                {report.accessibility_flags.map((flag, index) => (
                  <li key={index} className="text-[12px] text-ink-600">
                    <strong className="font-medium text-ink">{flag.area}:</strong> {flag.message}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <dl className="space-y-1.5 text-[12px]">
              {Object.entries(report.metadata).map(([key, value]) => (
                <div key={key}>
                  <dt className="text-ink-500">{key}</dt>
                  <dd className="break-words text-ink">{Array.isArray(value) ? value.join(", ") : value}</dd>
                </div>
              ))}
            </dl>
          )}

          {report && report.unresolved_flags.length > 0 ? (
            <div className="mt-5">
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                Unresolved flags · {report.unresolved_flags.length}
              </p>
              <ul className="space-y-1.5">
                {report.unresolved_flags.map((flag, index) => (
                  <li key={index} className="text-[11.5px] text-amber-800">
                    {flag}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

function PlanHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 mt-6 text-[12px] font-semibold uppercase tracking-wide text-ink-500 first:mt-0">
      {children}
    </h3>
  );
}

/** Mirrors ReportButton.tsx's CompanionDeliverablesSection, scoped to one
 * companion at a time (the left nav already separates them into their own
 * views here, unlike the Report modal which shows all of them together). */
function CompanionPanel({ kind, companions }: { kind: CompanionKey; companions: CompanionDeliverables }) {
  if (kind === "job_aid" && companions.job_aid) {
    const { title, sections } = companions.job_aid;
    return (
      <>
        <PlanHeading>{title}</PlanHeading>
        {sections.map((section) => (
          <div key={section.chapter} className="mb-3">
            <p className="font-medium text-ink">{section.chapter}</p>
            <ul className="list-disc space-y-1 pl-5 text-ink-600">
              {section.checklist.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
            {section.watch_outs.length > 0 ? (
              <ul className="mt-1 space-y-1">
                {section.watch_outs.map((item, index) => (
                  <li key={index} className="text-[12px] text-amber-800">
                    Watch out: {item}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ))}
      </>
    );
  }

  if (kind === "quick_ref" && companions.quick_ref) {
    const { title, sections } = companions.quick_ref;
    return (
      <>
        <PlanHeading>{title}</PlanHeading>
        {sections.map((section) => (
          <div key={section.chapter} className="mb-3">
            <p className="font-medium text-ink">{section.chapter}</p>
            <ul className="list-disc space-y-1 pl-5 text-ink-600">
              {section.key_points.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        ))}
      </>
    );
  }

  if (kind === "manager_guide" && companions.manager_guide) {
    const { title, note, sections } = companions.manager_guide;
    return (
      <>
        <PlanHeading>{title}</PlanHeading>
        <p className="mb-3 text-ink-600">{note}</p>
        {sections.map((section) => (
          <div key={section.chapter} className="mb-3">
            <p className="font-medium text-ink">{section.chapter}</p>
            <ul className="list-disc space-y-1 pl-5 text-ink-600">
              {section.coaching_prompts.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
            {section.scenario_notes.length > 0 ? (
              <ul className="mt-1 list-disc space-y-1 pl-5 text-[12px] text-ink-500">
                {section.scenario_notes.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ))}
      </>
    );
  }

  return null;
}
