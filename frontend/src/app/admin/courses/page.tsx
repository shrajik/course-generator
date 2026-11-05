"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  BookOpen as BookOpenEmpty,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Eye,
  FileText,
  Loader2,
  RefreshCcw,
  Search,
  ShieldCheck,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import {
  AdminAlert,
  AdminEmpty,
  AdminLoading,
  AdminPageHeader,
  AdminPanel,
  AdminStatusBadge,
} from "@/components/admin/AdminUI";
import {
  getAdminCourse,
  getAdminCourseModelUsage,
  listAdminCourses,
  type AdminCourse,
  type AdminCourseListOptions,
  type AdminCourseListResponse,
  type AdminModelUsageResponse,
} from "@/lib/api/admin";
import { summarizeModelUsage } from "@/lib/admin/modelUsage";
import { createRequestGuard } from "@/lib/admin/requestGuard";

const PAGE_SIZE = 25;
const KNOWN_STATUSES = [
  "created",
  "planned",
  "researching",
  "writing",
  "reviewing",
  "assembling",
  "illustrating",
  "ready",
  "failed",
];
const selectClass =
  "h-10 rounded-[8px] border border-line bg-white px-3 text-[13px] text-ink outline-none transition-colors focus:border-brand-500";
const inputClass =
  "h-10 w-full rounded-[8px] border border-line bg-white px-3 pl-9 text-[13px] text-ink outline-none placeholder:text-ink-400 transition-colors focus:border-brand-500";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

/** "Nov 5, 2026": a table cell should not wrap; the full date and time is in the tooltip and the details. */
function formatDay(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value));
}

function StatusBadge({ status }: { status: string }) {
  const ready = status === "ready";
  const failed = status === "failed";
  const active = ["researching", "writing", "reviewing", "assembling", "illustrating"].includes(status);
  return <AdminStatusBadge tone={failed ? "danger" : ready ? "success" : active ? "warning" : "neutral"}>{status}</AdminStatusBadge>;
}

/** Separate from StatusBadge on purpose - a quality-gate finding never
 * changes `status` (see CourseRecord.quality_snapshot), so it gets its own,
 * clearly distinct indicator rather than relabeling the generation status. */
function QualityOutcomeBadge({ outcome }: { outcome: "passed" | "failed" | null }) {
  if (outcome !== "failed") return null;
  return <AdminStatusBadge tone="warning">Quality attention</AdminStatusBadge>;
}

/** Expert-review state. Nothing is shown for a course that was never submitted. */
function ReviewBadge({ state }: { state: string }) {
  if (state === "in_review") return <AdminStatusBadge tone="warning">Awaiting review</AdminStatusBadge>;
  if (state === "changes_requested") return <AdminStatusBadge tone="danger">Changes requested</AdminStatusBadge>;
  if (state === "approved") return <AdminStatusBadge tone="success">Approved</AdminStatusBadge>;
  return null;
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid gap-1 border-b border-line py-3 last:border-0 sm:grid-cols-[160px_1fr] sm:gap-4">
      <dt className="text-[12px] uppercase tracking-[0.08em] text-ink-400">{label}</dt>
      <dd className="break-words text-[13px] text-ink">{value}</dd>
    </div>
  );
}

function yesNo(value: boolean): string {
  return value ? "Yes" : "No";
}

/** The per-call table and summary line for a loaded, non-empty model-usage
 * response - split out from the page component purely to keep the JSX
 * readable; still fetched/owned by the page above it. */
