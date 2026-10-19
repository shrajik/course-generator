"use client";

/**
 * Source documents: the creator's own PDFs, Word files and notes, which become
 * the ground truth the course is written from and checked against.
 *
 * Files are uploaded straight away and processed on the server (read, split
 * into passages, indexed); each row shows that progress. A ready file can be
 * ticked to use it for this course - files uploaded before are listed too, so
 * they can be reused. Optional by design: with nothing ticked the course is
 * generated exactly as before.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight, FileText, Loader2, Trash2, Upload } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  ACCEPTED_SOURCE_TYPES,
  deleteSource,
  isProcessing,
  listSources,
  uploadSource,
  type SourceRecord,
} from "@/lib/api/sources";
import { useAuth } from "@/lib/auth/auth-provider";
import { cn } from "@/lib/utils/cn";

const POLL_MS = 1500;
const STATUS_LABEL: Record<string, string> = {
  uploaded: "Waiting to start",
  parsing: "Reading the document",
  chunking: "Splitting into passages",
  embedding: "Indexing",
};

interface Props {
  selected: string[];
  onChange: (sourceIds: string[]) => void;
}

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function SourcesPanel({ selected, onChange }: Props) {
  const { isAuthenticated } = useAuth();
  const [open, setOpen] = useState(selected.length > 0);
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  // Always the latest selection, for callbacks that outlive a render.
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const refresh = useCallback(async () => {
    try {
      setSources(await listSources());
      setLoaded(true);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load your files.");
    }
  }, []);

  useEffect(() => {
    if (open && isAuthenticated) void refresh();
  }, [open, isAuthenticated, refresh]);

  // Keep polling while anything is still being processed.
  const busy = sources.some((source) => isProcessing(source.status));
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [busy, refresh]);

  // Files just uploaded are ticked once they turn out to be usable - never a
  // failed one - and a ticked file that no longer exists or has failed is dropped.
  const autoSelect = useRef(new Set<string>());
  useEffect(() => {
    if (!loaded) return;
    const byId = new Map(sources.map((source) => [source.source_id, source]));
    const next = selectedRef.current.filter((id) => {
      const source = byId.get(id);
      return source !== undefined && source.status !== "failed";
    });
    for (const id of Array.from(autoSelect.current)) {
      const source = byId.get(id);
      if (!source) {
        autoSelect.current.delete(id);
      } else if (source.status === "ready") {
        autoSelect.current.delete(id);
        if (!next.includes(id)) next.push(id);
      } else if (source.status === "failed") {
        autoSelect.current.delete(id);
      }
    }
    if (next.length !== selectedRef.current.length || next.some((id, i) => id !== selectedRef.current[i])) {
      onChange(next);
    }
  }, [sources, loaded, onChange]);

  const upload = async (files: FileList | File[]) => {
    setError(null);
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        try {
          const record = await uploadSource(file);
          setSources((current) => [record, ...current.filter((s) => s.source_id !== record.source_id)]);
          autoSelect.current.add(record.source_id);
          setLoaded(true);
        } catch (caught) {
          setError(
            `${file.name}: ${caught instanceof ApiError ? caught.message : "the upload failed."}`,
          );
        }
      }
    } finally {
      setUploading(false);
    }
  };

  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);

  const remove = async (source: SourceRecord) => {
    try {
      await deleteSource(source.source_id);
      setSources((current) => current.filter((item) => item.source_id !== source.source_id));
      onChange(selected.filter((id) => id !== source.source_id));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not delete the file.");
    }
  };

  return (
    <div className="rounded-card border border-line">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <span>
          <span className="text-[12.5px] font-medium text-ink-700">Source Documents</span>
          <span className="ml-2 text-[11.5px] text-ink-400">
            {selected.length > 0
              ? `${selected.length} selected as the ground truth`
              : "Optional — upload PDFs, Word files or notes to write the course from"}
          </span>
        </span>
        {open ? (
          <ChevronDown size={16} className="shrink-0 text-ink-400" aria-hidden />
        ) : (
          <ChevronRight size={16} className="shrink-0 text-ink-400" aria-hidden />
        )}
      </button>

      {open ? (
        <div className="space-y-3 border-t border-line px-4 py-4">
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
              "flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-6 text-center transition",
              dragging ? "border-brand-400 bg-brand-50" : "border-line bg-white",
            )}
          >
            {uploading ? (
              <Loader2 size={20} className="animate-spin text-brand-600" aria-hidden />
            ) : (
              <Upload size={20} className="text-ink-400" aria-hidden />
            )}
            <p className="text-[12.5px] text-ink-700">
              Drag files here, or{" "}
              <button
                type="button"
                onClick={() => input.current?.click()}
                className="font-medium text-brand-700 underline"
              >
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

          {error ? (
            <p className="flex items-start gap-2 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700">
              <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden />
              {error}
            </p>
          ) : null}

          {sources.length > 0 ? (
            <ul className="divide-y divide-line rounded-lg border border-line">
              {sources.map((source) => (
                <SourceRow
                  key={source.source_id}
                  source={source}
                  checked={selected.includes(source.source_id)}
                  onToggle={() => toggle(source.source_id)}
                  onRemove={() => void remove(source)}
                />
              ))}
            </ul>
          ) : null}
          <p className="text-[11.5px] text-ink-400">
            The writer is shown the passages that match each chapter and cites them, such as [S1.3].
            Scanned PDFs need a text layer.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function SourceRow({
  source,
  checked,
  onToggle,
  onRemove,
}: {
  source: SourceRecord;
  checked: boolean;
  onToggle: () => void;
  onRemove: () => void;
}) {
  const processing = isProcessing(source.status);
  const ready = source.status === "ready";
  return (
    <li className="flex items-start gap-3 px-3 py-2.5">
      <input
        type="checkbox"
        checked={checked}
        disabled={!ready}
        onChange={onToggle}
        aria-label={`Use ${source.filename}`}
        className="mt-1"
      />
      <FileText size={16} className="mt-0.5 shrink-0 text-ink-400" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[12.5px] font-medium text-ink-800">{source.filename}</p>
        <p className="text-[11.5px] text-ink-500">
          {formatSize(source.size_bytes)}
          {ready
            ? ` · ${source.page_count ? `${source.page_count} pages · ` : ""}${source.word_count.toLocaleString()} words · ${source.chunk_count} passages`
            : null}
        </p>
        {processing ? (
          <p className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-brand-700">
            <Loader2 size={11} className="animate-spin" aria-hidden />
            {source.message || STATUS_LABEL[source.status] || "Processing"}
          </p>
        ) : null}
        {ready ? (
          <p className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-emerald-700">
            <CheckCircle2 size={11} aria-hidden />
            Ready
            {source.embedded_chunks < source.chunk_count ? " (keyword search only)" : ""}
          </p>
        ) : null}
        {source.status === "failed" ? (
          <p className="mt-0.5 flex items-start gap-1.5 text-[11.5px] text-red-700">
            <AlertCircle size={11} className="mt-0.5 shrink-0" aria-hidden />
            {source.error ?? "Could not be processed."}
          </p>
        ) : null}
      </div>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Delete ${source.filename}`}
        className="text-ink-400 hover:text-red-600"
      >
        <Trash2 size={14} />
      </button>
    </li>
  );
}
