"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Loader2 } from "lucide-react";
import { AiAssistant } from "./AiAssistant";
import { BlocksPanel } from "./BlocksPanel";
import { Canvas } from "./Canvas";
import { CanvasToolbar } from "./CanvasToolbar";
import { EditorToolbar } from "./EditorToolbar";
import { LeftRail, type RailPanel } from "./LeftRail";
import { PageSidebar } from "./PageSidebar";
import { PropertiesPanel } from "./PropertiesPanel";
import { UploadsPanel } from "./UploadsPanel";
import { ApiError } from "@/lib/api/client";
import { aiEdit, exportPdf, getDocument, saveDocument } from "@/lib/api/documents";
import {
  approveCourse,
  getCourseActivity,
  getCourseReview,
  requestCourseChanges,
  submitForReview,
} from "@/lib/api/courses";
import { useAuth } from "@/lib/auth/auth-provider";
import { useEditor } from "@/lib/editor/store";
import type { CourseDocument } from "@/lib/types/document";
import type { CourseActivityEntry, CourseReview } from "@/lib/types/course";

export function CourseEditor({ documentId }: { documentId: string }) {
  const router = useRouter();
  const editor = useEditor();
  const { user } = useAuth();
  const [panel, setPanel] = useState<RailPanel>("pages");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);

  const [review, setReview] = useState<CourseReview | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);

  // null = not loaded / no access (e.g. viewer isn't the owner or admin) -
  // the toolbar hides the Activity button in that case rather than showing
  // an empty or broken list.
  const [activity, setActivity] = useState<CourseActivityEntry[] | null>(null);

  const [instruction, setInstruction] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [aiMessage, setAiMessage] = useState<string | null>(null);
  const [aiError, setAiError] = useState<string | null>(null);
  const assistantRef = useRef<HTMLInputElement>(null);

  const { load, sync, reconcile } = editor;

  // --- load the document --------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        const doc = await getDocument(documentId, controller.signal);
        if (!cancelled) load(doc);
      } catch (caught) {
        if (cancelled || caught instanceof DOMException) return;
        setLoadError(
          caught instanceof ApiError
            ? caught.message
            : "The course document could not be loaded.",
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [documentId, load]);

  // --- review/approval workflow --------------------------------------------
  const courseId = editor.document?.course_id ?? null;

  useEffect(() => {
    if (!courseId) return;
    let cancelled = false;
    getCourseReview(courseId)
      .then((info) => {
        if (!cancelled) setReview(info);
      })
      .catch(() => {
        // Review info is supplementary (e.g. filesystem/offline mode has no
        // review workflow at all) - a failure here shouldn't block editing.
      });
    return () => {
      cancelled = true;
    };
  }, [courseId]);

  // --- activity log ---------------------------------------------------------
  const refreshActivity = useCallback((id: string) => {
    getCourseActivity(id)
      .then((response) => setActivity(response.activities))
      .catch(() => setActivity(null)); // e.g. 403 - not the owner/admin
  }, []);

  useEffect(() => {
    if (courseId) refreshActivity(courseId);
  }, [courseId, refreshActivity]);

  const handleSubmitForReview = useCallback(async () => {
    if (!courseId) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      setReview(await submitForReview(courseId));
      refreshActivity(courseId);
    } catch (caught) {
      setReviewError(caught instanceof ApiError ? caught.message : "Could not submit for review.");
    } finally {
      setReviewBusy(false);
    }
  }, [courseId, refreshActivity]);

  const handleApprove = useCallback(async () => {
    if (!courseId) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      setReview(await approveCourse(courseId));
      refreshActivity(courseId);
    } catch (caught) {
      setReviewError(caught instanceof ApiError ? caught.message : "Could not approve the course.");
    } finally {
      setReviewBusy(false);
    }
  }, [courseId, refreshActivity]);

  const handleRequestChanges = useCallback(async () => {
    if (!courseId) return;
    const reason = window.prompt("What changes are needed?");
    if (reason === null) return; // cancelled
    if (!reason.trim()) {
      setReviewError("A reason is required when requesting changes.");
      return;
    }
    setReviewBusy(true);
    setReviewError(null);
    try {
      setReview(await requestCourseChanges(courseId, reason.trim()));
      refreshActivity(courseId);
    } catch (caught) {
      setReviewError(caught instanceof ApiError ? caught.message : "Could not request changes.");
    } finally {
      setReviewBusy(false);
    }
  }, [courseId, refreshActivity]);

  const handleReload = useCallback(async () => {
    setReloading(true);
    try {
      const doc = await getDocument(documentId);
      sync(doc);
    } catch (caught) {
      setLoadError(caught instanceof ApiError ? caught.message : "Reload failed.");
    } finally {
      setReloading(false);
    }
  }, [documentId, sync]);

  // --- save manual edits ----------------------------------------------------
  /** Persists the in-memory document as-is (no reflow - layout was already
   *  computed client-side) and adopts the server's response as the new
   *  baseline, so `dirty` clears and later saves version off the right base. */
  const saveManualEdits = useCallback(
    async (doc: CourseDocument) => {
      const saved = await saveDocument(documentId, doc);
      reconcile(saved);
      return saved;
    },
    [documentId, reconcile],
  );

  const handleSave = useCallback(async () => {
    if (!editor.document) return;
    setSaving(true);
    setSaveError(null);
    try {
      await saveManualEdits(editor.document);
      setJustSaved(true);
      if (courseId) refreshActivity(courseId);
    } catch (caught) {
      setSaveError(caught instanceof ApiError ? caught.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  }, [editor.document, saveManualEdits, courseId, refreshActivity]);

  useEffect(() => {
    if (!justSaved) return;
    const timer = setTimeout(() => setJustSaved(false), 2500);
    return () => clearTimeout(timer);
  }, [justSaved]);

  // --- AI edit ------------------------------------------------------------
  /**
   *  select blocks -> instruction -> POST /ai-edit -> patch -> apply locally
   *                -> reconcile with the backend's persisted document
   *
   * The backend applies and persists the patch itself, so after applying the
   * operations optimistically we adopt its copy (which also carries the
   * re-pagination and any regenerated image).
   */
  const runAiEdit = useCallback(
    async (text: string, blockIds?: string[]) => {
      const ids = blockIds ?? editor.selectedIds;
      if (ids.length === 0 || !text.trim()) return;
      const hadLocalEdits = editor.dirty;

      setAiBusy(true);
      setAiError(null);
      setAiMessage(null);
      try {
        const response = await aiEdit(documentId, {
          selected_block_ids: ids,
          instruction: text,
        });

        const result = editor.applyDocumentPatch(response.patch);

        if (!hadLocalEdits) {
          // Safe to adopt the server copy verbatim.
          const authoritative = await getDocument(documentId);
          reconcile(authoritative);
        }

        const appliedCount = result?.applied.length ?? response.applied_operations.length;
        const rejected = [
          ...(result?.rejected ?? []),
          ...response.rejected_operations,
        ].length;

        setAiMessage(
          [
            `${appliedCount} change${appliedCount === 1 ? "" : "s"} applied.`,
            rejected ? `${rejected} rejected.` : "",
            response.patch.reasoning,
            hadLocalEdits
              ? "Your unsaved manual edits were kept, so this page may differ from the backend copy."
              : "",
          ]
            .filter(Boolean)
            .join(" "),
        );
        setInstruction("");
      } catch (caught) {
        setAiError(
          caught instanceof ApiError ? caught.message : "The AI edit could not be applied.",
        );
      } finally {
        setAiBusy(false);
      }
    },
    [documentId, editor, reconcile],
  );

  const handleReplaceImage = useCallback(
    (blockId: string, description: string) => {
      void runAiEdit(`Replace this image with: ${description}`, [blockId]);
    },
    [runAiEdit],
  );

  // --- export -------------------------------------------------------------
  const handleExport = useCallback(async () => {
    setExporting(true);
    setSaveError(null);
    try {
      // The saved document is the single source of truth for both the editor
      // and the PDF - export a stale backend copy while edits are only in
      // the browser and the PDF wouldn't match what's on screen.
      if (editor.dirty && editor.document) {
        await saveManualEdits(editor.document);
      }
      const blob = await exportPdf(documentId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${(editor.document?.course_title ?? "course")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "")}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      if (courseId) refreshActivity(courseId);
    } catch (caught) {
      setAiError(caught instanceof ApiError ? caught.message : "PDF export failed.");
    } finally {
      setExporting(false);
    }
  }, [documentId, editor.dirty, editor.document, saveManualEdits, courseId, refreshActivity]);

  // --- keyboard shortcuts --------------------------------------------------
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target?.isContentEditable ||
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.tagName === "SELECT";

      const modifier = event.metaKey || event.ctrlKey;
      if (modifier && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) editor.redo();
        else editor.undo();
        return;
      }
      if (modifier && event.key.toLowerCase() === "y") {
        event.preventDefault();
        editor.redo();
        return;
      }
      if (typing) return;
      if ((event.key === "Delete" || event.key === "Backspace") && editor.selectedIds[0]) {
        event.preventDefault();
        editor.deleteBlock(editor.selectedIds[0]);
      }
      if (event.key === "Escape") editor.select([]);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [editor]);

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center gap-2 text-[13px] text-ink-500">
        <Loader2 size={16} className="animate-spin text-brand-600" />
        Loading course document…
      </div>
    );
  }

  if (loadError || !editor.document) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-3 px-6 text-center">
        <AlertTriangle size={20} className="text-danger" />
        <p className="text-[13px] text-ink">{loadError ?? "No document available."}</p>
        <button
          type="button"
          onClick={() => router.push("/")}
          className="text-[12.5px] font-medium text-brand-600 hover:underline"
        >
          Start a new course
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-white">
      <EditorToolbar
        onPreview={() => router.push(`/preview/${documentId}`)}
        onExport={handleExport}
        onReload={handleReload}
        onSave={handleSave}
        exporting={exporting}
        reloading={reloading}
        saving={saving}
        saveError={saveError}
        justSaved={justSaved}
        review={review}
        currentUserId={user?.id ?? null}
        currentUserRole={user?.role ?? null}
        reviewBusy={reviewBusy}
        reviewError={reviewError}
        onSubmitForReview={handleSubmitForReview}
        onApproveCourse={handleApprove}
        onRequestChanges={handleRequestChanges}
        activity={activity}
      />

      <div className="flex min-h-0 flex-1">
        <LeftRail
          panel={panel}
          onPanelChange={setPanel}
          onFocusAssistant={() => assistantRef.current?.focus()}
        />

        <div className="w-[128px] shrink-0 border-r border-line bg-white">
          {panel === "pages" ? <PageSidebar /> : null}
          {panel === "blocks" ? <BlocksPanel /> : null}
          {panel === "uploads" ? <UploadsPanel /> : null}
        </div>

        <div className="flex min-w-0 flex-1 flex-col">
          <CanvasToolbar />
          <div className="min-h-0 flex-1">
            <Canvas />
          </div>
          <AiAssistant
            ref={assistantRef}
            value={instruction}
            onChange={setInstruction}
            onSubmit={(text) => void runAiEdit(text)}
            busy={aiBusy}
            message={aiMessage}
            error={aiError}
          />
        </div>

        <PropertiesPanel onReplaceImage={handleReplaceImage} busy={aiBusy} />
      </div>
    </div>
  );
}
