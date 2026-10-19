"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, ClipboardCheck, Loader2, TriangleAlert, XCircle } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ApiError } from "@/lib/api/client";
import {
  getComplianceReport,
  getDesignOutputs,
  type ComplianceReport,
  type DesignOutputs,
} from "@/lib/api/documents";
import { useEditor } from "@/lib/editor/store";
import { FactCheckSection } from "./FactCheckSection";
import { cn } from "@/lib/utils/cn";

const STATUS = {
  pass: { label: "Pass", Icon: CheckCircle2, className: "text-emerald-700" },
  fail: { label: "Fail", Icon: XCircle, className: "text-red-700" },
  review: { label: "Needs review", Icon: TriangleAlert, className: "text-amber-700" },
} as const;

/**
 * The end-of-course report card: the 8 quality checks, accessibility flags and
 * a metadata block. It reads the SAVED document, so unsaved edits are not in it.
 */
export function ReportButton() {
  const editor = useEditor();
  const documentId = editor.document?.document_id ?? null;
  const [open, setOpen] = useState(false);
  const [schema, setSchema] = useState<"lom" | "dublin_core">("lom");
  const [report, setReport] = useState<ComplianceReport | null>(null);
  const [plans, setPlans] = useState<DesignOutputs>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!documentId) return;
    setLoading(true);
    setError(null);
    try {
      setReport(await getComplianceReport(documentId, schema));
      // Plan documents exist only when a design model that produces one was chosen.
      setPlans(await getDesignOutputs(documentId).catch(() => ({})));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The report could not be built.");
    } finally {
      setLoading(false);
    }
  }, [documentId, schema]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  if (!documentId) return null;

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <ClipboardCheck size={13} />
        Report
      </Button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Course quality report"
        subtitle="Quality checks, accessibility flags and metadata for the saved course"
        width="max-w-3xl"
      >
        {loading && !report ? (
          <p className="flex items-center gap-2 text-[13px] text-ink-500">
            <Loader2 size={14} className="animate-spin" /> Building the report…
          </p>
        ) : null}
        {error ? <p className="text-[13px] text-red-700">{error}</p> : null}
        {editor.dirty ? (
          <p className="mb-3 rounded-[8px] border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
            You have unsaved changes. This report reflects the last saved version.
          </p>
        ) : null}
        {report ? <ReportBody report={report} schema={schema} onSchema={setSchema} /> : null}
        {report ? <DesignPlans plans={plans} /> : null}
        {editor.document ? <FactCheckSection courseId={editor.document.course_id} /> : null}
      </Modal>
    </>
  );
}

