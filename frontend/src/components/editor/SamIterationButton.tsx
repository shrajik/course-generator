"use client";

import { useEffect, useState } from "react";
import { Loader2, Repeat } from "lucide-react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ApiError } from "@/lib/api/client";
import { getCourse } from "@/lib/api/courses";
import { generateCourse } from "@/lib/api/generation";
import { useEditor } from "@/lib/editor/store";

/**
 * SAM builds one module per iteration and waits for review feedback before the
 * next. Shown only for a SAM course that still has modules to build: the person
 * records their feedback on the draft, and the next module is generated.
 */
export function SamIterationButton() {
  const editor = useEditor();
  const router = useRouter();
  const courseId = editor.document?.course_id ?? null;
  const [progress, setProgress] = useState<{ built: number; total: number } | null>(null);
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!courseId) return;
    let cancelled = false;
    getCourse(courseId)
      .then((detail) => {
        if (cancelled) return;
        const design = detail.course.input.design_models;
        if (!design?.models.includes("sam")) return setProgress(null);
        const chapters = detail.course.chapters;
        setProgress({ built: chapters.filter((c) => c.written).length, total: chapters.length });
      })
      .catch(() => setProgress(null));
    return () => {
      cancelled = true;
    };
  }, [courseId]);

  if (!courseId || !progress || progress.built >= progress.total) return null;

  const submit = async () => {
    if (!feedback.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await generateCourse(courseId, { sam_feedback: feedback.trim() });
      router.push(`/generate/${courseId}`);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The next iteration could not start.");
      setBusy(false);
    }
  };

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <Repeat size={13} />
        Next iteration ({progress.built + 1} of {progress.total})
      </Button>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={`SAM iteration ${progress.built + 1}`}
        subtitle="Record your feedback on the draft so far. The next module is built after it."
        footer={
          <Button size="sm" onClick={() => void submit()} disabled={!feedback.trim() || busy}>
            {busy ? <Loader2 size={13} className="animate-spin" /> : null}
            Build next module
          </Button>
        }
      >
        <label htmlFor="sam-feedback" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
          Feedback on the previous iteration
        </label>
        <textarea
          id="sam-feedback"
          rows={5}
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
          placeholder="Answer the draft's open questions: tone, depth, structure, activity type..."
          className="w-full rounded-lg border border-line px-3 py-2 text-[13px] outline-none focus:border-brand-400"
        />
        {error ? <p className="mt-2 text-[12.5px] text-red-700">{error}</p> : null}
      </Modal>
    </>
  );
}
