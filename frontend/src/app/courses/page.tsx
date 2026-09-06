"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowRight, BookOpen, FileText, Loader2, RefreshCcw } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { listCourses, type CourseListItem } from "@/lib/api/courses";
import { AdminAlert, AdminEmpty, AdminStatusBadge } from "@/components/admin/AdminUI";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value));
}

function statusTone(status: string): "neutral" | "success" | "warning" | "danger" {
  if (status === "ready") return "success";
  if (status === "failed") return "danger";
  if (["researching", "writing", "reviewing", "assembling", "illustrating"].includes(status)) return "warning";
  return "neutral";
}

function statusLabel(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function MyCoursesPage() {
  const [courses, setCourses] = useState<CourseListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    listCourses().then((response) => { if (active) setCourses(response.courses); }).catch((caught) => { if (active) setError(caught instanceof ApiError ? caught.message : "Courses could not be loaded."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  return <div className="creator-page mx-auto w-full max-w-[1120px]">
    <div className="creator-page-heading"><div><p className="creator-eyebrow">Workspace</p><h1 className="creator-page-title">My Courses</h1><p className="creator-page-description">Courses created in your workspace, with their latest generation status.</p></div><Link href="/" className="creator-primary-button"><BookOpen size={15} /> Create Course</Link></div>
    {error ? <AdminAlert>{error}</AdminAlert> : null}
    <section className="creator-content-panel mt-5">
      <div className="flex items-center justify-between border-b border-line px-5 py-4"><div><h2 className="text-[15px] font-semibold text-ink">Your course library</h2><p className="mt-1 text-[12px] text-ink-500">{courses.length} course{courses.length === 1 ? "" : "s"} available</p></div><RefreshCcw size={16} className="text-ink-400" /></div>
      {loading ? <div className="creator-loading"><Loader2 size={18} className="animate-spin text-brand-500" /> Loading courses</div> : courses.length === 0 ? <AdminEmpty><BookOpen size={24} className="text-brand-300" /><span>No courses yet. Start with your first course.</span><Link href="/" className="creator-text-link">Create a course <ArrowRight size={13} /></Link></AdminEmpty> : <div className="divide-y divide-line">{courses.map((course) => <CourseRow key={course.course_id} course={course} />)}</div>}
    </section>
  </div>;
}

function CourseRow({ course }: { course: CourseListItem }) {
  return <article className="creator-course-row"><div className="flex min-w-0 items-start gap-3"><span className="creator-list-icon"><FileText size={16} /></span><div className="min-w-0"><h3 className="truncate text-[14px] font-semibold text-ink">{course.course_title}</h3><p className="mt-1 truncate font-mono text-[11px] text-ink-400">{course.course_id} · {course.template_id}</p></div></div><div className="flex shrink-0 items-center gap-4"><AdminStatusBadge tone={statusTone(course.status)}>{statusLabel(course.status)}</AdminStatusBadge><span className="hidden text-[12px] text-ink-400 sm:block">Updated {formatDate(course.updated_at)}</span><Link href={`/editor/${course.document_id}`} className="creator-outline-button">Open <ArrowRight size={13} /></Link></div></article>;
}