function ModelUsageDetail({ usage }: { usage: AdminModelUsageResponse }) {
  const summary = summarizeModelUsage(usage.calls);
  return (
    <>
      <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-ink">
        <span>
          {summary.totalCalls} call{summary.totalCalls === 1 ? "" : "s"}
        </span>
        <span>{summary.totalInputTokens.toLocaleString()} input tokens</span>
        <span>{summary.totalOutputTokens.toLocaleString()} output tokens</span>
        <span className={summary.failedCount > 0 ? "text-ink-500" : undefined}>
          {summary.failedCount} failed
        </span>
        <span>
          {summary.fallbackCount} fallback{summary.fallbackCount === 1 ? "" : "s"}
        </span>
      </div>
      <div className="overflow-x-auto rounded-[8px] border border-line">
        <table className="w-full min-w-[640px] text-left text-[12px]">
          <thead className="bg-surface-muted text-[10px] uppercase tracking-[0.08em] text-ink-400">
            <tr>
              <th className="px-3 py-2 font-medium">Purpose</th>
              <th className="px-3 py-2 font-medium">Kind</th>
              <th className="px-3 py-2 font-medium">Model</th>
              <th className="px-3 py-2 font-medium">Provider</th>
              <th className="px-3 py-2 font-medium text-right">In</th>
              <th className="px-3 py-2 font-medium text-right">Out</th>
              <th className="px-3 py-2 font-medium text-right">Sec</th>
              <th className="px-3 py-2 font-medium text-right">Retries</th>
              <th className="px-3 py-2 font-medium">Flags</th>
              <th className="px-3 py-2 font-medium">Timestamp</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {usage.calls.map((call, index) => (
              <tr key={`${call.purpose}-${call.timestamp}-${index}`} className="text-ink">
                <td className="max-w-[220px] truncate px-3 py-2 font-mono text-[11px]" title={call.purpose}>
                  {call.purpose}
                </td>
                <td className="px-3 py-2">{call.kind}</td>
                <td className="px-3 py-2">{call.model}</td>
                <td className="px-3 py-2">{call.provider || "—"}</td>
                <td className="px-3 py-2 text-right">{call.input_tokens.toLocaleString()}</td>
                <td className="px-3 py-2 text-right">{call.output_tokens.toLocaleString()}</td>
                <td className="px-3 py-2 text-right">{call.seconds.toFixed(1)}</td>
                <td className="px-3 py-2 text-right">{call.retries}</td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {call.failed ? <AdminStatusBadge tone="danger">Failed</AdminStatusBadge> : null}
                    {call.used_fallback ? (
                      <AdminStatusBadge tone="warning">Fallback</AdminStatusBadge>
                    ) : null}
                  </div>
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-[11px] text-ink-400">
                  {call.timestamp ? formatDate(call.timestamp) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function AdminCoursesPage() {
  const [page, setPage] = useState(0);
  const [searchInput, setSearchInput] = useState("");
  const [ownerInput, setOwnerInput] = useState("");
  const [search, setSearch] = useState("");
  const [owner, setOwner] = useState("");
  const [status, setStatus] = useState("all");
  const [reviewStatus, setReviewStatus] = useState("all");
  const [data, setData] = useState<AdminCourseListResponse | null>(null);
  const [selectedCourse, setSelectedCourse] = useState<AdminCourse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detailsError, setDetailsError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadingDetailsId, setLoadingDetailsId] = useState<string | null>(null);
  const [modelUsageOpen, setModelUsageOpen] = useState(false);
  const [modelUsage, setModelUsage] = useState<AdminModelUsageResponse | null>(null);
  const [modelUsageLoading, setModelUsageLoading] = useState(false);
  const [modelUsageError, setModelUsageError] = useState<string | null>(null);
  // Stable across renders (created once) - guards against a stale
  // model-usage response (e.g. Course A's fetch resolving after the admin
  // has already switched to Course B) overwriting the wrong course's state.
  const modelUsageGuard = useRef(createRequestGuard()).current;

  const statusOptions = useMemo(
    () => Array.from(new Set([...(data?.statuses ?? []), ...KNOWN_STATUSES])).sort(),
    [data?.statuses],
  );

  const queryOptions = useCallback((): AdminCourseListOptions => {
    const trimmedSearch = search.trim();
    const trimmedOwner = owner.trim();
    return {
      search: trimmedSearch || undefined,
      owner: trimmedOwner || undefined,
      status: status === "all" ? undefined : status,
      review_status: reviewStatus === "all" ? undefined : reviewStatus,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    };
  }, [owner, page, search, status, reviewStatus]);

  const loadCourses = useCallback(() => {
    let active = true;
    setIsLoading(true);
    listAdminCourses(queryOptions())
      .then((response) => {
        if (!active) return;
        setData(response);
        setError(null);
      })
      .catch((caught) => {
        if (!active) return;
        setError(caught instanceof ApiError ? caught.message : "Courses could not be loaded.");
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [queryOptions]);

  useEffect(() => loadCourses(), [loadCourses]);

  const viewDetails = async (target: AdminCourse) => {
    setSelectedCourse(target);
    setDetailsError(null);
    setLoadingDetailsId(target.id);
    // Switching courses - any previously loaded usage data belongs to the
    // last course, not this one, so it must not linger on screen. Also
    // invalidate any in-flight model-usage fetch for the PREVIOUS course,
    // even if the admin never re-expands Model Usage for this one - without
    // this, a slow fetch for the old course could still land later and
    // silently overwrite this course's (closed) usage state.
    setModelUsageOpen(false);
    setModelUsage(null);
    setModelUsageError(null);
    modelUsageGuard.invalidate();
    try {
      const detail = await getAdminCourse(target.id);
      setSelectedCourse(detail);
    } catch (caught) {
      setDetailsError(caught instanceof ApiError ? caught.message : "Course details could not be loaded.");
    } finally {
      setLoadingDetailsId(null);
    }
  };

  const toggleModelUsage = async () => {
    if (!selectedCourse) return;
    const next = !modelUsageOpen;
    setModelUsageOpen(next);
    // Lazy: only fetch the first time it's expanded, not on every toggle
    // and not when the detail panel itself first opens - this is extra
    // data most admins won't need on every single course they glance at.
    if (next && modelUsage === null && !modelUsageLoading) {
      // A fresh id for THIS fetch - issued before the await, so a later
      // fetch (a different course, or a retry) can supersede it. Every
      // state update below only applies if this id is still the most
      // recent one issued - a stale response (e.g. this course's own fetch
      // resolving after the admin has already switched away, or a second
      // overlapping fetch for the same course) is simply dropped.
      const requestId = modelUsageGuard.start();
      setModelUsageLoading(true);
      setModelUsageError(null);
      try {
        // The course's own course_id (e.g. "crs_abc123"), NOT its database
        // id (UUID) - this one admin endpoint's path parameter is the
        // string course_id, unlike every other /admin/courses/{id} route.
        const usage = await getAdminCourseModelUsage(selectedCourse.course_id);
        if (!modelUsageGuard.isCurrent(requestId)) return;
        setModelUsage(usage);
      } catch (caught) {
        if (!modelUsageGuard.isCurrent(requestId)) return;
        setModelUsageError(
          caught instanceof ApiError ? caught.message : "Model usage could not be loaded.",
        );
      } finally {
        if (modelUsageGuard.isCurrent(requestId)) setModelUsageLoading(false);
      }
    }
  };

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPage(0);
    setSearch(searchInput);
    setOwner(ownerInput);
  };

  const clearFilters = () => {
    setSearchInput("");
    setOwnerInput("");
    setSearch("");
    setOwner("");
    setStatus("all");
    setReviewStatus("all");
    setPage(0);
  };

  const courses = data?.courses ?? [];
  const total = data?.total ?? 0;
  const canGoBack = page > 0;
  const canGoForward = (page + 1) * PAGE_SIZE < total;

  return (
    <div className="admin-page mx-auto flex w-full max-w-[1240px] flex-col gap-5">
      <AdminPageHeader eyebrow="Course management" title="Courses" description="Inspect generated courses, filter processing states, and review stored course metadata." />

      {error ? <AdminAlert>{error}</AdminAlert> : null}

      <AdminPanel className="p-4">
        <form onSubmit={applyFilters} className="grid gap-3 lg:grid-cols-[minmax(240px,1fr)_220px_180px_auto]">
          <div className="relative">
            <Search
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
            />
            <input
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              className={inputClass}
              placeholder="Search title, course ID, document ID, owner"
            />
          </div>
          <div className="relative">
            <FileText
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
            />
            <input
              value={ownerInput}
              onChange={(event) => setOwnerInput(event.target.value)}
              className={inputClass}
              placeholder="Filter owner"
            />
          </div>
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setPage(0);
            }}
            className={selectClass}
            aria-label="Status filter"
          >
            <option value="all">All statuses</option>
            {statusOptions.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <select
            value={reviewStatus}
            onChange={(event) => {
              setReviewStatus(event.target.value);
              setPage(0);
            }}
            className={selectClass}
            aria-label="Review filter"
          >
            <option value="all">Any review state</option>
            <option value="in_review">Awaiting review</option>
            <option value="changes_requested">Changes requested</option>
            <option value="approved">Approved</option>
            <option value="draft">Not submitted</option>
          </select>
          <div className="flex items-center gap-2">
            <Button
              type="submit"
              variant="outline"
              size="sm"
              className="h-9"
              title="Apply filters"
            >
              <Search size={14} />
              Search
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={clearFilters}
              className="h-9"
              title="Reset filters"
            >
              <RefreshCcw size={14} />
              Reset
            </Button>
          </div>
        </form>
      </AdminPanel>

      <AdminPanel>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="text-[13px] text-ink-500">
            {total.toLocaleString()} course{total === 1 ? "" : "s"}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!canGoBack || isLoading}
              onClick={() => setPage((value) => Math.max(0, value - 1))}
              
              title="Previous page"
            >
              <ChevronLeft size={14} />
            </Button>
            <span className="text-[12px] text-ink-400">Page {page + 1}</span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!canGoForward || isLoading}
              onClick={() => setPage((value) => value + 1)}
              
              title="Next page"
            >
              <ChevronRight size={14} />
            </Button>
          </div>
        </div>

        {isLoading ? (
          <AdminLoading label="Loading courses" />
        ) : courses.length === 0 ? (
          <AdminEmpty><BookOpenEmpty /><span>No courses match the current filters.</span></AdminEmpty>
        ) : (
          <div className="overflow-x-auto">
            <table className="admin-table w-full min-w-[860px] text-left text-[13px]">
              <thead className="bg-surface-muted text-[11px] uppercase tracking-[0.08em] text-ink-500">
                <tr>
                  <th scope="col" className="px-4 py-3 font-semibold">Course</th>
                  <th scope="col" className="px-4 py-3 font-semibold">Owner</th>
                  <th scope="col" className="px-4 py-3 font-semibold">Status</th>
                  <th scope="col" className="whitespace-nowrap px-4 py-3 font-semibold">Last updated</th>
                  <th scope="col" className="px-4 py-3 text-right font-semibold">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {courses.map((item) => (
                  <tr key={item.id} className="align-top text-ink">
                    <td className="max-w-[340px] px-4 py-3.5">
                      <span className="block truncate font-medium" title={item.title}>{item.title}</span>
                      <span className="mt-0.5 block truncate font-mono text-[11.5px] text-ink-400" title={item.course_id}>
                        {item.course_id}
                      </span>
                      <span className="mt-0.5 block text-[12px] text-ink-500">
                        {item.template_id} · {item.toc_count} chapter{item.toc_count === 1 ? "" : "s"}
                      </span>
                    </td>
                    <td className="max-w-[200px] truncate px-4 py-3.5 text-ink-500">{item.owner ?? "Not tracked"}</td>
                    <td className="px-4 py-3.5">
                      <div className="flex flex-col items-start gap-1.5">
                        <StatusBadge status={item.status} />
                        <ReviewBadge state={item.review_status} />
                        <QualityOutcomeBadge outcome={item.quality_outcome} />
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3.5 text-ink-500" title={formatDate(item.updated_at)}>
                      {formatDay(item.updated_at)}
                    </td>
                    <td className="px-4 py-3.5">
                      <div className="flex flex-col items-end gap-2">
                        {item.review_status === "in_review" ? (
                          <a
                            href={`/review/${item.document_id}`}
                            className="inline-flex h-8 items-center gap-1.5 rounded-[8px] bg-brand-600 px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-brand-700"
                          >
                            <ShieldCheck size={14} aria-hidden />
                            Review
                          </a>
                        ) : null}
                        <Button type="button" variant="outline" size="sm" onClick={() => viewDetails(item)} title="View details">
                          {loadingDetailsId === item.id ? (
                            <Loader2 size={14} className="animate-spin" aria-hidden />
                          ) : (
                            <Eye size={14} aria-hidden />
                          )}
                          Details
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </AdminPanel>

      {selectedCourse ? (
        <AdminPanel className="p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-[12px] font-semibold uppercase tracking-[0.14em] text-brand-600">
                Course Details
              </p>
              <h3 className="mt-2 text-[18px] font-semibold text-ink">{selectedCourse.title}</h3>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setSelectedCourse(null);
                setModelUsageOpen(false);
                setModelUsage(null);
                setModelUsageError(null);
                modelUsageGuard.invalidate();
              }}
              
            >
              Close
            </Button>
          </div>
          {detailsError ? (
            <div className="mt-4 flex items-center gap-2 rounded-[8px] border border-brand-200 bg-surface-clay p-3 text-sm text-ink-500">
              <AlertCircle size={16} />
              {detailsError}
            </div>
          ) : null}
          <dl className="mt-4">
            <DetailRow label="Database ID" value={<span className="font-mono text-[12px]">{selectedCourse.id}</span>} />
            <DetailRow label="Course ID" value={<span className="font-mono text-[12px]">{selectedCourse.course_id}</span>} />
            <DetailRow label="Document ID" value={<span className="font-mono text-[12px]">{selectedCourse.document_id}</span>} />
            <DetailRow label="Owner" value={selectedCourse.owner ?? "Not tracked"} />
            <DetailRow label="Status" value={<StatusBadge status={selectedCourse.status} />} />
            <DetailRow
              label="Quality outcome"
              value={
                selectedCourse.quality_outcome === "failed" ? (
                  <AdminStatusBadge tone="warning">Needs attention</AdminStatusBadge>
                ) : selectedCourse.quality_outcome === "passed" ? (
                  <AdminStatusBadge tone="success">Passed</AdminStatusBadge>
                ) : (
                  "Not evaluated yet"
                )
              }
            />
            <DetailRow label="Run state" value={selectedCourse.run_state ?? "Not running"} />
            <DetailRow label="Template" value={selectedCourse.template_id} />
            <DetailRow label="Target audience" value={selectedCourse.target_audience ?? "Not provided"} />
            <DetailRow label="Language" value={selectedCourse.language ?? "en"} />
            <DetailRow label="Tone" value={selectedCourse.tone || "Not provided"} />
            <DetailRow label="TOC chapters" value={selectedCourse.toc_count.toLocaleString()} />
            <DetailRow label="Progress chapters" value={selectedCourse.chapters_count.toLocaleString()} />
            <DetailRow label="Has blueprint" value={yesNo(selectedCourse.has_blueprint)} />
            <DetailRow label="Has document" value={yesNo(selectedCourse.has_document)} />
            <DetailRow label="Last error" value={selectedCourse.last_error ?? "None"} />
            <DetailRow
              label="Warnings"
              value={selectedCourse.warnings.length ? selectedCourse.warnings.join(", ") : "None"}
            />
            <DetailRow label="Created date" value={formatDate(selectedCourse.created_at)} />
            <DetailRow label="Updated date" value={formatDate(selectedCourse.updated_at)} />
          </dl>

          <div className="mt-5 border-t border-line pt-4">
            <button
              type="button"
              onClick={() => void toggleModelUsage()}
              className="flex w-full items-center justify-between text-left"
            >
              <span className="text-[12px] font-semibold uppercase tracking-[0.14em] text-brand-600">
                Model Usage
              </span>
              {modelUsageOpen ? (
                <ChevronUp size={16} className="text-ink" />
              ) : (
                <ChevronDown size={16} className="text-ink" />
              )}
            </button>

            {modelUsageOpen ? (
              <div className="mt-3">
                {modelUsageLoading ? (
                  <AdminLoading label="Loading model usage" />
                ) : modelUsageError ? (
                  <div className="flex items-center gap-2 rounded-[8px] border border-brand-200 bg-surface-clay p-3 text-sm text-ink-500">
                    <AlertCircle size={16} />
                    {modelUsageError}
                  </div>
                ) : modelUsage ? (
                  modelUsage.job_id === null ? (
                    <p className="text-[13px] text-ink">
                      No generation run recorded yet for this course.
                    </p>
                  ) : modelUsage.calls.length === 0 ? (
                    <p className="text-[13px] text-ink">
                      The latest run ({modelUsage.run_state ?? "unknown"}) made no real AI calls.
                    </p>
                  ) : (
                    <ModelUsageDetail usage={modelUsage} />
                  )
                ) : null}
              </div>
            ) : null}
          </div>
        </AdminPanel>
      ) : null}
    </div>
  );
}
