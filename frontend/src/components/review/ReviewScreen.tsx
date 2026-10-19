"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Check, CheckCircle2, Loader2, RotateCcw, ShieldAlert, ShieldCheck, ThumbsDown, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ApiError } from "@/lib/api/client";
import {
  getAudit,
  runAudit,
  setFindingStatus,
  type AuditFinding,
  type AuditReport,
  type FindingStatus,
} from "@/lib/api/audit";
import { approveCourse, getCourseReview, requestCourseChanges } from "@/lib/api/courses";
import { assetUrl, getDocument } from "@/lib/api/documents";
import {
  getCourseSources,
  getPassagesByRefs,
  getReviewBrief,
  getSourcePassages,
  type CourseSourceInfo,
  type Passage,
  type ReviewBrief,
} from "@/lib/api/review";
import { useAuth } from "@/lib/auth/auth-provider";
import { segmentText, sourceNumber, splitMarkers } from "@/lib/review/highlight";
import type { CourseReview } from "@/lib/types/course";
import type { Block, CourseDocument } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";

const SEVERITY_MARK = {
  high: "bg-red-100 border-b-2 border-red-400",
  medium: "bg-amber-100 border-b-2 border-amber-400",
  low: "bg-yellow-50 border-b border-yellow-300",
} as const;

const KIND_LABEL: Record<AuditFinding["kind"], string> = {
  invalid_citation: "Made-up source",
  contradicted: "Contradicts source",
  weak_citation: "Weak citation",
  unsupported: "Not in sources",
  assumption: "Assumption",
};

const TEXT_TYPES = new Set(["paragraph", "callout", "tip", "warning", "quote", "story"]);

/**
 * Gate 2: the subject-matter expert reviews the course beside the documents it
 * was written from. Every statement the fact check flagged is highlighted in
 * place; selecting one shows why and the passage of the sources that bears on
 * it; each can be accepted or dismissed. Final approval is available once no
 * high-severity finding is left undecided.
 */
