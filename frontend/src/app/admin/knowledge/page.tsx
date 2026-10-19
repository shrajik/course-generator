"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileText,
  Loader2,
  Search,
  Trash2,
  Upload,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import {
  AdminAlert,
  AdminEmpty,
  AdminLoading,
  AdminPageHeader,
  AdminPanel,
} from "@/components/admin/AdminUI";
import {
  deleteKnowledge,
  knowledgeChunks,
  listKnowledge,
  searchKnowledge,
  updateKnowledge,
  uploadKnowledge,
  type KnowledgeChunk,
  type KnowledgeDocument,
  type KnowledgeHit,
  type KnowledgeStats,
} from "@/lib/api/knowledge";
import { ACCEPTED_SOURCE_TYPES, isProcessing } from "@/lib/api/sources";
import { cn } from "@/lib/utils/cn";

const POLL_MS = 1500;
const inputClass =
  "h-10 w-full rounded-[8px] border border-line bg-white px-3 text-[13px] text-ink outline-none placeholder:text-ink-400 transition-colors focus:border-brand-500";

function formatSize(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * The global knowledge library, the platform's shared memory. Documents added
 * here are searched by every course while its chapters are written; a chapter
 * is offered only the passages relevant to it. Each document has a permanent
 * number, so a citation such as [G2.5] always means the same passage.
 */
export default function AdminKnowledgePage() {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [description, setDescription] = useState("");
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await listKnowledge();
      setDocuments(response.documents);
      setStats(response.stats);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The library could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const busy = documents.some((document) => isProcessing(document.status));
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [busy, refresh]);

  const upload = async (files: FileList | File[]) => {
    setError(null);
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        try {
          await uploadKnowledge(file, description);
        } catch (caught) {
          setError(`${file.name}: ${caught instanceof ApiError ? caught.message : "the upload failed."}`);
        }
      }
      setDescription("");
      await refresh();
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="space-y-5">
      <AdminPageHeader
        eyebrow="Global memory"
        title="Knowledge library"
        description="Documents every course draws on. When a chapter is written, the library is searched and the relevant passages are given to the writer, which cites them."
      />
      {error ? <AdminAlert>{error}</AdminAlert> : null}

      {stats ? (
        <div className="grid gap-3 sm:grid-cols-4">
          {[
            ["Documents", stats.documents],
            ["Searchable", stats.searchable],
            ["Passages", stats.passages],
            ["Processing", stats.processing],
          ].map(([label, value]) => (
            <AdminPanel key={label} className="px-4 py-3">
              <p className="text-[11px] font-medium uppercase tracking-wide text-ink-400">{label}</p>
              <p className="mt-1 text-[22px] font-semibold text-ink">{value}</p>
            </AdminPanel>
          ))}
        </div>
      ) : null}

      <AdminPanel className="p-4">
        <div
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            if (event.dataTransfer.files.length > 0) void upload(event.dataTransfer.files);
          }}
          className={cn(
            "flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-8 text-center transition",
            dragging ? "border-brand-400 bg-brand-50" : "border-line bg-white",
          )}
        >
          {uploading ? <Loader2 size={22} className="animate-spin text-brand-600" /> : <Upload size={22} className="text-ink-400" />}
          <p className="text-[13px] text-ink-700">
            Drag documents here, or{" "}
            <button type="button" onClick={() => input.current?.click()} className="font-medium text-brand-700 underline">
              choose files
            </button>
          </p>
          <p className="text-[11.5px] text-ink-400">PDF, Word (.docx), text or Markdown · up to 20 MB each</p>
          <input
            ref={input}
            type="file"
            multiple
            accept={ACCEPTED_SOURCE_TYPES}
            className="hidden"
            onChange={(event) => {
              if (event.target.files) void upload(event.target.files);
              event.target.value = "";
            }}
          />
        </div>
        <label className="mt-3 block text-[12.5px] font-medium text-ink-700">
          Description for the next upload (optional)
          <input
            className={cn(inputClass, "mt-1.5")}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="for example: Company refund and returns policy, 2026 edition"
          />
        </label>
      </AdminPanel>

      <AdminPanel>
        <div className="border-b border-line px-5 py-4">
          <h2 className="text-[15px] font-semibold text-ink">Documents</h2>
          <p className="mt-1 text-[12px] text-ink-500">Switch a document off to stop courses using it, without deleting it.</p>
        </div>
        {loading ? (
          <AdminLoading label="Loading the library" />
        ) : documents.length === 0 ? (
          <AdminEmpty>
            <FileText size={24} className="text-brand-300" />
            <span>No documents yet. Upload one and every new course can use it.</span>
          </AdminEmpty>
        ) : (
          <ul className="divide-y divide-line">
            {documents.map((document) => (
              <DocumentRow key={document.source_id} document={document} onChanged={refresh} onError={setError} />
            ))}
          </ul>
        )}
      </AdminPanel>

      <SearchProbe />
    </div>
  );
}

