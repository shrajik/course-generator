"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FileText, Loader2, Search, Upload } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { AdminAlert, AdminEmpty, AdminLoading, AdminPanel, AdminStatusBadge } from "@/components/admin/AdminUI";
import { ApiError } from "@/lib/api/client";
import * as kb from "@/lib/api/kb";
import { buildCategoryTree, isBusy, lifecycleBadge, processingBadge } from "@/lib/kb/model";
import { cn } from "@/lib/utils/cn";
import { CategoryTree } from "./CategoryTree";
import { ItemDrawer } from "./ItemDrawer";

const POLL_MS = 2000;
const LIFECYCLES: { value: kb.Lifecycle | ""; label: string }[] = [
  { value: "", label: "Any status" },
  { value: "draft", label: "Draft" },
  { value: "published", label: "Published" },
  { value: "unpublished", label: "Unpublished" },
  { value: "archived", label: "Archived" },
];

/**
 * The knowledge base as an admin manages it: browse by category, find and filter, drop files in (only the
 * file is needed - the system proposes the rest), review, publish. Courses only ever see published items.
 */
export function KnowledgeWorkspace() {
  const [categories, setCategories] = useState<kb.KbCategory[]>([]);
  const [items, setItems] = useState<kb.KbItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string | null>(null);
  const [lifecycle, setLifecycle] = useState<kb.Lifecycle | "">("");
  const [needsReview, setNeedsReview] = useState(false);
  const [trash, setTrash] = useState(false);

  const tree = useMemo(() => buildCategoryTree(categories), [categories]);
  const everything = useMemo(() => tree.reduce((sum, node) => sum + node.total, 0), [tree]);

  const fail = (caught: unknown, fallback: string) => setError(caught instanceof ApiError ? caught.message : fallback);

  const refreshCategories = useCallback(async () => {
    try {
      setCategories(await kb.listCategories());
    } catch (caught) {
      fail(caught, "The categories could not be loaded.");
    }
  }, []);

  const refreshItems = useCallback(async () => {
    try {
      const page = await kb.listItems({
        q: query || undefined, category_id: category ?? undefined, lifecycle: lifecycle || undefined,
        needs_review: needsReview || undefined, deleted: trash || undefined, limit: 100,
      });
      setItems(page.items);
      setTotal(page.total);
    } catch (caught) {
      fail(caught, "The list could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [query, category, lifecycle, needsReview, trash]);

  // Typing in the search box waits a moment so each keystroke is not a request.
  useEffect(() => {
    const timer = setTimeout(() => void refreshItems(), query ? 250 : 0);
    return () => clearTimeout(timer);
  }, [refreshItems, query]);
  useEffect(() => void refreshCategories(), [refreshCategories]);

  const busy = items.some((item) => isBusy(item.processing));
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void refreshItems(), POLL_MS);
    return () => clearInterval(timer);
  }, [busy, refreshItems]);

  const changed = useCallback(() => {
    void refreshItems();
    void refreshCategories();
  }, [refreshItems, refreshCategories]);

  return (
    <div className="space-y-4">
      {error ? <AdminAlert>{error}</AdminAlert> : null}
      <Intake onCreated={(id) => { setSelected(id); setTrash(false); changed(); }} onError={setError} />
      <div className={cn("grid gap-4", selected ? "lg:grid-cols-[210px_minmax(0,1fr)_420px]" : "lg:grid-cols-[210px_minmax(0,1fr)]")}>
        <AdminPanel className="h-fit p-3">
          <CategoryTree
            tree={tree}
            total={everything}
            selected={category}
            onSelect={setCategory}
            onCreate={async (name, parentId) => {
              try {
                await kb.createCategory(name, parentId);
                await refreshCategories();
              } catch (caught) {
                fail(caught, "The category could not be created.");
              }
            }}
            onApprove={async (id) => {
              await kb.approveCategory(id).catch((caught) => fail(caught, "Could not approve it."));
              await refreshCategories();
            }}
          />
        </AdminPanel>

        <AdminPanel>
          <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
            <label className="relative min-w-[180px] flex-1">
              <Search size={14} className="absolute left-2.5 top-2.5 text-ink-400" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search titles, descriptions and tags" className="h-9 w-full rounded-[8px] border border-line pl-8 pr-3 text-[13px] outline-none focus:border-brand-500" />
            </label>
            <select value={lifecycle} onChange={(e) => setLifecycle(e.target.value as kb.Lifecycle | "")} className="h-9 rounded-[8px] border border-line bg-white px-2 text-[13px]">
              {LIFECYCLES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
            </select>
            <label className="flex items-center gap-1.5 text-[12.5px] text-ink-700"><input type="checkbox" checked={needsReview} onChange={(e) => setNeedsReview(e.target.checked)} /> Needs review</label>
            <label className="flex items-center gap-1.5 text-[12.5px] text-ink-700"><input type="checkbox" checked={trash} onChange={(e) => setTrash(e.target.checked)} /> Trash</label>
          </div>
          {loading ? <AdminLoading label="Loading the knowledge base" /> : items.length === 0 ? (
            <AdminEmpty>
              <FileText size={24} className="text-brand-300" />
              <span>{query || category || lifecycle || needsReview ? "Nothing matches these filters." : "No knowledge yet. Drop a file above to start."}</span>
            </AdminEmpty>
          ) : (
            <>
              <ul className="divide-y divide-line">
                {items.map((item) => <Row key={item.id} item={item} active={item.id === selected} onOpen={() => setSelected(item.id)} />)}
              </ul>
              {total > items.length ? <p className="p-3 text-[12px] text-ink-500">Showing {items.length} of {total}. Narrow the search to see the rest.</p> : null}
            </>
          )}
        </AdminPanel>

        {selected ? <ItemDrawer itemId={selected} categories={categories} onClose={() => setSelected(null)} onChanged={changed} /> : null}
      </div>
      <Probe />
    </div>
  );
}

function Row({ item, active, onOpen }: { item: kb.KbItem; active: boolean; onOpen: () => void }) {
  const life = lifecycleBadge(item);
  const proc = processingBadge(item.processing);
  return (
    <li>
      <button type="button" onClick={onOpen} className={cn("flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-brand-50", active && "bg-brand-50")}>
        <span className="mt-0.5 rounded-[6px] bg-brand-50 px-2 py-0.5 font-sans text-[12px] font-medium text-brand-700">G{item.number}</span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13.5px] font-medium text-ink">{item.title}</p>
          <p className="truncate text-[11.5px] text-ink-500">
            {[item.category?.name ?? "Uncategorized", ...item.tags.slice(0, 3)].join(" · ")}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">
          {item.needs_review && item.lifecycle === "draft" ? <AdminStatusBadge tone="warning">Review</AdminStatusBadge> : null}
          <AdminStatusBadge tone={life.tone}>{life.label}</AdminStatusBadge>
          {item.processing !== "ready" && item.processing !== null ? <AdminStatusBadge tone={proc.tone}>{proc.label}</AdminStatusBadge> : null}
          {item.unpublished_changes ? <AdminStatusBadge tone="warning">Newer version</AdminStatusBadge> : null}
        </div>
      </button>
    </li>
  );
}

/** Add knowledge: files dropped or chosen, or text typed in. The title is optional - leave it and the system picks one. */
function Intake({ onCreated, onError }: { onCreated: (id: string) => void; onError: (message: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [working, setWorking] = useState(false);
  const [typing, setTyping] = useState(false);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");

  const run = async (work: () => Promise<{ id: string }>) => {
    setWorking(true);
    onError("");
    try {
      onCreated((await work()).id);
    } catch (caught) {
      onError(caught instanceof ApiError ? caught.message : "That could not be added.");
    } finally {
      setWorking(false);
    }
  };
  // Each file becomes its own item (several files for one item are added from its Versions tab).
  const upload = async (files: FileList | File[]) => {
    for (const file of Array.from(files)) await run(() => kb.createItem({ files: [file] }));
  };

  return (
    <AdminPanel className="p-4">
      <div
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); if (e.dataTransfer.files.length) void upload(e.dataTransfer.files); }}
        className={cn("flex flex-col items-center gap-1.5 rounded-lg border border-dashed px-4 py-6 text-center", dragging ? "border-brand-400 bg-brand-50" : "border-line bg-white")}
      >
        {working ? <Loader2 size={20} className="animate-spin text-brand-600" /> : <Upload size={20} className="text-ink-400" />}
        <p className="text-[13px] text-ink-700">
          Drop files here, or <button type="button" onClick={() => input.current?.click()} className="font-medium text-brand-700 underline">choose files</button>
          {" "}or <button type="button" onClick={() => setTyping(!typing)} className="font-medium text-brand-700 underline">write text</button>
        </p>
        <p className="text-[11.5px] text-ink-400">PDF, Word, text or Markdown. You do not need to fill anything in: the title, tags and category are proposed for you to review.</p>
        <input ref={input} type="file" multiple accept=".pdf,.docx,.txt,.md,.markdown" className="hidden" onChange={(e) => { if (e.target.files) void upload(e.target.files); e.target.value = ""; }} />
      </div>
      {typing ? (
        <div className="mt-3 space-y-2">
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (optional)" className="h-9 w-full rounded-[8px] border border-line px-3 text-[13px] outline-none focus:border-brand-500" />
          <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="Write or paste the knowledge here. Use # headings to organise it." className="h-32 w-full rounded-[8px] border border-line p-3 text-[13px] outline-none focus:border-brand-500" />
          <Button size="sm" disabled={!text.trim() || working} onClick={() => run(() => kb.createItem({ bodyMd: text, title: title || undefined })).then(() => { setText(""); setTitle(""); setTyping(false); })}>Add</Button>
        </div>
      ) : null}
    </AdminPanel>
  );
}