export function ReviewScreen({ documentId }: { documentId: string }) {
  const { user } = useAuth();
  const [doc, setDoc] = useState<CourseDocument | null>(null);
  const [audit, setAudit] = useState<AuditReport | null>(null);
  const [brief, setBrief] = useState<ReviewBrief | null>(null);
  const [review, setReview] = useState<CourseReview | null>(null);
  const [sources, setSources] = useState<CourseSourceInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeRef, setActiveRef] = useState<string | null>(null);
  const [showLow, setShowLow] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"approve" | "changes" | null>(null);
  const [comment, setComment] = useState("");

  const courseId = doc?.course_id ?? null;

  // The document comes first: it names the course everything else belongs to.
  useEffect(() => {
    let live = true;
    getDocument(documentId)
      .then((loaded) => live && setDoc(loaded))
      .catch((caught) => live && setError(caught instanceof ApiError ? caught.message : "The course could not be loaded."));
    return () => {
      live = false;
    };
  }, [documentId]);

  const loadAll = useCallback(async (id: string) => {
    const [auditResult, briefResult, reviewResult, sourceResult] = await Promise.allSettled([
      getAudit(id),
      getReviewBrief(id),
      getCourseReview(id),
      getCourseSources(id),
    ]);
    if (auditResult.status === "fulfilled") setAudit(auditResult.value);
    if (briefResult.status === "fulfilled") setBrief(briefResult.value);
    if (reviewResult.status === "fulfilled") setReview(reviewResult.value);
    if (sourceResult.status === "fulfilled") setSources(sourceResult.value);
  }, []);

  useEffect(() => {
    if (courseId) void loadAll(courseId);
  }, [courseId, loadAll]);

  const refreshBrief = useCallback(async () => {
    if (!courseId) return;
    try {
      setBrief(await getReviewBrief(courseId));
    } catch {
      // The brief is advisory; keep the last one.
    }
  }, [courseId]);

  const findings = useMemo(() => audit?.findings ?? [], [audit]);
  const selected = findings.find((f) => f.finding_id === selectedId) ?? null;

  const byBlock = useMemo(() => {
    const map = new Map<string, AuditFinding[]>();
    for (const finding of findings) {
      if (finding.status !== "open" || (finding.severity === "low" && !showLow)) continue;
      map.set(finding.block_id, [...(map.get(finding.block_id) ?? []), finding]);
    }
    return map;
  }, [findings, showLow]);

  const select = (finding: AuditFinding) => {
    setSelectedId(finding.finding_id);
    setActiveRef(finding.evidence[0]?.ref ?? finding.refs[0] ?? null);
    document.getElementById(`block-${finding.block_id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const decide = async (finding: AuditFinding, status: FindingStatus) => {
    if (!courseId) return;
    setBusy(finding.finding_id);
    setError(null);
    try {
      const updated = await setFindingStatus(courseId, finding.finding_id, status);
      setAudit((current) =>
        current
          ? {
              ...current,
              findings: current.findings.map((f) => (f.finding_id === updated.finding_id ? { ...f, status: updated.status } : f)),
            }
          : current,
      );
      await refreshBrief();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The decision could not be saved.");
    } finally {
      setBusy(null);
    }
  };

  const rerun = async () => {
    if (!courseId) return;
    setBusy("audit");
    setError(null);
    try {
      setAudit(await runAudit(courseId));
      await refreshBrief();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The fact check failed.");
    } finally {
      setBusy(null);
    }
  };

  const submitDecision = async () => {
    if (!courseId || !dialog) return;
    setBusy("decision");
    setError(null);
    try {
      const next = dialog === "approve" ? await approveCourse(courseId, comment.trim() || undefined) : await requestCourseChanges(courseId, comment.trim());
      setReview(next);
      setDialog(null);
      setComment("");
      await refreshBrief();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The decision could not be recorded.");
      setDialog(null);
    } finally {
      setBusy(null);
    }
  };

  const isReviewer = user?.role === "editor_reviewer" || user?.role === "admin";
  const canDecide = isReviewer && review?.review_status === "in_review";
  const blockedReason = brief && !brief.ready_to_approve ? brief.blockers[0] : null;

  if (error && !doc) {
    return <p className="p-8 text-[13px] text-red-700">{error}</p>;
  }
  if (!doc) {
    return (
      <p className="flex items-center gap-2 p-8 text-[13px] text-ink-500">
        <Loader2 size={15} className="animate-spin" /> Loading the course…
      </p>
    );
  }

  return (
    <div className="editor-shell flex flex-col overflow-hidden bg-canvas">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-white px-5 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <Link href={`/editor/${documentId}`} className="creator-icon-button" aria-label="Back to the editor">
            <ArrowLeft size={16} />
          </Link>
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-[0.1em] text-ink-400">Expert review</p>
            <h1 className="truncate text-[15px] font-semibold text-ink">{doc.course_title}</h1>
          </div>
          {review ? <StatusBadge status={review.review_status} /> : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void rerun()} disabled={busy === "audit"}>
            {busy === "audit" ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
            Run fact check
          </Button>
          {canDecide ? (
            <>
              <Button variant="outline" size="sm" onClick={() => setDialog("changes")}>
                <ThumbsDown size={13} /> Request changes
              </Button>
              <Button size="sm" onClick={() => setDialog("approve")} disabled={Boolean(blockedReason)} title={blockedReason ?? "Give this course final approval"}>
                <ShieldCheck size={13} /> Final SME approval
              </Button>
            </>
          ) : null}
        </div>
      </header>

      {error ? <p className="border-b border-red-200 bg-red-50 px-5 py-2 text-[12.5px] text-red-700">{error}</p> : null}

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1.25fr)_minmax(360px,1fr)]">
        <main className="min-h-0 overflow-y-auto px-6 py-6" aria-label="Course content">
          <div className="mx-auto max-w-[760px] rounded-panel border border-line bg-white px-8 py-7 shadow-sm">
            <Legend showLow={showLow} onShowLow={setShowLow} hasAudit={audit !== null} />
            {doc.pages
              .filter((page) => page.kind === "content")
              .flatMap((page) => page.blocks)
              .map((block) => (
                <BlockView
                  key={block.id}
                  block={block}
                  documentId={documentId}
                  findings={byBlock.get(block.id) ?? []}
                  selectedId={selectedId}
                  onSelect={select}
                  onRef={setActiveRef}
                />
              ))}
          </div>
        </main>

        <aside className="min-h-0 space-y-4 overflow-y-auto border-l border-line bg-white px-5 py-5" aria-label="Review panel">
          {brief ? <BriefCard brief={brief} /> : null}
          {selected ? (
            <FindingCard finding={selected} busy={busy === selected.finding_id} onDecide={decide} onRef={setActiveRef} />
          ) : (
            <p className="rounded-[8px] border border-dashed border-line px-3 py-3 text-[12.5px] text-ink-500">
              {findings.length === 0
                ? audit
                  ? "The fact check found nothing to review."
                  : "No fact check yet. Run it to compare the course with its source documents."
                : "Select a highlighted statement in the course to see why it was flagged and what the sources say."}
            </p>
          )}
          {courseId ? <SourceViewer courseId={courseId} sources={sources} activeRef={activeRef} /> : null}
        </aside>
      </div>

      <Modal
        open={dialog !== null}
        onClose={() => setDialog(null)}
        title={dialog === "approve" ? "Final SME approval" : "Request changes"}
        subtitle={dialog === "approve" ? "Confirm you have checked this course against its sources." : "Tell the author what must change."}
        footer={
          <Button
            size="sm"
            onClick={() => void submitDecision()}
            disabled={busy === "decision" || (dialog === "changes" && !comment.trim())}
          >
            {busy === "decision" ? <Loader2 size={13} className="animate-spin" /> : null}
            {dialog === "approve" ? "Approve course" : "Send back"}
          </Button>
        }
      >
        <label htmlFor="decision-comment" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
          {dialog === "approve" ? "Comment (optional)" : "What needs to change"}
        </label>
        <textarea
          id="decision-comment"
          rows={4}
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          className="w-full rounded-lg border border-line px-3 py-2 text-[13px] outline-none focus:border-brand-400"
        />
      </Modal>
    </div>
  );
}

function StatusBadge({ status }: { status: CourseReview["review_status"] }) {
  const style = {
    draft: "border-line bg-canvas text-ink-500",
    in_review: "border-blue-200 bg-blue-50 text-blue-800",
    changes_requested: "border-amber-200 bg-amber-50 text-amber-800",
    approved: "border-emerald-200 bg-emerald-50 text-emerald-800",
  }[status];
  return (
    <span className={cn("rounded-full border px-2.5 py-0.5 text-[11px] font-medium", style)}>
      {status.replace("_", " ").replace(/^\w/, (c) => c.toUpperCase())}
    </span>
  );
}

function Legend({ showLow, onShowLow, hasAudit }: { showLow: boolean; onShowLow: (v: boolean) => void; hasAudit: boolean }) {
  return (
    <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-line pb-3 text-[11.5px] text-ink-500">
      <span className="font-medium text-ink-600">Highlights:</span>
      <span><span className={cn("mr-1 inline-block h-2.5 w-4 align-middle", SEVERITY_MARK.high)} />High</span>
      <span><span className={cn("mr-1 inline-block h-2.5 w-4 align-middle", SEVERITY_MARK.medium)} />Medium</span>
      <label className="flex items-center gap-1.5">
        <input type="checkbox" checked={showLow} onChange={(e) => onShowLow(e.target.checked)} />
        <span className={cn("inline-block h-2.5 w-4 align-middle", SEVERITY_MARK.low)} /> Assumptions
      </label>
      {!hasAudit ? <span className="text-amber-700">No fact check has been run.</span> : null}
    </div>
  );
}

// --- the course, read-only -------------------------------------------------------

function HighlightedText({
  text,
  findings,
  selectedId,
  onSelect,
  onRef,
}: {
  text: string;
  findings: AuditFinding[];
  selectedId: string | null;
  onSelect: (finding: AuditFinding) => void;
  onRef: (ref: string) => void;
}) {
  return (
    <>
      {segmentText(text, findings).map((segment, index) => {
        const inner = splitMarkers(segment.text).map((part, i) =>
          part.kind === "text" ? (
            <span key={i}>{part.text}</span>
          ) : (
            <button
              key={i}
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onRef(part.ref);
              }}
              className="mx-0.5 rounded bg-brand-50 px-1 text-[11px] font-medium text-brand-700 hover:bg-brand-100"
              title={`Show passage ${part.ref}`}
            >
              {part.label}
            </button>
          ),
        );
        if (!segment.finding) return <span key={index}>{inner}</span>;
        const finding = segment.finding;
        return (
          <mark
            key={index}
            role="button"
            tabIndex={0}
            onClick={() => onSelect(finding)}
            onKeyDown={(event) => (event.key === "Enter" || event.key === " ") && onSelect(finding)}
            className={cn(
              "cursor-pointer rounded-[2px] px-0.5 text-inherit",
              SEVERITY_MARK[finding.severity],
              finding.finding_id === selectedId && "ring-2 ring-brand-500",
            )}
            title={KIND_LABEL[finding.kind]}
          >
            {inner}
          </mark>
        );
      })}
    </>
  );
}

function asLines(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value] : [];
  if (Array.isArray(value)) return value.flatMap(asLines);
  if (value && typeof value === "object") return Object.values(value).flatMap(asLines);
  return [];
}

function BlockView({
  block,
  documentId,
  findings,
  selectedId,
  onSelect,
  onRef,
}: {
  block: Block;
  documentId: string;
  findings: AuditFinding[];
  selectedId: string | null;
  onSelect: (finding: AuditFinding) => void;
  onRef: (ref: string) => void;
}) {
  const c = block.content as Record<string, unknown>;
  const text = typeof c.text === "string" ? c.text : "";
  const wrap = (child: React.ReactNode) => (
    <div id={`block-${block.id}`} className="mb-4 scroll-mt-24">
      {child}
    </div>
  );

  if (block.type === "heading") {
    const level = Number(c.level ?? 2);
    const cls = level <= 1 ? "mt-8 text-[22px] font-bold" : level === 2 ? "mt-6 text-[17px] font-semibold" : "mt-4 text-[14.5px] font-semibold";
    return wrap(<h2 className={cn("text-ink", cls)}>{text}</h2>);
  }

  if (TEXT_TYPES.has(block.type)) {
    const label = block.type === "paragraph" || block.type === "story" || block.type === "quote" ? null : String(c.title || block.type);
    return wrap(
      <div className={cn("text-[13.5px] leading-6 text-ink-800", label && "rounded-[8px] border border-line bg-canvas px-3.5 py-2.5")}>
        {label ? <p className="mb-0.5 text-[11px] font-semibold uppercase tracking-wide text-ink-500">{label}</p> : null}
        <HighlightedText text={text} findings={findings} selectedId={selectedId} onSelect={onSelect} onRef={onRef} />
      </div>,
    );
  }

  if (block.type === "image") {
    if (c.kind === "toc") return null;
    const path = typeof c.path === "string" ? c.path : null;
    const isHtml = Boolean(path?.endsWith(".html"));
    const source = !isHtml ? assetUrl(documentId, path) : null;
    return wrap(
      <figure className="rounded-[8px] border border-line p-2">
        <div className="relative">
          {source ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={source} alt={typeof c.alt === "string" ? c.alt : ""} className="mx-auto max-h-[320px] w-auto max-w-full" />
          ) : (
            <p className="py-6 text-center text-[12px] text-ink-400">{isHtml ? "Interactive visual: view it in the editor." : "Image not available."}</p>
          )}
          {c.generated ? (
            <span className="absolute left-1 top-1 rounded bg-slate-900/75 px-1.5 py-0.5 text-[10px] font-medium text-white">
              AI-generated: verify against the text
            </span>
          ) : null}
        </div>
        {typeof c.caption === "string" && c.caption ? <figcaption className="mt-1.5 text-center text-[12px] text-ink-500">{c.caption}</figcaption> : null}
      </figure>,
    );
  }

  if (block.type === "code" || block.type === "code_cell") {
    return wrap(
      <pre className="overflow-x-auto rounded-[8px] border border-line bg-[#f8f9fa] px-3 py-2 font-mono text-[12px] leading-5 text-[#202124]">
        {typeof c.code === "string" ? c.code : ""}
      </pre>,
    );
  }

  if (block.type === "table") {
    const columns = Array.isArray(c.columns) ? (c.columns as unknown[]).map(String) : [];
    const rows = Array.isArray(c.rows) ? (c.rows as { cells?: unknown[] }[]) : [];
    return wrap(
      <table className="w-full border-collapse text-[12.5px]">
        {columns.length ? (
          <thead>
            <tr>{columns.map((col) => <th key={col} className="border border-line bg-canvas px-2 py-1 text-left font-medium">{col}</th>)}</tr>
          </thead>
        ) : null}
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>{(row.cells ?? []).map((cell, j) => <td key={j} className="border border-line px-2 py-1">{String(cell)}</td>)}</tr>
          ))}
        </tbody>
      </table>,
    );
  }

  if (block.type === "divider") return null;

  const title = typeof c.title === "string" ? c.title : "";
  const lines = asLines({ ...c, title: undefined });
  if (lines.length === 0 && !title) return null;
  return wrap(
    <div className="rounded-[8px] border border-line px-3.5 py-2.5 text-[12.5px] text-ink-700">
      <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-500">{title || block.type.replaceAll("_", " ")}</p>
      <ul className="list-disc space-y-0.5 pl-4">
        {lines.slice(0, 40).map((line, i) => <li key={i}>{line}</li>)}
      </ul>
    </div>,
  );
}

// --- the side panel ----------------------------------------------------------------

function BriefCard({ brief }: { brief: ReviewBrief }) {
  const blocked = !brief.ready_to_approve;
  return (
    <section className={cn("rounded-[10px] border px-3.5 py-3 text-[12.5px]", blocked ? "border-red-200 bg-red-50" : "border-emerald-200 bg-emerald-50")}>
      <p className={cn("flex items-start gap-2 font-medium", blocked ? "text-red-800" : "text-emerald-800")}>
        {blocked ? <ShieldAlert size={15} className="mt-0.5 shrink-0" /> : <CheckCircle2 size={15} className="mt-0.5 shrink-0" />}
        {brief.headline}
      </p>
      {brief.advisories.length > 0 ? (
        <ul className="mt-2 list-disc space-y-1 pl-5 text-[12px] text-ink-700">
          {brief.advisories.map((line) => <li key={line}>{line}</li>)}
        </ul>
      ) : null}
      {brief.chapters.some((c) => c.high + c.medium + c.low > 0) ? (
        <table className="mt-2.5 w-full text-[11.5px] text-ink-600">
          <thead>
            <tr className="text-left text-ink-400"><th className="font-medium">Chapter</th><th className="px-1 font-medium">High</th><th className="px-1 font-medium">Med</th><th className="px-1 font-medium">Low</th></tr>
          </thead>
          <tbody>
            {brief.chapters.filter((c) => c.high + c.medium + c.low > 0).map((c) => (
              <tr key={c.chapter}><td className="truncate pr-2">{c.chapter}</td><td className="px-1">{c.high}</td><td className="px-1">{c.medium}</td><td className="px-1">{c.low}</td></tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}

function FindingCard({
  finding,
  busy,
  onDecide,
  onRef,
}: {
  finding: AuditFinding;
  busy: boolean;
  onDecide: (finding: AuditFinding, status: FindingStatus) => void;
  onRef: (ref: string) => void;
}) {
  const decided = finding.status !== "open";
  return (
    <section className="rounded-[10px] border border-line px-3.5 py-3 text-[12.5px]">
      <p className="mb-1 flex flex-wrap items-center gap-2 text-[11px]">
        <span className={cn("rounded px-1.5 py-0.5 font-semibold uppercase", finding.severity === "high" ? "bg-red-50 text-red-700" : finding.severity === "medium" ? "bg-amber-50 text-amber-800" : "bg-slate-100 text-slate-600")}>
          {finding.severity}
        </span>
        <span className="font-medium text-ink">{KIND_LABEL[finding.kind]}</span>
        {finding.verified_by === "ai" ? <span className="text-ink-400">AI-checked</span> : null}
        {decided ? <span className="font-medium text-ink-500">{finding.status === "accepted" ? "Accepted" : "Dismissed"}</span> : null}
      </p>
      <p className="text-ink">“{finding.sentence}”</p>
      {finding.explanation ? <p className="mt-1 text-ink-500">{finding.explanation}</p> : null}
      {finding.evidence.map((item) => (
        <button key={item.ref} type="button" onClick={() => onRef(item.ref)} className="mt-2 block w-full border-l-2 border-brand-300 pl-2.5 text-left text-[12px] text-ink-600 hover:bg-canvas">
          {item.excerpt}
          <span className="block text-[11px] text-ink-400">[{item.ref}] {item.filename}{item.page ? `, page ${item.page}` : ""}: open the passage</span>
        </button>
      ))}
      <div className="mt-3 flex gap-2">
        {decided ? (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => onDecide(finding, "open")}>Reopen</Button>
        ) : (
          <>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide(finding, "accepted")}><Check size={13} /> Accept</Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide(finding, "dismissed")}><X size={13} /> Dismiss</Button>
          </>
        )}
      </div>
      <p className="mt-1.5 text-[11px] text-ink-400">Accept: the statement is fine. Dismiss: the flag was a false alarm.</p>
    </section>
  );
}

function SourceViewer({ courseId, sources, activeRef }: { courseId: string; sources: CourseSourceInfo[]; activeRef: string | null }) {
  const [tab, setTab] = useState<number | null>(null);
  const [passages, setPassages] = useState<Record<number, Passage[]>>({});
  const [pinned, setPinned] = useState<Passage | null>(null);
  const [loading, setLoading] = useState(false);
  const pinnedBox = useRef<HTMLDivElement>(null);

  const number = tab ?? sources[0]?.number ?? null;

  // A reference picked in the course (or from a finding) is shown in full.
  useEffect(() => {
    if (!activeRef) return;
    let live = true;
    const wanted = sourceNumber(activeRef);
    if (wanted !== null) setTab(wanted);
    getPassagesByRefs(courseId, [activeRef])
      .then((found) => {
        if (!live) return;
        setPinned(found[0] ?? null);
        pinnedBox.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      })
      .catch(() => live && setPinned(null));
    return () => {
      live = false;
    };
  }, [activeRef, courseId]);

  const loadMore = useCallback(
    async (n: number) => {
      setLoading(true);
      try {
        const have = passages[n]?.length ?? 0;
        const more = await getSourcePassages(courseId, n, have, 40);
        setPassages((current) => ({ ...current, [n]: [...(current[n] ?? []), ...more] }));
      } catch {
        // Shown as an empty list; the reviewer can retry by switching tabs.
      } finally {
        setLoading(false);
      }
    },
    [courseId, passages],
  );

  useEffect(() => {
    if (number !== null && passages[number] === undefined) void loadMore(number);
  }, [number, passages, loadMore]);

  if (sources.length === 0) {
    return <p className="text-[12px] text-ink-400">This course was not written from source documents.</p>;
  }
  const current = sources.find((s) => s.number === number);
  const shown = number !== null ? passages[number] ?? [] : [];

  return (
    <section>
      <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-ink-500">Source documents</h3>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {sources.map((source) => (
          <button
            key={source.source_id}
            type="button"
            onClick={() => setTab(source.number)}
            className={cn("rounded-[6px] border px-2 py-1 text-[11.5px]", source.number === number ? "border-brand-400 bg-brand-50 text-brand-700" : "border-line text-ink-600")}
          >
            {source.prefix} · {source.filename}
          </button>
        ))}
      </div>

      {pinned ? (
        <div ref={pinnedBox} className="mb-3 rounded-[8px] border-2 border-brand-300 bg-brand-50/40 px-3 py-2.5 text-[12.5px]">
          <p className="mb-1 text-[11px] font-semibold text-brand-700">
            Cited passage [{pinned.ref}] · {pinned.filename}{pinned.page ? `, page ${pinned.page}` : ""}{pinned.heading ? ` · ${pinned.heading}` : ""}
          </p>
          <p className="whitespace-pre-wrap text-ink-800">{pinned.text}</p>
        </div>
      ) : null}

      <ul className="space-y-2">
        {shown.map((passage) => (
          <li key={passage.ref} className={cn("rounded-[8px] border px-3 py-2 text-[12px] text-ink-700", passage.ref === pinned?.ref ? "border-brand-300" : "border-line")}>
            <p className="mb-0.5 text-[11px] text-ink-400">[{passage.ref}]{passage.page ? ` · page ${passage.page}` : ""}{passage.heading ? ` · ${passage.heading}` : ""}</p>
            <p className="whitespace-pre-wrap">{passage.text}</p>
          </li>
        ))}
      </ul>
      {current && shown.length < current.chunk_count ? (
        <Button variant="outline" size="sm" className="mt-2" disabled={loading} onClick={() => number !== null && void loadMore(number)}>
          {loading ? <Loader2 size={13} className="animate-spin" /> : null}
          Load more passages
        </Button>
      ) : null}
    </section>
  );
}
