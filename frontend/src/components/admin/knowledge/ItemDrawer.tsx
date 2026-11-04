"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { AdminAlert, AdminStatusBadge } from "@/components/admin/AdminUI";
import { ApiError } from "@/lib/api/client";
import * as kb from "@/lib/api/kb";
import { isBusy, lifecycleBadge, nextStep, processingBadge, proposals, type Proposal } from "@/lib/kb/model";
import { cn } from "@/lib/utils/cn";

const POLL_MS = 1500;
const TABS = ["Overview", "Versions", "Content", "Relations", "Activity"] as const;
type Tab = (typeof TABS)[number];
const field = "h-9 w-full rounded-[8px] border border-line bg-white px-3 text-[13px] text-ink outline-none focus:border-brand-500";

interface Props {
  itemId: string;
  categories: kb.KbCategory[];
  onClose: () => void;
  /** Called after anything that changes what the list shows. */
  onChanged: () => void;
}

export function ItemDrawer({ itemId, categories, onClose, onChanged }: Props) {
  const [item, setItem] = useState<kb.KbItemDetail | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<string[]>([]);

  const load = useCallback(async () => {
    try {
      setItem(await kb.getItem(itemId));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The item could not be loaded.");
    }
  }, [itemId]);

  useEffect(() => {
    setItem(null);
    setError(null);
    setDismissed([]);
    setTab("Overview");
    void load();
  }, [load]);

  const busy = item?.versions.some((v) => isBusy(v.processing_status)) ?? false;
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void load().then(onChanged), POLL_MS);
    return () => clearInterval(timer);
  }, [busy, load, onChanged]);

  /** Run an action, then show the result; a refusal (409 and the like) is shown, never swallowed. */
  const act = async (work: () => Promise<unknown>) => {
    setError(null);
    try {
      await work();
      await load();
      onChanged();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "That did not work.");
      await load();
    }
  };

  if (!item) {
    return (
      <Shell onClose={onClose}>
        {error ? <AdminAlert>{error}</AdminAlert> : <Loader2 className="animate-spin text-brand-500" size={18} />}
      </Shell>
    );
  }
  const life = lifecycleBadge(item);
  const proc = processingBadge(item.processing);
  const hint = nextStep(item);
  const open = proposals(item).filter((p) => !dismissed.includes(p.field));

  return (
    <Shell onClose={onClose}>
      <div className="space-y-3">
        <div>
          <p className="font-sans text-[12px] font-medium tracking-wider text-brand-700">G{item.number}</p>
          <h3 className="text-[16px] font-semibold text-ink">{item.title}</h3>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            <AdminStatusBadge tone={life.tone}>{life.label}</AdminStatusBadge>
            <AdminStatusBadge tone={proc.tone}>{proc.label}</AdminStatusBadge>
            {item.unpublished_changes ? <AdminStatusBadge tone="warning">Unpublished changes</AdminStatusBadge> : null}
          </div>
          {hint ? <p className="mt-2 text-[12.5px] text-ink-500">{hint}</p> : null}
        </div>
        {error ? <AdminAlert>{error}</AdminAlert> : null}
        <Actions item={item} act={act} />
        {open.length > 0 ? (
          <Proposals
            items={open}
            onAccept={(p) => act(() => acceptProposal(item, p))}
            onDismiss={(p) => setDismissed([...dismissed, p.field])}
          />
        ) : null}
        <div className="flex gap-1 border-b border-line">
          {TABS.map((name) => (
            <button key={name} type="button" onClick={() => setTab(name)} className={cn("px-2.5 py-1.5 text-[12.5px]", tab === name ? "border-b-2 border-brand-600 font-medium text-brand-700" : "text-ink-500")}>
              {name}
            </button>
          ))}
        </div>
        {tab === "Overview" ? <Overview item={item} categories={categories} act={act} /> : null}
        {tab === "Versions" ? <Versions item={item} act={act} /> : null}
        {tab === "Content" ? <Content item={item} /> : null}
        {tab === "Relations" ? <Relations item={item} act={act} /> : null}
        {tab === "Activity" ? <Activity itemId={item.id} /> : null}
      </div>
    </Shell>
  );
}

