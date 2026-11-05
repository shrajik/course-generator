"use client";

import { useEffect, useState } from "react";
import { BadgeCheck, BookOpen, FileText, Loader2 } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { getPassagesByRefs, type Passage } from "@/lib/api/review";
import { ORIGIN_CLASSES, originLabel, originOf, type CitationOrigin } from "@/lib/editor/citations";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";

/**
 * The passage behind a citation such as [G2.5]: which document it comes from
 * (the organisation's knowledge library or the creator's own upload), where
 * in that document, and the passage itself.
 */
const ORIGIN_ICON: Record<CitationOrigin, typeof BookOpen> = { knowledge: BookOpen, memory: BadgeCheck, upload: FileText };
const NOT_FOUND: Record<CitationOrigin, string> = {
  knowledge: "This passage is no longer in the knowledge library - the document may have been switched off or removed.",
  memory: "This approved item is no longer available - it may have been archived.",
  upload: "This passage could not be found in the course's uploaded sources.",
};
const ORIGIN_NOTE: Record<CitationOrigin, string> = {
  knowledge: "From the organisation's knowledge library, which every course searches while it is written.",
  memory: "From material approved in an earlier course. It is locked: an expert signed it off, and it is offered to similar new courses.",
  upload: "From a document uploaded for this course.",
};

export function PassageDialog({
  courseId,
  refId,
  onClose,
}: {
  courseId: string | null;
  refId: string | null;
  onClose: () => void;
}) {
  const [passage, setPassage] = useState<Passage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!refId || !courseId) return;
    let cancelled = false;
    setLoading(true);
    setPassage(null);
    setError(null);
    getPassagesByRefs(courseId, [refId])
      .then((found) => {
        if (cancelled) return;
        if (found.length) setPassage(found[0]);
        else
          setError(NOT_FOUND[originOf(refId)]);
      })
      .catch((caught) => !cancelled && setError(caught instanceof ApiError ? caught.message : "The passage could not be loaded."))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [courseId, refId]);

  const origin = refId ? originOf(refId) : "knowledge";
  const Icon = ORIGIN_ICON[origin];

  return (
    <Modal
      open={refId !== null}
      onClose={onClose}
      title={`Where this comes from [${refId ?? ""}]`}
      subtitle={originLabel(origin)}
      width="max-w-xl"
      footer={
        <Button size="sm" variant="outline" onClick={onClose}>
          Close
        </Button>
      }
    >
      {loading ? (
        <p className="flex items-center gap-2 text-[12.5px] text-ink-500">
          <Loader2 size={14} className="animate-spin" /> Loading the passage…
        </p>
      ) : error ? (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-[12.5px] text-amber-800">{error}</p>
      ) : passage ? (
        <div className="space-y-3">
          <div className="flex items-start gap-2.5">
            <span className={`rounded-md p-1.5 ${ORIGIN_CLASSES[origin].chip}`}>
              <Icon size={15} />
            </span>
            <div>
              <p className="text-[13px] font-semibold text-ink">{passage.filename}</p>
              <p className="text-[11.5px] text-ink-500">
                {[passage.page ? `Page ${passage.page}` : null, passage.heading || null, `passage ${passage.ref}`]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            </div>
          </div>
          <blockquote className="whitespace-pre-wrap rounded-lg border-l-4 border-line bg-canvas px-3 py-2.5 text-[12.5px] leading-5 text-ink-700">
            {passage.text}
          </blockquote>
          <p className="text-[11px] leading-4 text-ink-400">
            {ORIGIN_NOTE[origin]}{" "}
            The AI was given this passage while writing and cited it here.
          </p>
        </div>
      ) : null}
    </Modal>
  );
}
