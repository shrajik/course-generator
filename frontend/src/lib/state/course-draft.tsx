"use client";

/**
 * The Create Course form and the TOC editor are two screens over one draft.
 * The draft lives in React state and is mirrored to sessionStorage so a refresh
 * on the TOC screen does not lose the user's input. The backend remains the
 * source of truth from the moment the course is created.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { TemplateKind, TocItem } from "@/lib/types/course";

/** The inputs a drafted TOC was generated from - lets the Create Course
 * screen tell "still the same course, just fine-tuning" apart from "this is
 * actually a new/different course" and decide whether to re-draft. */
export interface TocDraftedFor {
  courseTitle: string;
  targetAudience: string;
  template: TemplateKind;
}

export interface CourseDraft {
  courseTitle: string;
  targetAudience: string;
  dos: string[];
  donts: string[];
  template: TemplateKind;
  toc: TocItem[];
  /** Null means `toc` was never AI-drafted (e.g. built by hand, or stale from
   * a previous course) - see CreateCourseForm's regeneration check. */
  tocDraftedFor: TocDraftedFor | null;
  /** Set once the course exists in the backend. */
  courseId: string | null;
  documentId: string | null;
}

export const DEFAULT_DOS = [
  "Use simple explanations",
  "Include practical examples",
  "Add exercises and quizzes",
  "Make it engaging",
];

export const DEFAULT_DONTS = [
  "Don't use too much jargon",
  "Don't make it too theoretical",
  "Don't skip fundamentals",
  "Don't overload with code",
];

export const EMPTY_DRAFT: CourseDraft = {
  courseTitle: "",
  targetAudience: "",
  dos: DEFAULT_DOS,
  donts: DEFAULT_DONTS,
  template: "technical",
  toc: [],
  tocDraftedFor: null,
  courseId: null,
  documentId: null,
};

const STORAGE_KEY = "course-creator:draft";

interface CourseDraftContextValue {
  draft: CourseDraft;
  hydrated: boolean;
  update: (patch: Partial<CourseDraft>) => void;
  reset: () => void;
}

const CourseDraftContext = createContext<CourseDraftContextValue | null>(null);

export function CourseDraftProvider({ children }: { children: React.ReactNode }) {
  const [draft, setDraft] = useState<CourseDraft>(EMPTY_DRAFT);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    try {
      const stored = window.sessionStorage.getItem(STORAGE_KEY);
      if (stored) setDraft({ ...EMPTY_DRAFT, ...(JSON.parse(stored) as CourseDraft) });
    } catch {
      /* corrupt storage is not worth surfacing */
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
    } catch {
      /* storage may be unavailable in private mode */
    }
  }, [draft, hydrated]);

  const update = useCallback((patch: Partial<CourseDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
  }, []);

  const reset = useCallback(() => setDraft(EMPTY_DRAFT), []);

  const value = useMemo(
    () => ({ draft, hydrated, update, reset }),
    [draft, hydrated, update, reset],
  );

  return <CourseDraftContext.Provider value={value}>{children}</CourseDraftContext.Provider>;
}

export function useCourseDraft(): CourseDraftContextValue {
  const context = useContext(CourseDraftContext);
  if (!context) throw new Error("useCourseDraft must be used inside CourseDraftProvider");
  return context;
}
