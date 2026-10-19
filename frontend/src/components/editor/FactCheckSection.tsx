"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Loader2, RotateCcw, ShieldCheck, X } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  getAudit,
  runAudit,
  setFindingStatus,
  type AuditFinding,
  type AuditReport,
  type FindingKind,
  type FindingStatus,
} from "@/lib/api/audit";
import { cn } from "@/lib/utils/cn";

const KIND: Record<FindingKind, { label: string; hint: string }> = {
  invalid_citation: { label: "Made-up source", hint: "Cites a reference that does not exist." },
  contradicted: { label: "Contradicts source", hint: "The source documents say something different." },
  weak_citation: { label: "Weak citation", hint: "The cited passage may not support this." },
  unsupported: { label: "Not in sources", hint: "A specific claim the sources do not state." },
  assumption: { label: "Assumption", hint: "General content the sources do not cover." },
};

const SEVERITY_STYLE = {
  high: "bg-red-50 text-red-700",
  medium: "bg-amber-50 text-amber-800",
  low: "bg-slate-100 text-slate-600",
} as const;

/**
 * The fact check: every factual-looking sentence of the course compared with
 * the creator's own source documents. A reviewer works through the findings
 * and marks each accepted (it is fine) or dismissed (a false alarm); those
 * decisions are kept when the check is re-run.
 */
export function FactCheckSection({ courseId }: { courseId: string }) {
  const [report, setReport] = useState<AuditReport | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [showLow, setShowLow] = useState(false);
  const [showDecided, setShowDecided] = useState(false);

  const load = useCallback(async () => {
    try {
      setReport(await getAudit(courseId));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The fact check could not be loaded.");
      setReport(null);
    }
  }, [courseId]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      setReport(await runAudit(courseId));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The fact check failed.");
    } finally {
      setRunning(false);
    }
  };

  const decide = async (finding: AuditFinding, status: FindingStatus) => {
    try {
      const updated = await setFindingStatus(courseId, finding.finding_id, status);
      setReport((current) =>
        current
          ? {
              ...current,
              findings: current.findings.map((item) =>
                item.finding_id === updated.finding_id ? { ...item, status: updated.status } : item,
              ),
              summary: {
                ...current.summary,
                open:
                  current.summary.open +
                  (updated.status === "open" ? 1 : 0) -
                  (finding.status === "open" ? 1 : 0),
              },
            }
          : current,
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The decision could not be saved.");
    }
  };

  const visible = (report?.findings ?? []).filter(
    (finding) => (showLow || finding.severity !== "low") && (showDecided || finding.status === "open"),
  );

  return (
    <section className="mt-6 text-[13px]">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-[12px] font-semibold uppercase tracking-wide text-ink-500">
          Fact check against source documents
        </h3>
        <button
          type="button"
          onClick={() => void run()}
          disabled={running}
          className="inline-flex items-center gap-1.5 rounded-[6px] border border-line px-2 py-1 text-[11.5px] text-ink-700 hover:border-brand-300 disabled:opacity-50"
        >
          {running ? <Loader2 size={12} className="animate-spin" /> : report ? <RotateCcw size={12} /> : <ShieldCheck size={12} />}
          {report ? "Run again" : "Run fact check"}
        </button>
      </div>

      {error ? <p className="mb-2 text-[12px] text-red-700">{error}</p> : null}
      {report === undefined ? (
        <p className="text-ink-500">Loading…</p>
      ) : report === null ? (
        <p className="text-[12px] text-ink-500">
          Not run yet. It compares the course with the source documents it was written from, so it needs a
          course that has some.
        </p>
      ) : (
        <>
          <p className="mb-2 text-ink-600">
            {report.sentences_checked} statements checked · {report.summary.supported_percent}% match the
            sources · {report.summary.cited_percent}% cite them ·{" "}
            <strong className="font-medium text-ink">{report.summary.open} open</strong> (
            {report.summary.by_severity.high} high, {report.summary.by_severity.medium} medium,{" "}
            {report.summary.by_severity.low} low)
            {report.ai_used ? " · doubtful statements were also read by the AI" : " · checked by rules only"}
          </p>
          {report.warnings.map((warning) => (
            <p key={warning} className="mb-2 rounded-[8px] border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
              {warning}
            </p>
          ))}
          <div className="mb-2 flex flex-wrap gap-4 text-[12px] text-ink-600">
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={showLow} onChange={(e) => setShowLow(e.target.checked)} />
              Show assumptions (low)
            </label>
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={showDecided} onChange={(e) => setShowDecided(e.target.checked)} />
              Show reviewed
            </label>
          </div>

          {visible.length === 0 ? (
            <p className="rounded-[8px] border border-line px-3 py-3 text-ink-500">
              {report.findings.length === 0
                ? "Nothing was flagged: every checked statement matches the sources."
                : "Nothing left to review with these filters."}
            </p>
          ) : (
            <ul className="space-y-2">
              {visible.map((finding) => (
                <FindingCard key={finding.finding_id} finding={finding} onDecide={decide} />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function FindingCard({
  finding,
  onDecide,
}: {
  finding: AuditFinding;
  onDecide: (finding: AuditFinding, status: FindingStatus) => void;
}) {
  const kind = KIND[finding.kind];
  const decided = finding.status !== "open";
  return (
    <li className={cn("rounded-[8px] border border-line px-3 py-2.5", decided && "opacity-60")}>
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px]">
        <span className={cn("rounded px-1.5 py-0.5 font-semibold uppercase", SEVERITY_STYLE[finding.severity])}>
          {finding.severity}
        </span>
        <span className="font-medium text-ink" title={kind.hint}>
          {kind.label}
        </span>
        <span className="text-ink-400">
          {finding.chapter}
          {finding.page_number ? ` · page ${finding.page_number}` : ""}
          {finding.verified_by === "ai" ? " · AI-checked" : ""}
        </span>
        {decided ? (
          <span className="font-medium text-ink-500">{finding.status === "accepted" ? "Accepted" : "Dismissed"}</span>
        ) : null}
      </div>
      <p className="text-ink">“{finding.sentence}”</p>
      {finding.explanation ? <p className="mt-1 text-[12px] text-ink-500">{finding.explanation}</p> : null}
      {finding.evidence.map((item) => (
        <blockquote key={item.ref} className="mt-1.5 border-l-2 border-line pl-2.5 text-[12px] text-ink-600">
          {item.excerpt}
          <span className="block text-[11px] text-ink-400">
            [{item.ref}] {item.filename}
            {item.page ? `, page ${item.page}` : ""}
          </span>
        </blockquote>
      ))}
      <div className="mt-2 flex gap-2">
        {decided ? (
          <button type="button" onClick={() => onDecide(finding, "open")} className="text-[11.5px] text-ink-500 underline">
            Reopen
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => onDecide(finding, "accepted")}
              className="inline-flex items-center gap-1 rounded-[6px] border border-emerald-200 px-2 py-0.5 text-[11.5px] text-emerald-700 hover:bg-emerald-50"
            >
              <Check size={11} /> Accept
            </button>
            <button
              type="button"
              onClick={() => onDecide(finding, "dismissed")}
              className="inline-flex items-center gap-1 rounded-[6px] border border-line px-2 py-0.5 text-[11.5px] text-ink-600 hover:bg-slate-50"
            >
              <X size={11} /> Dismiss
            </button>
          </>
        )}
      </div>
    </li>
  );
}
