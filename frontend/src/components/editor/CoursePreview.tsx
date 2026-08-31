"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, Loader2 } from "lucide-react";
import { BlockRenderer } from "@/components/blocks/BlockRenderer";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import { getDocument } from "@/lib/api/documents";
import { PAGE_HEIGHT, PAGE_WIDTH, type CourseDocument } from "@/lib/types/document";

/** The course as the reader sees it: no selection, no handles, no toolbars. */
export function CoursePreview({ documentId }: { documentId: string }) {
  const router = useRouter();
  const [doc, setDoc] = useState<CourseDocument | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await getDocument(documentId);
        if (!cancelled) setDoc(loaded);
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof ApiError ? caught.message : "Preview failed to load.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  return (
    <div className="min-h-screen bg-canvas">
      <header className="no-print sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-line bg-white/95 px-4 py-2.5 backdrop-blur">
        <div className="flex items-center gap-3">
          <Button variant="outline" size="sm" onClick={() => router.push(`/editor/${documentId}`)}>
            <ArrowLeft size={13} />
            Back to editor
          </Button>
          <p className="text-[12.5px] font-semibold text-ink">
            {doc?.course_title ?? "Preview"}
          </p>
        </div>
        {doc ? (
          <p className="text-[11.5px] text-ink-400">
            {doc.pages.length} pages · version {doc.version}
          </p>
        ) : null}
      </header>

      <main className="mx-auto flex w-full max-w-[860px] flex-col items-center gap-6 px-4 py-8">
        {error ? (
          <p className="rounded-[10px] border border-red-200 bg-red-50 px-3 py-2 text-[12.5px] text-red-800">
            {error}
          </p>
        ) : !doc ? (
          <p className="flex items-center gap-2 py-16 text-[13px] text-ink-500">
            <Loader2 size={15} className="animate-spin text-brand-600" />
            Loading preview…
          </p>
        ) : (
          doc.pages.map((page) => (
            <div
              key={page.id}
              className="relative shrink-0 overflow-hidden rounded-[3px] bg-white shadow-canvas"
              style={{
                width: PAGE_WIDTH,
                height: PAGE_HEIGHT,
                background: page.background ?? "#ffffff",
              }}
            >
              {page.blocks.map((block) => (
                <BlockRenderer
                  key={block.id}
                  block={block}
                  documentId={doc.document_id}
                  editable={false}
                />
              ))}
            </div>
          ))
        )}
      </main>
    </div>
  );
}