function DocumentRow({
  document,
  onChanged,
  onError,
}: {
  document: KnowledgeDocument;
  onChanged: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [chunks, setChunks] = useState<KnowledgeChunk[] | null>(null);
  const [description, setDescription] = useState(document.description);
  // Shown at once and rolled back if the server refuses, so the switch feels instant.
  const [enabled, setEnabled] = useState(document.enabled);
  const processing = isProcessing(document.status);
  const ready = document.status === "ready";

  useEffect(() => setDescription(document.description), [document.description]);
  useEffect(() => setEnabled(document.enabled), [document.enabled]);

  const act = async (work: () => Promise<unknown>, fallback: string) => {
    try {
      await work();
      await onChanged();
    } catch (caught) {
      onError(caught instanceof ApiError ? caught.message : fallback);
    }
  };

  const toggleOpen = async () => {
    setOpen(!open);
    if (!open && chunks === null && ready) {
      try {
        setChunks(await knowledgeChunks(document.source_id));
      } catch {
        setChunks([]);
      }
    }
  };

  return (
    <li className="px-5 py-3.5">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 rounded-[6px] bg-brand-50 px-2 py-0.5 font-sans text-[12px] font-medium tracking-wider text-brand-700">
          G{document.global_number ?? "?"}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13.5px] font-medium text-ink">{document.filename}</p>
          <p className="text-[11.5px] text-ink-500">
            {formatSize(document.size_bytes)}
            {ready
              ? ` · ${document.page_count ? `${document.page_count} pages · ` : ""}${document.word_count.toLocaleString()} words · ${document.chunk_count} passages`
              : null}
          </p>
          {processing ? (
            <p className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-brand-700">
              <Loader2 size={11} className="animate-spin" /> {document.message || "Processing"}
            </p>
          ) : null}
          {document.status === "failed" ? (
            <p className="mt-0.5 flex items-start gap-1.5 text-[11.5px] text-red-700">
              <AlertCircle size={11} className="mt-0.5 shrink-0" /> {document.error ?? "Could not be processed."}
            </p>
          ) : null}
          {ready ? (
            <input
              aria-label={`Description of ${document.filename}`}
              className="mt-1.5 w-full max-w-[560px] rounded-[6px] border border-transparent px-1.5 py-1 text-[12px] text-ink-600 outline-none hover:border-line focus:border-brand-400"
              value={description}
              placeholder="Add a description"
              onChange={(event) => setDescription(event.target.value)}
              onBlur={() =>
                description !== document.description
                  ? void act(() => updateKnowledge(document.source_id, { description }), "The description could not be saved.")
                  : undefined
              }
            />
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-3">
          {ready ? (
            <label className="flex items-center gap-1.5 text-[12px] text-ink-600">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => {
                  const next = event.target.checked;
                  setEnabled(next);
                  void act(async () => {
                    try {
                      await updateKnowledge(document.source_id, { enabled: next });
                    } catch (caught) {
                      setEnabled(!next);
                      throw caught;
                    }
                  }, "The change could not be saved.");
                }}
              />
              {enabled ? (
                <span className="flex items-center gap-1 text-emerald-700"><CheckCircle2 size={12} /> Used by courses</span>
              ) : (
                <span className="text-ink-400">Switched off</span>
              )}
            </label>
          ) : null}
          {ready ? (
            <button type="button" onClick={() => void toggleOpen()} className="text-[12px] text-ink-500 hover:text-ink" aria-expanded={open}>
              {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            </button>
          ) : null}
          <button
            type="button"
            aria-label={`Delete ${document.filename}`}
            onClick={() => {
              if (window.confirm(`Delete ${document.filename}? Courses already written keep their text, but its [G${document.global_number}.n] citations will no longer resolve.`)) {
                void act(() => deleteKnowledge(document.source_id), "The document could not be deleted.");
              }
            }}
            className="text-ink-400 hover:text-red-600"
          >
            <Trash2 size={15} />
          </button>
        </div>
      </div>

      {open ? (
        <ul className="mt-3 space-y-2 border-l-2 border-line pl-4">
          {chunks === null ? <li className="text-[12px] text-ink-400">Loading…</li> : null}
          {chunks?.map((chunk) => (
            <li key={chunk.index} className="text-[12px] text-ink-700">
              <p className="text-[11px] text-ink-400">
                [G{document.global_number}.{chunk.index + 1}]
                {chunk.page ? ` · page ${chunk.page}` : ""}
                {chunk.heading_path.length ? ` · ${chunk.heading_path.join(" > ")}` : ""}
              </p>
              <p className="line-clamp-3 whitespace-pre-wrap">{chunk.text}</p>
            </li>
          ))}
          {chunks && chunks.length < document.chunk_count ? (
            <li className="text-[11.5px] text-ink-400">Showing the first {chunks.length} of {document.chunk_count} passages.</li>
          ) : null}
        </ul>
      ) : null}
    </li>
  );
}

function SearchProbe() {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!query.trim()) return;
    setSearching(true);
    setError(null);
    try {
      setHits(await searchKnowledge(query.trim()));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The search failed.");
    } finally {
      setSearching(false);
    }
  };

  return (
    <AdminPanel className="p-5">
      <h2 className="text-[15px] font-semibold text-ink">Try the search</h2>
      <p className="mt-1 text-[12px] text-ink-500">
        Type what a chapter might be about. This shows exactly what a course would be offered, and nothing when no passage is relevant enough.
      </p>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void run();
        }}
      >
        <input className={inputClass} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="for example: how are refunds handled for disputed requests" />
        <Button type="submit" disabled={searching || !query.trim()}>
          {searching ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
          Search
        </Button>
      </form>
      {error ? <p className="mt-2 text-[12px] text-red-700">{error}</p> : null}
      {hits !== null ? (
        hits.length === 0 ? (
          <p className="mt-3 rounded-[8px] border border-line px-3 py-3 text-[12.5px] text-ink-500">
            Nothing in the library is relevant to this, so a chapter on it would get no library passages.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {hits.map((hit) => (
              <li key={hit.ref} className="rounded-[8px] border border-line px-3 py-2.5 text-[12.5px]">
                <p className="mb-1 text-[11px] text-ink-400">
                  <span className="font-mono font-semibold text-brand-700">[{hit.ref}]</span> {hit.filename}
                  {hit.page ? `, page ${hit.page}` : ""}
                  {hit.heading ? ` · ${hit.heading}` : ""}
                </p>
                <p className="whitespace-pre-wrap text-ink-800">{hit.text}</p>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </AdminPanel>
  );
}