/** Ask the library as a chapter would, to see what a course would be offered. */
function Probe() {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<kb.KbHit[] | null>(null);
  const [working, setWorking] = useState(false);
  const run = async () => {
    if (!query.trim()) return;
    setWorking(true);
    try {
      setHits(await kb.searchKb(query));
    } catch {
      setHits([]);
    } finally {
      setWorking(false);
    }
  };
  return (
    <AdminPanel className="p-4">
      <h3 className="text-[14px] font-semibold text-ink">Try a question</h3>
      <p className="mb-2 text-[12px] text-ink-500">See which published passages a course would be offered, and the citations it would use.</p>
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="for example: how long do refunds take?" className="h-9 flex-1 rounded-[8px] border border-line px-3 text-[13px] outline-none focus:border-brand-500" />
        <Button size="sm" disabled={working}>{working ? "Searching" : "Search"}</Button>
      </form>
      {hits ? (
        hits.length === 0 ? <p className="mt-2 text-[12.5px] text-ink-500">Nothing relevant enough. A course would be offered nothing here.</p> : (
          <ul className="mt-2 space-y-2">
            {hits.map((hit) => (
              <li key={hit.ref} className="rounded-[8px] border border-line p-2.5 text-[12.5px]">
                <p className="text-ink-500"><span className="font-sans font-medium text-brand-700">[{hit.ref}]</span> {hit.filename}{hit.heading ? ` · ${hit.heading}` : ""}{hit.page ? ` · page ${hit.page}` : ""}</p>
                <p className="mt-1 text-ink-700">{hit.text}</p>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </AdminPanel>
  );
}
