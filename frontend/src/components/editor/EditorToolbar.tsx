"use client";

import { useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  Download,
  Eye,
  History,
  Loader2,
  MoreHorizontal,
  RefreshCw,
  Redo2,
  Save,
  Send,
  ThumbsDown,
  ThumbsUp,
  Undo2,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { useEditor } from "@/lib/editor/store";
import { asString } from "@/lib/editor/blocks";
import { cn } from "@/lib/utils/cn";
import type { Role } from "@/lib/types/auth";
import type { ActivityAction, CourseActivityEntry, CourseReview, ReviewStatus } from "@/lib/types/course";

interface EditorToolbarProps {
  onPreview: () => void;
  onExport: () => void;
  onReload: () => void;
  onSave: () => void;
  exporting: boolean;
  reloading: boolean;
  saving: boolean;
  saveError: string | null;
  justSaved: boolean;
  review: CourseReview | null;
  currentUserId: string | null;
  currentUserRole: Role | null;
  reviewBusy: boolean;
  reviewError: string | null;
  onSubmitForReview: () => void;
  onApproveCourse: () => void;
  onRequestChanges: () => void;
  activity: CourseActivityEntry[] | null;
}

const REVIEW_STATUS_LABELS: Record<ReviewStatus, string> = {
  draft: "Draft",
  in_review: "In Review",
  changes_requested: "Changes Requested",
  approved: "Approved",
};

const REVIEW_STATUS_CLASSES: Record<ReviewStatus, string> = {
  draft: "border-line bg-canvas text-ink-500",
  in_review: "border-blue-200 bg-blue-50 text-blue-800",
  changes_requested: "border-amber-200 bg-amber-50 text-amber-800",
  approved: "border-emerald-200 bg-emerald-50 text-emerald-800",
};

const ACTIVITY_LABELS: Record<ActivityAction, string> = {
  created: "created the course",
  updated: "saved changes",
  submitted_for_review: "submitted for review",
  changes_requested: "requested changes",
  approved: "approved the course",
  exported: "exported the PDF",
};

function formatActivityTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

interface ChapterEntry {
  id: string;
  number: number;
  title: string;
  pageIndex: number;
}

export function EditorToolbar({
  onPreview,
  onExport,
  onReload,
  onSave,
  exporting,
  reloading,
  saving,
  saveError,
  justSaved,
  review,
  currentUserId,
  currentUserRole,
  reviewBusy,
  reviewError,
  onSubmitForReview,
  onApproveCourse,
  onRequestChanges,
  activity,
}: EditorToolbarProps) {
  const editor = useEditor();
  const { document: doc, activePageIndex } = editor;
  const [menuOpen, setMenuOpen] = useState(false);
  const [chapterOpen, setChapterOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);

  // --- review/approval workflow --------------------------------------------
  const isOwner = Boolean(review?.owner_id && review.owner_id === currentUserId);
  const isReviewerRole = currentUserRole === "editor_reviewer" || currentUserRole === "admin";
  const canSubmitForReview =
    isOwner && (review?.review_status === "draft" || review?.review_status === "changes_requested");
  const canReviewNow = isReviewerRole && review?.review_status === "in_review";

  /** Chapter list derived from the document itself, not hardcoded. */
  const chapters = useMemo<ChapterEntry[]>(() => {
    if (!doc) return [];
    const found = new Map<string, ChapterEntry>();
    doc.pages.forEach((page, pageIndex) => {
      page.blocks.forEach((block) => {
        const id = block.meta.chapter_id;
        if (!id || found.has(id)) return;
        if (block.type !== "heading") return;
        found.set(id, {
          id,
          number: block.meta.chapter_number ?? found.size + 1,
          title: asString(block.content.text),
          pageIndex,
        });
      });
    });
    return [...found.values()].sort((a, b) => a.number - b.number);
  }, [doc]);

  const current =
    [...chapters].reverse().find((chapter) => chapter.pageIndex <= activePageIndex) ?? null;

  return (
    <header className="flex items-center gap-3 border-b border-line bg-white px-4 py-2.5">
      <div className="flex items-center gap-2.5">
        <span className="flex h-6 w-6 items-center justify-center rounded-[7px] bg-brand-600 text-[12px] font-semibold text-white">
          4
        </span>
        <h1 className="text-[14px] font-bold tracking-[-0.01em] text-ink">Course Editor</h1>
      </div>

      {chapters.length > 0 ? (
        <div className="relative ml-3">
          <button
            type="button"
            onClick={() => setChapterOpen((open) => !open)}
            className="flex items-center gap-2 rounded-[8px] px-2 py-1 text-left transition-colors hover:bg-brand-50"
          >
            <span className="text-[11.5px] font-medium text-ink-400">
              Chapter {String(current?.number ?? 1).padStart(2, "0")}
            </span>
            <span className="max-w-[240px] truncate text-[12.5px] font-semibold text-ink">
              {current?.title ?? doc?.course_title}
            </span>
            <ChevronDown size={13} className="text-ink-400" />
          </button>
          {chapterOpen ? (
            <div className="absolute left-0 top-9 z-30 w-72 overflow-hidden rounded-[10px] border border-line bg-white py-1 shadow-pop">
              {chapters.map((chapter) => (
                <button
                  key={chapter.id}
                  type="button"
                  onClick={() => {
                    editor.setPage(chapter.pageIndex);
                    setChapterOpen(false);
                  }}
                  className={cn(
                    "flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] hover:bg-brand-50",
                    chapter.id === current?.id ? "text-brand-700" : "text-ink-700",
                  )}
                >
                  <span className="w-6 shrink-0 tabular-nums text-ink-400">
                    {String(chapter.number).padStart(2, "0")}
                  </span>
                  <span className="truncate">{chapter.title}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {review ? (
        <div className="ml-3 flex items-center gap-2">
          <span
            className={cn(
              "rounded-full border px-2 py-0.5 text-[10.5px] font-medium",
              REVIEW_STATUS_CLASSES[review.review_status],
            )}
          >
            {REVIEW_STATUS_LABELS[review.review_status]}
          </span>
          {review.review_status === "changes_requested" && review.review_comment ? (
            <span
              className="max-w-[280px] truncate text-[11.5px] text-amber-800"
              title={review.review_comment}
            >
              “{review.review_comment}”
            </span>
          ) : null}
        </div>
      ) : null}

      <div className="ml-auto flex items-center gap-2">
        {reviewError ? (
          <span
            className="rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-[10.5px] font-medium text-red-700"
            title={reviewError}
          >
            Review action failed
          </span>
        ) : null}

        {canSubmitForReview ? (
          <Button variant="outline" size="sm" onClick={onSubmitForReview} disabled={reviewBusy}>
            {reviewBusy ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
            Submit for Review
          </Button>
        ) : null}

        {canReviewNow ? (
          <>
            <Button variant="outline" size="sm" onClick={onRequestChanges} disabled={reviewBusy}>
              <ThumbsDown size={13} />
              Request Changes
            </Button>
            <Button size="sm" onClick={onApproveCourse} disabled={reviewBusy}>
              {reviewBusy ? <Loader2 size={13} className="animate-spin" /> : <ThumbsUp size={13} />}
              Approve
            </Button>
          </>
        ) : null}

        {saveError ? (
          <span
            className="rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-[10.5px] font-medium text-red-700"
            title={saveError}
          >
            Save failed
          </span>
        ) : editor.dirty ? (
          <span
            className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10.5px] font-medium text-amber-800"
            title="You have unsaved manual edits. Click Save to persist them - Export PDF saves automatically first."
          >
            Unsaved changes
          </span>
        ) : justSaved ? (
          <span className="flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10.5px] font-medium text-emerald-800">
            <Check size={11} />
            Saved
          </span>
        ) : null}

        <Button
          variant="outline"
          size="sm"
          onClick={onSave}
          disabled={!editor.dirty || saving || exporting}
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
          Save
        </Button>

        <div className="flex items-center rounded-[8px] border border-line">
          <IconButton
            aria-label="Undo"
            disabled={!editor.canUndo}
            onClick={editor.undo}
            className="rounded-r-none"
          >
            <Undo2 size={14} />
          </IconButton>
          <span className="h-5 w-px bg-line" aria-hidden />
          <IconButton
            aria-label="Redo"
            disabled={!editor.canRedo}
            onClick={editor.redo}
            className="rounded-l-none"
          >
            <Redo2 size={14} />
          </IconButton>
        </div>

        <Button variant="outline" size="sm" onClick={onPreview}>
          <Eye size={13} />
          Preview
        </Button>

        <Button size="sm" onClick={onExport} disabled={exporting || saving}>
          {exporting ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
          Export PDF
        </Button>

        {activity ? (
          <div className="relative">
            <IconButton
              aria-label="Activity history"
              active={activityOpen}
              onClick={() => setActivityOpen((open) => !open)}
            >
              <History size={15} />
            </IconButton>
            {activityOpen ? (
              <div className="absolute right-0 top-8 z-30 w-80 overflow-hidden rounded-[10px] border border-line bg-white shadow-pop">
                <div className="border-b border-line px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                  Activity
                </div>
                {activity.length === 0 ? (
                  <p className="px-3 py-3 text-[12px] text-ink-400">No activity yet.</p>
                ) : (
                  <ul className="max-h-72 overflow-y-auto">
                    {activity.map((entry) => (
                      <li
                        key={entry.id}
                        className="border-b border-line px-3 py-2 text-[12px] last:border-0"
                      >
                        <p className="text-ink-700">
                          <span className="font-medium text-ink">
                            {entry.user_email ?? "Someone"}
                          </span>{" "}
                          {ACTIVITY_LABELS[entry.action]}
                        </p>
                        {entry.message ? (
                          <p className="mt-0.5 truncate text-ink-500" title={entry.message}>
                            “{entry.message}”
                          </p>
                        ) : null}
                        <p className="mt-0.5 text-[11px] text-ink-400">
                          {formatActivityTime(entry.created_at)}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="relative">
          <IconButton
            aria-label="More options"
            active={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <MoreHorizontal size={15} />
          </IconButton>
          {menuOpen ? (
            <div className="absolute right-0 top-8 z-30 w-60 overflow-hidden rounded-[10px] border border-line bg-white shadow-pop">
              <button
                type="button"
                onClick={() => {
                  onReload();
                  setMenuOpen(false);
                }}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-ink-700 hover:bg-brand-50"
              >
                {reloading ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <RefreshCw size={12} />
                )}
                Reload from backend
              </button>
              <div className="border-t border-line px-3 py-2 text-[11px] text-ink-400">
                <p>Document {doc?.document_id}</p>
                <p className="mt-0.5">
                  Version {doc?.version} · {doc?.pages.length} pages
                </p>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