function Shell({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  return (
    <aside className="sticky top-4 max-h-[calc(100vh-2rem)] overflow-y-auto rounded-[12px] border border-line bg-white p-4 shadow-sm">
      <button type="button" onClick={onClose} aria-label="Close" className="float-right text-ink-400 hover:text-ink">
        <X size={16} />
      </button>
      {children}
    </aside>
  );
}

async function acceptProposal(item: kb.KbItemDetail, p: Proposal) {
  if (p.field === "title") return kb.updateItem(item.id, { title: p.value });
  if (p.field === "description") return kb.updateItem(item.id, { description: p.value });
  if (p.field === "category") return kb.updateItem(item.id, { category_id: p.value.id });
  return kb.updateItem(item.id, { tags: [...item.tags, ...p.value] });
}

function Actions({ item, act }: { item: kb.KbItemDetail; act: (work: () => Promise<unknown>) => Promise<void> }) {
  if (item.deleted) {
    return <Button size="sm" variant="outline" onClick={() => act(() => kb.lifecycleAction(item.id, "undelete"))}>Restore from trash</Button>;
  }
  const ready = item.versions[0]?.processing_status === "ready";
  return (
    <div className="flex flex-wrap gap-2">
      {item.lifecycle !== "published" && item.lifecycle !== "archived" ? (
        <Button size="sm" disabled={!ready} onClick={() => act(() => kb.publishItem(item.id))}>Publish</Button>
      ) : null}
      {item.lifecycle === "published" ? <Button size="sm" variant="outline" onClick={() => act(() => kb.lifecycleAction(item.id, "unpublish"))}>Unpublish</Button> : null}
      {item.lifecycle === "archived" ? (
        <Button size="sm" variant="outline" onClick={() => act(() => kb.lifecycleAction(item.id, "restore"))}>Restore</Button>
      ) : (
        <Button size="sm" variant="outline" onClick={() => act(() => kb.lifecycleAction(item.id, "archive"))}>Archive</Button>
      )}
      <Button size="sm" variant="danger" onClick={() => act(() => kb.lifecycleAction(item.id, "delete"))}>Delete</Button>
    </div>
  );
}

function Proposals({ items, onAccept, onDismiss }: { items: Proposal[]; onAccept: (p: Proposal) => void; onDismiss: (p: Proposal) => void }) {
  const show = (p: Proposal) => (p.field === "category" ? p.value.name : Array.isArray(p.value) ? p.value.join(", ") : p.value);
  return (
    <div className="space-y-1.5 rounded-[8px] bg-brand-50 p-2.5">
      <p className="text-[11.5px] font-medium uppercase tracking-wide text-brand-700">Suggested for you</p>
      {items.map((p) => (
        <div key={p.field} className="flex items-start gap-2 text-[12.5px]">
          <div className="min-w-0 flex-1">
            <span className="text-ink-500">{p.label}: </span>
            <span className="text-ink">{show(p)}</span>
          </div>
          <button type="button" onClick={() => onAccept(p)} className="font-medium text-brand-700">Accept</button>
          <button type="button" onClick={() => onDismiss(p)} className="text-ink-400">Dismiss</button>
        </div>
      ))}
    </div>
  );
}

type Act = (work: () => Promise<unknown>) => Promise<void>;

function Overview({ item, categories, act }: { item: kb.KbItemDetail; categories: kb.KbCategory[]; act: Act }) {
  const [title, setTitle] = useState(item.title);
  const [description, setDescription] = useState(item.description);
  const [category, setCategory] = useState(item.category?.id ?? "");
  const [tags, setTags] = useState(item.tags.join(", "));
  // Re-sync only when the saved item changes under us (a proposal accepted, a reload), not on every keystroke.
  useEffect(() => {
    setTitle(item.title);
    setDescription(item.description);
    setCategory(item.category?.id ?? "");
    setTags(item.tags.join(", "));
  }, [item.title, item.description, item.category?.id, item.tags]);

  const save = () =>
    act(() =>
      kb.updateItem(item.id, {
        title,
        description,
        category_id: category || null,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      }),
    );
  return (
    <div className="space-y-2.5 text-[12.5px]">
      <label className="block font-medium text-ink-700">Title<input className={cn(field, "mt-1")} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="block font-medium text-ink-700">
        Description
        <textarea className={cn(field, "mt-1 h-20 py-2")} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional" />
      </label>
      <label className="block font-medium text-ink-700">
        Category
        <select className={cn(field, "mt-1")} value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">None</option>
          {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </label>
      <label className="block font-medium text-ink-700">Tags (comma separated)<input className={cn(field, "mt-1")} value={tags} onChange={(e) => setTags(e.target.value)} /></label>
      <Button size="sm" onClick={save}>Save</Button>
    </div>
  );
}

function Versions({ item, act }: { item: kb.KbItemDetail; act: Act }) {
  const file = useRef<HTMLInputElement>(null);
  const [keep, setKeep] = useState(true);
  const upload = async (files: FileList | null) => {
    if (files && files.length > 0) await act(() => kb.addVersion(item.id, { files: Array.from(files), keepPreviousSources: keep }));
  };
  return (
    <div className="space-y-2 text-[12.5px]">
      <ul className="divide-y divide-line">
        {item.versions.map((v) => {
          const proc = processingBadge(v.processing_status);
          return (
            <li key={v.id} className="space-y-1 py-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium text-ink">Version {v.version_no}</span>
                {v.is_live ? <AdminStatusBadge tone="success">Live</AdminStatusBadge> : null}
                <AdminStatusBadge tone={proc.tone}>{proc.label}</AdminStatusBadge>
                <span className="text-ink-400">{v.passages} passages</span>
              </div>
              <p className="text-ink-500">{[...v.files, v.has_text ? "typed text" : ""].filter(Boolean).join(", ") || "—"}{v.change_note ? ` · ${v.change_note}` : ""}</p>
              {v.processing_error ? <p className="text-danger">{v.processing_error}</p> : null}
              {v.warnings.map((w) => <p key={w} className="text-amber-700">{w}</p>)}
              <div className="flex gap-2">
                {v.processing_status === "ready" && !v.is_live && !item.deleted ? (
                  <Button size="sm" variant="outline" onClick={() => act(() => kb.publishVersion(item.id, v.id, item.live_version_id))}>
                    {item.lifecycle === "published" ? "Switch to this version" : "Publish this version"}
                  </Button>
                ) : null}
                {v.processing_status === "failed" ? <Button size="sm" variant="outline" onClick={() => act(() => kb.retryVersion(item.id, v.id))}>Retry</Button> : null}
              </div>
            </li>
          );
        })}
      </ul>
      <div className="rounded-[8px] border border-dashed border-line p-2.5">
        <p className="font-medium text-ink-700">Add a new version</p>
        <p className="text-ink-500">The live version keeps serving until you publish this one.</p>
        <label className="mt-1.5 flex items-center gap-1.5 text-ink-700">
          <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} /> Keep the current files as well
        </label>
        <Button size="sm" variant="outline" className="mt-2" onClick={() => file.current?.click()}>Choose file(s)</Button>
        <input ref={file} type="file" multiple className="hidden" accept=".pdf,.docx,.txt,.md,.markdown" onChange={(e) => { void upload(e.target.files); e.target.value = ""; }} />
      </div>
    </div>
  );
}

function Content({ item }: { item: kb.KbItemDetail }) {
  const [chunks, setChunks] = useState<kb.KbChunk[] | null>(null);
  const version = item.versions[0]?.id;
  useEffect(() => {
    let cancelled = false;
    if (version) kb.itemChunks(item.id, version).then((c) => !cancelled && setChunks(c)).catch(() => !cancelled && setChunks([]));
    return () => { cancelled = true; };
  }, [item.id, version]);
  if (chunks === null) return <Loader2 className="animate-spin text-brand-500" size={16} />;
  if (chunks.length === 0) return <p className="text-[12.5px] text-ink-500">Nothing to show yet.</p>;
  return (
    <ul className="space-y-2">
      {chunks.map((c) => (
        <li key={c.ref} className="rounded-[8px] border border-line p-2 text-[12px]">
          <p className="mb-1 flex flex-wrap items-center gap-1.5 text-ink-500">
            <span className="font-sans font-medium text-brand-700">[{c.ref}]</span>{c.heading}{c.page ? ` · page ${c.page}` : ""}
            {c.topics.map((t) => <span key={t} className="rounded bg-brand-50 px-1 text-[10.5px] text-brand-700">{t}</span>)}
            {!c.embedded ? <span className="text-amber-700">keyword search only</span> : null}
          </p>
          <p className="whitespace-pre-wrap text-ink-700">{c.text}</p>
        </li>
      ))}
    </ul>
  );
}

const RELATIONS: kb.RelationType[] = ["related_to", "references", "derived_from", "depends_on", "supersedes", "duplicate_of"];

function Relations({ item, act }: { item: kb.KbItemDetail; act: Act }) {
  const [term, setTerm] = useState("");
  const [matches, setMatches] = useState<kb.KbItem[]>([]);
  const [type, setType] = useState<kb.RelationType>("related_to");
  useEffect(() => {
    if (term.trim().length < 2) return setMatches([]);
    const timer = setTimeout(() => kb.listItems({ q: term, limit: 6 }).then((r) => setMatches(r.items.filter((i) => i.id !== item.id))).catch(() => setMatches([])), 250);
    return () => clearTimeout(timer);
  }, [term, item.id]);
  const label = (r: kb.KbRelation) => (r.direction === "out" ? r.relation_type.replace("_", " ") : `is ${r.relation_type.replace("_", " ")} by`);
  return (
    <div className="space-y-3 text-[12.5px]">
      {item.relations.length === 0 ? <p className="text-ink-500">No relationships yet.</p> : (
        <ul className="divide-y divide-line">
          {item.relations.map((r) => (
            <li key={r.id} className="flex items-center gap-2 py-1.5">
              <span className="text-ink-500">{label(r)}</span>
              <span className="min-w-0 flex-1 truncate text-ink">G{r.other_number} · {r.other_title}</span>
              {r.source === "ai" ? <span className="text-[10.5px] text-ink-400">suggested</span> : null}
              <button type="button" className="text-ink-400 hover:text-danger" onClick={() => act(() => kb.removeRelation(r.id))}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      <div className="space-y-1.5 rounded-[8px] border border-dashed border-line p-2.5">
        <div className="flex gap-2">
          <select className={cn(field, "w-40")} value={type} onChange={(e) => setType(e.target.value as kb.RelationType)}>
            {RELATIONS.map((t) => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
          </select>
          <input className={field} value={term} onChange={(e) => setTerm(e.target.value)} placeholder="Find an item to connect" />
        </div>
        {matches.map((m) => (
          <button key={m.id} type="button" className="block w-full truncate rounded px-2 py-1 text-left hover:bg-brand-50" onClick={() => { setTerm(""); void act(() => kb.addRelation(item.id, m.id, type)); }}>
            G{m.number} · {m.title}
          </button>
        ))}
      </div>
    </div>
  );
}

function Activity({ itemId }: { itemId: string }) {
  const [rows, setRows] = useState<kb.KbActivity[] | null>(null);
  useEffect(() => { kb.itemActivity(itemId).then(setRows).catch(() => setRows([])); }, [itemId]);
  if (rows === null) return <Loader2 className="animate-spin text-brand-500" size={16} />;
  return (
    <ul className="space-y-1 text-[12px]">
      {rows.map((r) => (
        <li key={r.id} className="flex gap-2">
          <span className="w-32 shrink-0 text-ink-400">{new Date(r.created_at).toLocaleString()}</span>
          <span className="text-ink-700">{r.action.replace(/_/g, " ")}</span>
        </li>
      ))}
    </ul>
  );
}
