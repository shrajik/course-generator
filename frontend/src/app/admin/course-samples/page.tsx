"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { BookMarked, CheckCircle2, Plus, Search, Trash2, X } from "lucide-react";
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
  deleteCourseSample,
  listCourseSamples,
  registerCourseSample,
  updateCourseSample,
  type CourseSampleEntry,
} from "@/lib/api/courseSamples";

const inputClass =
  "h-10 w-full rounded-[8px] border border-line bg-white px-3 text-[13px] text-ink outline-none placeholder:text-ink-400 transition-colors focus:border-brand-500";

function emptyDraft() {
  return { courseId: "", title: "", topic: "", description: "", tags: "" };
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export default function AdminCourseSamplesPage() {
  const [items, setItems] = useState<CourseSampleEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [draft, setDraft] = useState(emptyDraft());
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    setIsLoading(true);
    listCourseSamples({ search: search || undefined, limit: 60 })
      .then((response) => {
        setItems(response.items);
        setTotal(response.total);
        setError(null);
      })
      .catch((caught) => setError(caught instanceof ApiError ? caught.message : "Samples could not be loaded."))
      .finally(() => setIsLoading(false));
  }, [search]);

  useEffect(() => load(), [load]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    setSaving(true);
    try {
      await registerCourseSample({
        course_id: draft.courseId,
        title: draft.title || undefined,
        topic: draft.topic,
        description: draft.description,
        tags: draft.tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      setShowForm(false);
      setDraft(emptyDraft());
      load();
    } catch (caught) {
      setFormError(caught instanceof ApiError ? caught.message : "The sample could not be registered.");
    } finally {
      setSaving(false);
    }
  };

  const toggleApproved = async (entry: CourseSampleEntry) => {
    try {
      await updateCourseSample(entry.id, { approved: !entry.approved });
      load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not update the sample.");
    }
  };

  const remove = async (entry: CourseSampleEntry) => {
    try {
      await deleteCourseSample(entry.id);
      load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not remove the sample.");
    }
  };

  return (
    <div className="admin-page mx-auto flex w-full max-w-[1240px] flex-col gap-5">
      <AdminPageHeader
        eyebrow="AI memory layer"
        title="Course samples"
        description="Mark an existing, already-generated course as a reference for future generation. Nothing is copied - the AI reads the real course fresh each time it's used."
      />

      {error ? <AdminAlert>{error}</AdminAlert> : null}

      <AdminPanel className="flex flex-wrap items-center justify-between gap-3 p-4">
        <div className="relative w-full max-w-[320px]">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#8E8780]" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search title or topic"
            className={`${inputClass} pl-9`}
          />
        </div>
        <Button type="button" size="sm" onClick={() => setShowForm(true)} className="gap-1.5">
          <Plus size={14} />
          Register sample
        </Button>
      </AdminPanel>

      {showForm ? (
        <AdminPanel className="p-5">
          <div className="flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-[#F7EEE2]">Register a course sample</h3>
            <Button type="button" variant="ghost" size="sm" onClick={() => setShowForm(false)}>
              <X size={14} />
            </Button>
          </div>
          {formError ? <div className="mt-3"><AdminAlert>{formError}</AdminAlert></div> : null}
          <form onSubmit={submit} className="mt-4 grid gap-3 sm:grid-cols-2">
            <input
              required
              placeholder="Course ID (e.g. crs_abc123)"
              value={draft.courseId}
              onChange={(event) => setDraft({ ...draft, courseId: event.target.value })}
              className={inputClass}
            />
            <input
              placeholder="Title (defaults to the course title)"
              value={draft.title}
              onChange={(event) => setDraft({ ...draft, title: event.target.value })}
              className={inputClass}
            />
            <input
              placeholder="Topic (e.g. negotiation)"
              value={draft.topic}
              onChange={(event) => setDraft({ ...draft, topic: event.target.value })}
              className={inputClass}
            />
            <input
              placeholder="Tags, comma separated"
              value={draft.tags}
              onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
              className={inputClass}
            />
            <textarea
              placeholder="Why this course is a good reference"
              value={draft.description}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
              className={`${inputClass} sm:col-span-2 h-20 pt-2`}
            />
            <div className="flex justify-end gap-2 sm:col-span-2">
              <Button type="button" variant="ghost" size="sm" onClick={() => setShowForm(false)}>
                Cancel
              </Button>
              <Button type="submit" size="sm" disabled={saving}>
                Register
              </Button>
            </div>
          </form>
        </AdminPanel>
      ) : null}

      <AdminPanel>
        {isLoading ? (
          <AdminLoading label="Loading samples" />
        ) : items.length === 0 ? (
          <AdminEmpty>
            <BookMarked size={24} className="text-brand-300" />
            <span>No course samples registered yet.</span>
          </AdminEmpty>
        ) : (
          <div className="overflow-x-auto">
            <table className="admin-table w-full min-w-[900px] text-left text-[13px]">
              <thead className="bg-[#181614] text-[12px] uppercase tracking-[0.08em] text-[#8E8780]">
                <tr>
                  <th className="px-4 py-3 font-medium">Title</th>
                  <th className="px-4 py-3 font-medium">Course ID</th>
                  <th className="px-4 py-3 font-medium">Topic</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Registered</th>
                  <th className="px-4 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#302D29]">
                {items.map((item) => (
                  <tr key={item.id} className="text-[#D8D0C7]">
                    <td className="max-w-[260px] px-4 py-3 font-medium text-[#F4EFE7]">
                      <span className="block truncate">{item.title}</span>
                      <span className="mt-1 block truncate text-[12px] font-normal text-[#8E8780]">
                        {item.description || "No description"}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-[12px] text-[#A9A29A]">{item.course_id}</td>
                    <td className="px-4 py-3 text-[#A9A29A]">{item.topic || "-"}</td>
                    <td className="px-4 py-3">
                      <AdminStatusBadge tone={item.approved ? "success" : "neutral"}>
                        {item.approved ? "approved" : "pending"}
                      </AdminStatusBadge>
                    </td>
                    <td className="px-4 py-3 text-[#A9A29A]">{formatDate(item.created_at)}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => toggleApproved(item)}
                          className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                          title={item.approved ? "Unapprove" : "Approve"}
                        >
                          <CheckCircle2 size={13} />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => remove(item)}
                          className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                          title="Remove"
                        >
                          <Trash2 size={13} />
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
      <p className="text-[12px] text-ink-400">{total.toLocaleString()} sample{total === 1 ? "" : "s"} registered</p>
    </div>
  );
}