function ReportBody({
  report,
  schema,
  onSchema,
}: {
  report: ComplianceReport;
  schema: "lom" | "dublin_core";
  onSchema: (schema: "lom" | "dublin_core") => void;
}) {
  return (
    <div className="space-y-6 text-[13px]">
      <p className="text-ink-600">
        {report.summary.pass} passed · {report.summary.review} need a person to confirm ·{" "}
        {report.summary.fail} failed
      </p>

      <section>
        <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-ink-500">
          Quality checks
        </h3>
        <ul className="divide-y divide-line rounded-[8px] border border-line">
          {report.qa_checks.map((check) => {
            const { Icon, label, className } = STATUS[check.status];
            return (
              <li key={check.id} className="flex gap-3 px-3 py-2.5">
                <Icon size={16} className={cn("mt-0.5 shrink-0", className)} aria-hidden />
                <div className="min-w-0">
                  <p className="font-medium text-ink">
                    {check.name} <span className={cn("ml-1 text-[11.5px] font-semibold", className)}>{label}</span>
                  </p>
                  <p className="text-[12px] text-ink-500">{check.note}</p>
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <section>
        <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-ink-500">
          Accessibility ({report.accessibility_target})
        </h3>
        <ul className="space-y-1.5">
          {report.accessibility_flags.map((flag, index) => (
            <li key={index} className="flex gap-2 text-[12.5px]">
              <span
                className={cn(
                  "mt-0.5 shrink-0 rounded px-1.5 text-[10.5px] font-semibold uppercase",
                  flag.severity === "fail" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-800",
                )}
              >
                {flag.severity === "fail" ? "Fix" : "Review"}
              </span>
              <span className="text-ink-600">
                <strong className="font-medium text-ink">{flag.area}:</strong> {flag.message}
                {flag.location ? <span className="text-ink-400"> ({flag.location})</span> : null}
              </span>
            </li>
          ))}
        </ul>
        {report.asset_requirements.length > 0 ? (
          <p className="mt-2 text-[12px] text-ink-500">
            {report.asset_requirements.filter((asset) => !asset.present).length} of{" "}
            {report.asset_requirements.length} images are missing alt text.
          </p>
        ) : null}
      </section>

      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-[12px] font-semibold uppercase tracking-wide text-ink-500">Metadata</h3>
          <div className="flex gap-1">
            {(["lom", "dublin_core"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => onSchema(option)}
                className={cn(
                  "rounded-[6px] border px-2 py-0.5 text-[11.5px]",
                  schema === option ? "border-brand-400 bg-brand-50 text-brand-700" : "border-line text-ink-500",
                )}
              >
                {option === "lom" ? "LOM" : "Dublin Core"}
              </button>
            ))}
          </div>
        </div>
        <dl className="grid grid-cols-[170px_1fr] gap-x-3 gap-y-1.5 rounded-[8px] border border-line p-3 text-[12.5px]">
          {Object.entries(report.metadata).map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="text-ink-500">{key}</dt>
              <dd className="break-words text-ink">{Array.isArray(value) ? value.join(", ") : value}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}

function PlanHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 mt-6 text-[12px] font-semibold uppercase tracking-wide text-ink-500">
      {children}
    </h3>
  );
}

/** ADDIE's implementation/evaluation notes and Kirkpatrick's four-level plan. */
function DesignPlans({ plans }: { plans: DesignOutputs }) {
  const { addie, kirkpatrick } = plans;
  if (!addie && !kirkpatrick) return null;
  return (
    <div className="text-[13px]">
      {addie ? (
        <section>
          <PlanHeading>ADDIE: implementation notes</PlanHeading>
          <p className="mb-2 text-ink-600">{addie.implementation.sequencing}</p>
          <ul className="divide-y divide-line rounded-[8px] border border-line">
            {addie.implementation.modules.map((module) => (
              <li key={module.module} className="flex flex-wrap justify-between gap-2 px-3 py-2">
                <span className="font-medium text-ink">{module.module}</span>
                <span className="text-[12px] text-ink-500">
                  after: {module.prerequisite} · tracking: {module.tracking}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[12px] text-ink-500">{addie.implementation.tracking_note}</p>
          <p className="text-[12px] text-ink-500">{addie.implementation.rollout_note}</p>
          <PlanHeading>ADDIE: evaluation summary</PlanHeading>
          <ul className="list-disc space-y-1 pl-5 text-ink-600">
            {Object.entries(addie.evaluation_summary).map(([key, text]) => (
              <li key={key}>{text}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {kirkpatrick ? (
        <section>
          <PlanHeading>Kirkpatrick evaluation plan</PlanHeading>
          <p className="mb-3 text-ink-600">Goal: {kirkpatrick.business_goal}</p>
          {kirkpatrick.flags.map((flag) => (
            <p
              key={flag}
              className="mb-2 rounded-[8px] border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-800"
            >
              {flag}
            </p>
          ))}
          <p className="font-medium text-ink">Level 1 - Reaction ({kirkpatrick.level_1_reaction.timing})</p>
          <ul className="mb-3 list-disc pl-5 text-ink-600">
            {kirkpatrick.level_1_reaction.questions.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
          <p className="font-medium text-ink">Level 2 - Learning ({kirkpatrick.level_2_learning.timing})</p>
          <ul className="mb-3 list-disc pl-5 text-ink-600">
            {kirkpatrick.level_2_learning.items.map((item, index) => (
              <li key={index}>
                {item.objective} <span className="text-ink-400">({item.bloom_level}: {item.assessment})</span>
              </li>
            ))}
          </ul>
          <p className="font-medium text-ink">Level 3 - Behavior ({kirkpatrick.level_3_behavior.timing})</p>
          <ul className="mb-3 list-disc pl-5 text-ink-600">
            {kirkpatrick.level_3_behavior.checklist.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <p className="font-medium text-ink">Level 4 - Results ({kirkpatrick.level_4_results.timing})</p>
          <p className="text-ink-600">{kirkpatrick.level_4_results.measure}</p>
          <p className="mb-3 text-[12px] text-ink-500">{kirkpatrick.level_4_results.note}</p>
          <p className="font-medium text-ink">Data sources</p>
          <p className="text-ink-600">
            Measurable in the LMS now: {kirkpatrick.data_sources.lms_measurable_now.join(", ")}. Needs
            external collection: {kirkpatrick.data_sources.external_collection.join(", ")}.
          </p>
          <p className="text-[12px] text-ink-500">{kirkpatrick.data_sources.note}</p>
        </section>
      ) : null}
    </div>
  );
}
