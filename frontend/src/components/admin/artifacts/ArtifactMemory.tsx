"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Search } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { AdminAlert, AdminEmpty, AdminLoading, AdminPanel, AdminStatusBadge } from "@/components/admin/AdminUI";
import { ApiError } from "@/lib/api/client";
import * as api from "@/lib/api/artifacts";
import { citation, provenance, reviewBadge, typeLabel, versionRows } from "@/lib/artifacts/model";
import { cn } from "@/lib/utils/cn";

const TYPES: api.ArtifactType[] = ["chapter", "visual", "questions", "assessment", "activity", "case_study", "other"];
const STATES: { value: api.ReviewStatus | ""; label: string }[] = [
  { value: "", label: "All" },
  { value: "pending_review", label: "Waiting for review" },
  { value: "approved", label: "Approved" },
  { value: "changes_requested", label: "Changes requested" },
];

/**
 * Approved artifact memory: what AI generated and a subject-matter expert approved. Approved versions are
 * locked for good (the database refuses changes); a change is a new version that needs its own approval.
 * Only approved, current versions are offered to future courses.
 */
export function ArtifactMemory({ canManage = true }: { canManage?: boolean }) {
  const [items, setItems] = useState<api.ArtifactSummary[]>([]);
  const [counts, setCounts] = useState<Record<api.ReviewStatus, number> | null>(null);
  const [state, setState] = useState<api.ReviewStatus | "">("pending_review");
  const [type, setType] = useState<api.ArtifactType | "">("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [page, tally] = await Promise.all([
        api.listArtifacts({ review_status: state || undefined, artifact_type: type || undefined, q: query || undefined, limit: 100 }),
        api.artifactCounts(),
      ]);
      setItems(page.items);
      setCounts(tally);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The artifacts could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [state, type, query]);

  useEffect(() => {
    const timer = setTimeout(() => void refresh(), query ? 250 : 0);
    return () => clearTimeout(timer);
  }, [refresh, query]);

  return (
    <div className="space-y-4">
      {error ? <AdminAlert>{error}</AdminAlert> : null}
      <div className="flex flex-wrap items-center gap-2">
        {STATES.map((s) => (
          <button key={s.value} type="button" onClick={() => setState(s.value)} className={cn("rounded-full border px-3 py-1 text-[12.5px]", state === s.value ? "border-brand-600 bg-brand-50 text-brand-700" : "border-line text-ink-700")}>
            {s.label}
            {counts && s.value ? <span className="ml-1.5 text-ink-400">{counts[s.value]}</span> : null}
          </button>
        ))}
        <select value={type} onChange={(e) => setType(e.target.value as api.ArtifactType | "")} className="h-8 rounded-[8px] border border-line bg-white px-2 text-[12.5px]">
          <option value="">Any type</option>
          {TYPES.map((t) => <option key={t} value={t}>{typeLabel(t)}</option>)}
        </select>
        <label className="relative min-w-[180px] flex-1">
          <Search size={14} className="absolute left-2.5 top-2 text-ink-400" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search titles and courses" className="h-8 w-full rounded-[8px] border border-line pl-8 pr-3 text-[12.5px] outline-none focus:border-brand-500" />
        </label>
      </div>
      <div className={cn("grid gap-4", selected ? "lg:grid-cols-[minmax(0,1fr)_440px]" : "")}>
        <AdminPanel>
          {loading ? <AdminLoading label="Loading artifacts" /> : items.length === 0 ? (
            <AdminEmpty><span>{state === "pending_review" ? "Nothing is waiting for review." : "No artifacts match."}</span></AdminEmpty>
          ) : (
            <ul className="divide-y divide-line">
              {items.map((item) => {
                const badge = reviewBadge(item.review_status);
                return (
                  <li key={item.id}>
                    <button type="button" onClick={() => setSelected(item.id)} className={cn("flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-brand-50", selected === item.id && "bg-brand-50")}>
                      <span className="mt-0.5 rounded-[6px] bg-brand-50 px-2 py-0.5 font-sans text-[12px] font-medium text-brand-700">A{item.number}</span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[13.5px] font-medium text-ink">{item.title}</p>
                        <p className="truncate text-[11.5px] text-ink-500">{typeLabel(item.artifact_type)} · {item.course_title || item.course_id}</p>
                      </div>
                      <div className="flex shrink-0 flex-wrap justify-end gap-1">
                        {item.status === "archived" ? <AdminStatusBadge>Archived</AdminStatusBadge> : null}
                        <AdminStatusBadge tone={badge.tone}>{badge.label}</AdminStatusBadge>
                        <span className="text-[11.5px] text-ink-400">v{item.latest_version}</span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </AdminPanel>
        {selected ? <Detail id={selected} canManage={canManage} onClose={() => setSelected(null)} onChanged={() => void refresh()} /> : null}
      </div>
      <Probe />
    </div>
  );
}

function Detail({ id, canManage, onClose, onChanged }: { id: string; canManage: boolean; onClose: () => void; onChanged: () => void }) {
  const [artifact, setArtifact] = useState<api.ArtifactDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setArtifact(await api.getArtifact(id));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "It could not be loaded.");
    }
  }, [id]);
  useEffect(() => {
    setArtifact(null);
    setComment("");
    setOpen(null);
    void load();
  }, [load]);

  const act = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
      setComment("");
      await load();
      onChanged();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "That did not work.");
      await load();
    }
  };

  if (!artifact) return <aside className="rounded-[12px] border border-line bg-white p-4">{error ? <AdminAlert>{error}</AdminAlert> : <Loader2 className="animate-spin text-brand-500" size={18} />}</aside>;
  const rows = versionRows(artifact);
  return (
    <aside className="sticky top-4 max-h-[calc(100vh-2rem)] space-y-3 overflow-y-auto rounded-[12px] border border-line bg-white p-4 text-[12.5px]">
      <button type="button" onClick={onClose} className="float-right text-ink-400 hover:text-ink">Close</button>
      <div>
        <p className="font-sans text-[12px] font-medium text-brand-700">A{artifact.number} · {typeLabel(artifact.artifact_type)}</p>
        <h3 className="text-[16px] font-semibold text-ink">{artifact.title}</h3>
        <p className="text-ink-500">{provenance(artifact, artifact.versions[0])}</p>
      </div>
      {error ? <AdminAlert>{error}</AdminAlert> : null}
      <ul className="divide-y divide-line">
        {rows.map(({ version, offered, ref, canDecide, reason }) => {
          const badge = reviewBadge(version.review_status);
          return (
            <li key={version.id} className="space-y-1.5 py-2.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium text-ink">Version {version.version_no}</span>
                <span className="font-sans text-ink-400">[{ref}]</span>
                <AdminStatusBadge tone={badge.tone}>{badge.label}</AdminStatusBadge>
                {offered ? <AdminStatusBadge tone="success">Offered to courses</AdminStatusBadge> : null}
                {version.locked ? <span className="text-ink-400">locked</span> : null}
                {version.review_status === "approved" && !version.indexed ? <span className="text-amber-700">not yet indexed</span> : null}
              </div>
              {version.review_comment ? <p className="text-ink-500">“{version.review_comment}”</p> : null}
              {version.approved_at ? <p className="text-ink-400">Approved {new Date(version.approved_at).toLocaleString()}</p> : null}
              <button type="button" className="text-brand-700 underline" onClick={() => setOpen(open === version.id ? null : version.id)}>
                {open === version.id ? "Hide content" : "Read content"}
              </button>
              {open === version.id ? <p className="max-h-60 overflow-y-auto whitespace-pre-wrap rounded-[8px] bg-brand-50 p-2 text-ink-700">{version.search_text}</p> : null}
              {canDecide ? (
                <div className="space-y-1.5">
                  <textarea value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comment (required to request changes)" className="h-16 w-full rounded-[8px] border border-line p-2 outline-none focus:border-brand-500" />
                  <div className="flex gap-2">
                    <Button size="sm" onClick={() => act(() => api.approveVersion(version.id, comment))}>Approve and lock</Button>
                    <Button size="sm" variant="outline" disabled={!comment.trim()} onClick={() => act(() => api.requestChanges(version.id, comment))}>Request changes</Button>
                  </div>
                </div>
              ) : reason && version.review_status !== "approved" ? <p className="text-ink-400">{reason}</p> : null}
            </li>
          );
        })}
      </ul>
      {artifact.links.length > 0 ? (
        <div>
          <p className="mb-1 font-medium text-ink-700">Connections</p>
          <ul className="space-y-0.5 text-ink-500">
            {artifact.links.map((l) => (
              <li key={l.id}>
                {l.direction === "in" ? "is the target of " : ""}{l.link_type.replace(/_/g, " ")}{" "}
                {l.target_course_id ? `course ${l.target_course_id}` : l.target_kb_number ? `knowledge G${l.target_kb_number}${l.target_kb_ordinal ? `.${l.target_kb_ordinal}` : ""}` : `A${l.other_number} · ${l.other_title}`}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {canManage ? (
        <div className="flex gap-2">
          {artifact.status === "active"
            ? <Button size="sm" variant="outline" onClick={() => act(() => api.archiveArtifact(artifact.id))}>Archive</Button>
            : <Button size="sm" variant="outline" onClick={() => act(() => api.restoreArtifact(artifact.id))}>Restore</Button>}
        </div>
      ) : null}
      <p className="text-ink-400">Reuse cites this as [{citation(artifact, artifact.versions[0])}]. Approved versions cannot be edited: change it by sending a new version.</p>
    </aside>
  );
}

/** What a chapter being written would be offered for a question. */
function Probe() {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<api.ArtifactHit[] | null>(null);
  const run = async () => {
    if (!query.trim()) return;
    setHits(await api.searchArtifacts(query).catch(() => []));
  };
  return (
    <AdminPanel className="p-4">
      <h3 className="text-[14px] font-semibold text-ink">Try a question</h3>
      <p className="mb-2 text-[12px] text-ink-500">See which approved artifacts a new course would be offered for reuse.</p>
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="for example: refund eligibility" className="h-9 flex-1 rounded-[8px] border border-line px-3 text-[13px] outline-none focus:border-brand-500" />
        <Button size="sm">Search</Button>
      </form>
      {hits ? (
        hits.length === 0 ? <p className="mt-2 text-[12.5px] text-ink-500">Nothing approved is relevant enough.</p> : (
          <ul className="mt-2 space-y-2">
            {hits.map((hit) => (
              <li key={hit.ref} className="rounded-[8px] border border-line p-2.5 text-[12.5px]">
                <p className="text-ink-500"><span className="font-sans font-medium text-brand-700">[{hit.ref}]</span> {typeLabel(hit.artifact_type)} · {hit.title} · {hit.course_title}</p>
                <p className="mt-1 text-ink-700">{hit.text}</p>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </AdminPanel>
  );
}
