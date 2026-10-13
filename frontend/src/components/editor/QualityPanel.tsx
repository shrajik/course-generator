"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, ShieldAlert } from "lucide-react";
import { getQualityReport } from "@/lib/api/courses";
import { useEditor } from "@/lib/editor/store";
import type { QualityFinding, QualityReportResponse } from "@/lib/types/course";
import { cn } from "@/lib/utils/cn";

const DIMENSION_LABELS: Record<QualityFinding["dimension"], string> = {
  accuracy: "Accuracy",
  completeness: "Completeness",
  pedagogy: "Pedagogy",
  visual: "Visual",
  document: "Document",
};

const SEVERITY_STYLE: Record<QualityFinding["severity"], string> = {
  blocker: "border-red-200 bg-red-50 text-red-800",
  major: "border-amber-200 bg-amber-50 text-amber-800",
  minor: "border-line bg-cream-50 text-ink-500",
};

function FindingCard({ finding }: { finding: QualityFinding }) {
  return (
    <div className={cn("rounded-[8px] border px-2.5 py-2 text-[11.5px]", SEVERITY_STYLE[finding.severity])}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold uppercase tracking-wide text-[9.5px]">
          {DIMENSION_LABELS[finding.dimension]} · {finding.severity}
        </span>
        <span className="truncate text-[10px] opacity-70" title={finding.artifact}>
          {finding.artifact}
        </span>
      </div>
      <p className="mt-1 leading-snug">{finding.description}</p>
      {finding.remediation ? (
        <p className="mt-1 text-[10.5px] italic opacity-80">{finding.remediation}</p>
      ) : null}
    </div>
  );
}

export function QualityPanel() {
  const editor = useEditor();
  const doc = editor.document;
  const courseId = doc?.course_id ?? null;

  const [data, setData] = useState<QualityReportResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!courseId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getQualityReport(courseId)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load the quality report.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [courseId]);

  if (!doc) return null;
  const sources = doc.meta.sources ?? [];
  const report = data?.report ?? null;
  const snapshot = data?.historical_snapshot ?? null;

  return (
    <div className="flex h-full flex-col">
      <div className="px-3 pb-2 pt-3">
        <p className="text-[12.5px] font-semibold text-ink">Quality &amp; Sources</p>
        <p className="mt-0.5 text-[11px] leading-snug text-ink-400">
          {report
            ? `Standards v${report.standards_version} (${report.standards_status}) - findings reflect an AI reviewer's judgement, not verified fact.`
            : "Checks already run during generation, surfaced here."}
        </p>
        {snapshot ? (
          <p className="mt-1 text-[10px] text-ink-400">
            Last evaluated at generation: {snapshot.outcome} ({snapshot.hard_failure_count} hard
            failure{snapshot.hard_failure_count === 1 ? "" : "s"}) - the findings below are a live
            recheck and may differ if this document was edited since.
          </p>
        ) : null}
      </div>

      <div className="subtle-scroll flex-1 space-y-4 overflow-y-auto px-2.5 pb-3">
        <section>
          <p className="mb-1.5 flex items-center gap-1.5 px-0.5 text-[11px] font-semibold text-ink-700">
            <ShieldAlert size={12} className="text-ink-400" />
            Quality findings
          </p>
          {loading ? (
            <p className="flex items-center gap-1.5 px-1 text-[11.5px] text-ink-400">
              <Loader2 size={12} className="animate-spin" />
              Loading…
            </p>
          ) : error ? (
            <p className="flex items-center gap-1.5 px-1 text-[11.5px] text-red-700">
              <AlertTriangle size={12} />
              {error}
            </p>
          ) : !report || report.findings.length === 0 ? (
            <p className="flex items-center gap-1.5 px-1 text-[11.5px] text-ink-400">
              <CheckCircle2 size={12} className="text-brand-500" />
              No findings.
            </p>
          ) : (
            <div className="space-y-2">
              {[...report.findings]
                .sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "blocker" ? -1 : 1))
                .map((finding, index) => (
                  <FindingCard key={`${finding.artifact}-${finding.reason}-${index}`} finding={finding} />
                ))}
            </div>
          )}
        </section>

        <section>
          <p className="mb-1.5 px-0.5 text-[11px] font-semibold text-ink-700">Sources</p>
          {sources.length === 0 ? (
            <p className="px-1 text-[11.5px] text-ink-400">No external sources recorded.</p>
          ) : (
            <div className="space-y-2">
              {sources.map((source, index) => (
                <div key={`${source.url}-${index}`} className="rounded-[8px] border border-line px-2.5 py-2">
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-start gap-1 text-[11.5px] font-medium text-brand-700 hover:underline"
                  >
                    <ExternalLink size={11} className="mt-[2px] shrink-0" />
                    {source.title || source.url}
                  </a>
                  {source.note ? (
                    <p className="mt-0.5 text-[10.5px] leading-snug text-ink-400">{source.note}</p>
                  ) : null}
                  {source.chapter_ids.length > 0 ? (
                    <p className="mt-1 text-[10px] text-ink-400">
                      Cited in {source.chapter_ids.length} chapter{source.chapter_ids.length === 1 ? "" : "s"}
                    </p>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
