"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { CheckCircle2, ImageOff, Plus, Search, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ApiError, apiUrl } from "@/lib/api/client";
import {
  AdminAlert,
  AdminEmpty,
  AdminLoading,
  AdminPageHeader,
  AdminPanel,
  AdminStatusBadge,
} from "@/components/admin/AdminUI";
import {
  deleteVisualKnowledge,
  listVisualKnowledge,
  registerVisualKnowledge,
  updateVisualKnowledge,
  type VisualKnowledgeEntry,
} from "@/lib/api/visualKnowledge";

const inputClass =
  "h-10 w-full rounded-[8px] border border-line bg-white px-3 text-[13px] text-ink outline-none placeholder:text-ink-400 transition-colors focus:border-brand-500";

function emptyDraft() {
  return { courseId: "", assetPath: "", kind: "diagram" as "diagram" | "illustration", topic: "", description: "", tags: "" };
}

export default function AdminVisualKnowledgePage() {
  const [items, setItems] = useState<VisualKnowledgeEntry[]>([]);
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
    listVisualKnowledge({ search: search || undefined, limit: 60 })
      .then((response) => {
        setItems(response.items);
        setTotal(response.total);
        setError(null);
      })
      .catch((caught) => setError(caught instanceof ApiError ? caught.message : "Visuals could not be loaded."))
      .finally(() => setIsLoading(false));
  }, [search]);

  useEffect(() => load(), [load]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    setSaving(true);
    try {
      await registerVisualKnowledge({
        course_id: draft.courseId || undefined,
        asset_path: draft.assetPath,
        kind: draft.kind,
        topic: draft.topic,
        description: draft.description,
        tags: draft.tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      setShowForm(false);
      setDraft(emptyDraft());
      load();
    } catch (caught) {
      setFormError(caught instanceof ApiError ? caught.message : "The visual could not be registered.");
    } finally {
      setSaving(false);
    }
  };

  const toggleApproved = async (entry: VisualKnowledgeEntry) => {
    try {
      await updateVisualKnowledge(entry.id, { approved: !entry.approved });
      load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not update the visual.");
    }
  };

  const remove = async (entry: VisualKnowledgeEntry) => {
    try {
      await deleteVisualKnowledge(entry.id);
      load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not remove the visual.");
    }
  };

  return (
    <div className="admin-page mx-auto flex w-full max-w-[1240px] flex-col gap-5">
      <AdminPageHeader
        eyebrow="AI memory layer"
        title="Visual knowledge"
        description="Reusable diagrams and illustrations the AI can reference for future courses. Registering points at an existing generated asset - nothing is duplicated."
      />

      {error ? <AdminAlert>{error}</AdminAlert> : null}

      <AdminPanel className="flex flex-wrap items-center justify-between gap-3 p-4">
        <div className="relative w-full max-w-[320px]">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#8E8780]" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search topic or description"
            className={`${inputClass} pl-9`}
          />
        </div>
        <Button type="button" size="sm" onClick={() => setShowForm(true)} className="gap-1.5">
          <Plus size={14} />
          Register visual
        </Button>
      </AdminPanel>

      {showForm ? (
        <AdminPanel className="p-5">
          <div className="flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-[#F7EEE2]">Register an existing visual</h3>
            <Button type="button" variant="ghost" size="sm" onClick={() => setShowForm(false)}>
              <X size={14} />
            </Button>
          </div>
          {formError ? <div className="mt-3"><AdminAlert>{formError}</AdminAlert></div> : null}
          <form onSubmit={submit} className="mt-4 grid gap-3 sm:grid-cols-2">
            <input
              placeholder="Course ID (e.g. crs_abc123)"
              value={draft.courseId}
              onChange={(event) => setDraft({ ...draft, courseId: event.target.value })}
              className={inputClass}
            />
            <input
              required
              placeholder="Asset path (e.g. assets/image_003.svg)"
              value={draft.assetPath}
              onChange={(event) => setDraft({ ...draft, assetPath: event.target.value })}
              className={inputClass}
            />
            <select
              value={draft.kind}
              onChange={(event) => setDraft({ ...draft, kind: event.target.value as "diagram" | "illustration" })}
              className={inputClass}
            >
              <option value="diagram">Diagram</option>
              <option value="illustration">Illustration</option>
            </select>
            <input
              placeholder="Topic (e.g. human eye anatomy)"
              value={draft.topic}
              onChange={(event) => setDraft({ ...draft, topic: event.target.value })}
              className={inputClass}
            />
            <input
              placeholder="Tags, comma separated"
              value={draft.tags}
              onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
              className={`${inputClass} sm:col-span-2`}
            />
            <textarea
              placeholder="Description"
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
          <AdminLoading label="Loading visuals" />
        ) : items.length === 0 ? (
          <AdminEmpty>
            <ImageOff size={24} className="text-brand-300" />
            <span>No visuals registered yet.</span>
          </AdminEmpty>
        ) : (
          <div className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((item) => (
              <article key={item.id} className="overflow-hidden rounded-[10px] border border-[#302D29] bg-[#181614]">
                <div className="flex h-36 items-center justify-center bg-[#0F0D0C]">
                  {item.asset_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={apiUrl(item.asset_url)} alt={item.topic} className="max-h-full max-w-full object-contain" />
                  ) : (
                    <ImageOff size={22} className="text-[#4B453F]" />
                  )}
                </div>
                <div className="p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-[13px] font-medium text-[#F4EFE7]">{item.topic || "Untitled"}</span>
                    <AdminStatusBadge tone={item.approved ? "success" : "neutral"}>
                      {item.approved ? "approved" : "pending"}
                    </AdminStatusBadge>
                  </div>
                  <p className="mt-1 line-clamp-2 text-[12px] text-[#A9A29A]">{item.description || "No description"}</p>
                  <p className="mt-1 text-[11px] text-[#8E8780]">{item.diagram_kind || item.kind}</p>
                  <div className="mt-3 flex items-center gap-1.5">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => toggleApproved(item)}
                      className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                      title={item.approved ? "Unapprove" : "Approve"}
                    >
                      <CheckCircle2 size={13} />
                      {item.approved ? "Unapprove" : "Approve"}
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => remove(item)}
                      className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                      title="Remove from the registry"
                    >
                      <Trash2 size={13} />
                    </Button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </AdminPanel>
      <p className="text-[12px] text-ink-400">{total.toLocaleString()} visual{total === 1 ? "" : "s"} registered</p>
    </div>
  );
}
