"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  BookOpen as BookOpenEmpty,
  ChevronLeft,
  ChevronRight,
  Eye,
  FileText,
  Loader2,
  RefreshCcw,
  Search,
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
  listAdminCourses,
  type AdminCourse,
  type AdminCourseListOptions,
  type AdminCourseListResponse,
} from "@/lib/api/admin";

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

function StatusBadge({ status }: { status: string }) {
  const ready = status === "ready";
  const failed = status === "failed";
  const active = ["researching", "writing", "reviewing", "assembling", "illustrating"].includes(status);
  return <AdminStatusBadge tone={failed ? "danger" : ready ? "success" : active ? "warning" : "neutral"}>{status}</AdminStatusBadge>;
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

export default function AdminCoursesPage() {
  const [page, setPage] = useState(0);
  const [searchInput, setSearchInput] = useState("");
  const [ownerInput, setOwnerInput] = useState("");
  const [search, setSearch] = useState("");
  const [owner, setOwner] = useState("");
  const [status, setStatus] = useState("all");
  const [data, setData] = useState<AdminCourseListResponse | null>(null);
  const [selectedCourse, setSelectedCourse] = useState<AdminCourse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detailsError, setDetailsError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadingDetailsId, setLoadingDetailsId] = useState<string | null>(null);

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
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    };
  }, [owner, page, search, status]);

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
    try {
      const detail = await getAdminCourse(target.id);
      setSelectedCourse(detail);
    } catch (caught) {
      setDetailsError(caught instanceof ApiError ? caught.message : "Course details could not be loaded.");
    } finally {
      setLoadingDetailsId(null);
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
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#8E8780]"
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
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#8E8780]"
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
          <div className="flex items-center gap-2">
            <Button
              type="submit"
              variant="ghost"
              size="sm"
              className="h-9 border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
              title="Apply filters"
            >
              <Search size={14} />
              Search
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={clearFilters}
              className="h-9 border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
              title="Reset filters"
            >
              <RefreshCcw size={14} />
              Reset
            </Button>
          </div>
        </form>
      </AdminPanel>

      <AdminPanel>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#302D29] px-4 py-3">
          <div className="text-[13px] text-[#BEB6AD]">
            {total.toLocaleString()} course{total === 1 ? "" : "s"}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canGoBack || isLoading}
              onClick={() => setPage((value) => Math.max(0, value - 1))}
              className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
              title="Previous page"
            >
              <ChevronLeft size={14} />
            </Button>
            <span className="text-[12px] text-[#8E8780]">Page {page + 1}</span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canGoForward || isLoading}
              onClick={() => setPage((value) => value + 1)}
              className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
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
            <table className="admin-table w-full min-w-[1180px] text-left text-[13px]">
              <thead className="bg-[#181614] text-[12px] uppercase tracking-[0.08em] text-[#8E8780]">
                <tr>
                  <th className="px-4 py-3 font-medium">Course</th>
                  <th className="px-4 py-3 font-medium">Course ID</th>
                  <th className="px-4 py-3 font-medium">Owner</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Document</th>
                  <th className="px-4 py-3 font-medium">Created</th>
                  <th className="px-4 py-3 font-medium">Updated</th>
                  <th className="px-4 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#302D29]">
                {courses.map((item) => (
                  <tr key={item.id} className="text-[#D8D0C7]">
                    <td className="max-w-[280px] px-4 py-3 font-medium text-[#F4EFE7]">
                      <span className="block truncate">{item.title}</span>
                      <span className="mt-1 block text-[12px] font-normal text-[#8E8780]">
                        {item.template_id} / {item.toc_count} chapter{item.toc_count === 1 ? "" : "s"}
                      </span>
                    </td>
                    <td className="max-w-[190px] truncate px-4 py-3 font-mono text-[12px] text-[#A9A29A]">
                      {item.course_id}
                    </td>
                    <td className="max-w-[220px] truncate px-4 py-3 text-[#A9A29A]">
                      {item.owner ?? "Not tracked"}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="max-w-[170px] truncate px-4 py-3 font-mono text-[12px] text-[#A9A29A]">
                      {item.document_id}
                    </td>
                    <td className="px-4 py-3 text-[#A9A29A]">{formatDate(item.created_at)}</td>
                    <td className="px-4 py-3 text-[#A9A29A]">{formatDate(item.updated_at)}</td>
                    <td className="px-4 py-3">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => viewDetails(item)}
                        className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                        title="View details"
                      >
                        {loadingDetailsId === item.id ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : (
                          <Eye size={14} />
                        )}
                        Details
                      </Button>
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
              <p className="text-[12px] font-semibold uppercase tracking-[0.14em] text-[#D88445]">
                Course Details
              </p>
              <h3 className="mt-2 text-[18px] font-semibold text-[#F7EEE2]">{selectedCourse.title}</h3>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setSelectedCourse(null)}
              className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
            >
              Close
            </Button>
          </div>
          {detailsError ? (
            <div className="mt-4 flex items-center gap-2 rounded-[8px] border border-[#5A3529] bg-[#2A1D19] p-3 text-sm text-[#F0C4A6]">
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
        </AdminPanel>
      ) : null}
    </div>
  );
}
