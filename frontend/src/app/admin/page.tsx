"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  getAdminDashboard,
  listAdminCourses,
  type AdminCourse,
  type AdminDashboardResponse,
} from "@/lib/api/admin";
import {
  AdminAlert,
  AdminEmpty,
  AdminLoading,
  AdminPageHeader,
  AdminPanel,
  AdminStatusBadge,
  AnimatedNumber,
} from "@/components/admin/AdminUI";

type Metric = {
  label: string;
  value: number | string;
  note: string;
  icon: typeof Users;
  tone: "brand" | "green" | "amber" | "neutral";
};

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(new Date(value));
}

function formatStatus(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusTone(status: string): "neutral" | "success" | "warning" | "danger" {
  if (status === "ready") return "success";
  if (status === "failed") return "danger";
  if (["researching", "writing", "reviewing", "assembling", "illustrating"].includes(status)) return "warning";
  return "neutral";
}

function MetricCard({ metric }: { metric: Metric }) {
  const Icon = metric.icon;
  const iconClass = {
    brand: "bg-brand-50 text-brand-500",
    green: "bg-[#F3FAF5] text-[#3F8F68]",
    amber: "bg-[#FFF8EA] text-[#9A6A22]",
    neutral: "bg-canvas text-ink-500",
  }[metric.tone];

  return (
    <AdminPanel className="admin-panel-hover p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[12px] font-medium text-ink-500">{metric.label}</p>
          <p className="mt-2 text-[28px] font-semibold tracking-[-0.03em] text-ink">
            {typeof metric.value === "number" ? <AnimatedNumber value={metric.value} /> : metric.value}
          </p>
        </div>
        <span className={`flex h-9 w-9 items-center justify-center rounded-[10px] ${iconClass}`}><Icon size={18} /></span>
      </div>
      <p className="mt-3 text-[11px] leading-4 text-ink-400">{metric.note}</p>
    </AdminPanel>
  );
}

function CourseTable({ courses, isLoading }: { courses: AdminCourse[]; isLoading: boolean }) {
  if (isLoading) return <AdminLoading label="Loading recent courses" />;
  if (courses.length === 0) return <AdminEmpty><BookOpen size={24} className="text-brand-300" /><span>No courses have been created yet.</span></AdminEmpty>;

  return (
    <div className="overflow-x-auto">
      <table className="admin-table w-full min-w-[760px] text-left text-[12px]">
        <thead className="border-b border-line bg-canvas uppercase tracking-[0.08em]"><tr><th className="px-4 py-3">Course</th><th className="px-4 py-3">Owner</th><th className="px-4 py-3">Status</th><th className="px-4 py-3">Updated</th><th className="px-4 py-3 text-right">Action</th></tr></thead>
        <tbody className="divide-y divide-line">
          {courses.map((course) => (
            <tr key={course.id}>
              <td className="max-w-[250px] px-4 py-3.5"><p className="truncate font-semibold text-ink">{course.title}</p><p className="mt-1 truncate font-mono text-[10px] text-ink-400">{course.course_id}</p></td>
              <td className="max-w-[170px] truncate px-4 py-3.5 text-ink-500">{course.owner ?? "Not tracked"}</td>
              <td className="px-4 py-3.5"><AdminStatusBadge tone={statusTone(course.status)}>{formatStatus(course.status)}</AdminStatusBadge></td>
              <td className="whitespace-nowrap px-4 py-3.5 text-ink-500">{formatDate(course.updated_at)}</td>
              <td className="px-4 py-3.5 text-right"><Link href="/admin/courses" className="inline-flex items-center gap-1 rounded-[7px] border border-line px-2.5 py-1.5 font-medium text-ink-500 transition-colors hover:border-brand-200 hover:bg-brand-50 hover:text-brand-700">Details <ChevronRight size={13} /></Link></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QuickAction({ href, icon: Icon, label, detail }: { href: string; icon: typeof Users; label: string; detail: string }) {
  return <Link href={href} className="group flex items-center gap-3 rounded-[9px] border border-line px-3 py-3 transition-colors duration-200 hover:border-brand-200 hover:bg-brand-50"><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] bg-brand-50 text-brand-500"><Icon size={16} /></span><span className="min-w-0 flex-1"><span className="block text-[12px] font-semibold text-ink">{label}</span><span className="mt-0.5 block truncate text-[11px] text-ink-400">{detail}</span></span><ArrowRight size={14} className="text-ink-300 transition-transform group-hover:translate-x-0.5 group-hover:text-brand-500" /></Link>;
}

export default function AdminDashboardPage() {
  const [dashboard, setDashboard] = useState<AdminDashboardResponse | null>(null);
  const [courses, setCourses] = useState<AdminCourse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [coursesError, setCoursesError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [areCoursesLoading, setAreCoursesLoading] = useState(true);

  useEffect(() => {
    let active = true;
    getAdminDashboard().then((data) => { if (active) { setDashboard(data); setError(null); } }).catch((caught) => { if (active) setError(caught instanceof ApiError ? caught.message : "Dashboard could not be loaded."); }).finally(() => { if (active) setIsLoading(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    listAdminCourses({ limit: 5, offset: 0 }).then((response) => { if (active) { setCourses(response.courses); setCoursesError(null); } }).catch((caught) => { if (active) setCoursesError(caught instanceof ApiError ? caught.message : "Recent courses could not be loaded."); }).finally(() => { if (active) setAreCoursesLoading(false); });
    return () => { active = false; };
  }, []);

  const metrics: Metric[] = dashboard ? [
    { label: "Total courses", value: dashboard.total_courses, note: "Courses stored in the platform", icon: BookOpen, tone: "brand" },
    { label: "Total users", value: dashboard.total_users, note: "Registered platform accounts", icon: Users, tone: "brand" },
    { label: "Pending review", value: "—", note: "Not exposed by the admin summary API", icon: ClipboardList, tone: "amber" },
    { label: "Published", value: "—", note: "Not exposed by the admin summary API", icon: CheckCircle2, tone: "green" },
  ] : [];

  return (
    <div className="admin-page mx-auto flex w-full max-w-[1400px] flex-col gap-5">
      <AdminPageHeader eyebrow="Admin" title="Welcome back!" description="Here's an overview of your course platform." />
      {isLoading ? <AdminPanel><AdminLoading label="Loading dashboard" /></AdminPanel> : null}
      {error ? <AdminAlert>{error}</AdminAlert> : null}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{metrics.map((metric) => <MetricCard key={metric.label} metric={metric} />)}</section>

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_292px]">
        <AdminPanel>
          <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-5 py-5 sm:px-6"><div><div className="flex items-center gap-2 text-brand-500"><BookOpen size={18} /><p className="text-[18px] font-semibold text-ink">Course Management</p></div><p className="mt-1 max-w-xl text-[12px] text-ink-500">Inspect generated courses, filter processing states, and review stored course metadata.</p></div><Link href="/" className="inline-flex h-9 items-center gap-1.5 rounded-[8px] bg-brand-500 px-3.5 text-[12px] font-semibold text-white shadow-[0_1px_2px_rgba(92,48,35,0.2)] transition-colors hover:bg-brand-700"><Plus size={15} /> New course</Link></div>
          <div className="flex flex-wrap items-center gap-2 border-b border-line bg-[#FFFCFA] px-5 py-3 sm:px-6"><div className="relative min-w-[210px] flex-1"><Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400" /><input aria-label="Search dashboard courses" className="h-9 w-full rounded-[8px] border border-line bg-white pl-9 pr-3 text-[12px] text-ink outline-none placeholder:text-ink-400 focus:border-brand-400" placeholder="Search title, course ID, document ID, owner..." /></div><Link href="/admin/courses" className="inline-flex h-9 items-center gap-1.5 rounded-[8px] border border-line bg-white px-3 text-[12px] font-medium text-ink-500 transition-colors hover:border-brand-200 hover:bg-brand-50 hover:text-ink">All filters <ArrowRight size={13} /></Link></div>
          {coursesError ? <div className="p-5"><AdminAlert>{coursesError}</AdminAlert></div> : <CourseTable courses={courses} isLoading={areCoursesLoading} />}
          <div className="flex items-center justify-between border-t border-line px-5 py-3.5 text-[11px] text-ink-400 sm:px-6"><span>{courses.length ? `Showing ${courses.length} most recently updated courses` : "Course records"}</span><Link href="/admin/courses" className="inline-flex items-center gap-1 font-semibold text-brand-500 hover:text-brand-700">View all courses <ArrowRight size={13} /></Link></div>
        </AdminPanel>

        <aside className="flex flex-col gap-5">
          <AdminPanel className="p-4"><div className="flex items-center justify-between"><div className="flex items-center gap-2"><span className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-50 text-brand-500"><Sparkles size={16} /></span><h3 className="text-[14px] font-semibold text-ink">Recent activity</h3></div><span className="text-[11px] text-ink-400">Live log</span></div><div className="mt-4 rounded-[9px] border border-dashed border-line bg-canvas px-4 py-5 text-center"><ClipboardList size={20} className="mx-auto text-ink-300" /><p className="mt-2 text-[12px] font-medium text-ink">No global activity feed</p><p className="mt-1 text-[11px] leading-4 text-ink-400">Activity history is available inside each course editor.</p><Link href="/admin/courses" className="mt-3 inline-flex items-center gap-1 text-[11px] font-semibold text-brand-500">Open courses <ArrowRight size={12} /></Link></div></AdminPanel>
          <AdminPanel className="p-4"><div className="mb-3 flex items-center gap-2"><span className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-50 text-brand-500"><Sparkles size={16} /></span><h3 className="text-[14px] font-semibold text-ink">Quick actions</h3></div><div className="space-y-2"><QuickAction href="/admin/users" icon={Users} label="Manage users" detail="Review accounts and roles" /><QuickAction href="/admin/courses" icon={ClipboardList} label="Review courses" detail="Filter and inspect records" /><QuickAction href="/" icon={Plus} label="Create a course" detail="Start a new course flow" /></div></AdminPanel>
          <AdminPanel className="relative overflow-hidden bg-[#FFFCFA] p-4"><div className="absolute -right-6 -top-8 h-24 w-24 rounded-full bg-brand-50" /><div className="relative"><ShieldCheck size={19} className="text-brand-500" /><h3 className="mt-3 text-[14px] font-semibold text-ink">Need help?</h3><p className="mt-1 text-[11px] leading-4 text-ink-500">Check the documentation or contact support.</p><button type="button" className="mt-3 inline-flex items-center gap-1 text-[11px] font-semibold text-brand-500">Go to Help Center <ArrowRight size={12} /></button></div></AdminPanel>
        </aside>
      </div>
    </div>
  );
}
