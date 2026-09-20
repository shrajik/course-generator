import type { ActivityAction } from "@/lib/types/course";

export const ACTIVITY_LABELS: Record<ActivityAction, string> = {
  created: "created the course",
  updated: "saved changes",
  submitted_for_review: "submitted for review",
  changes_requested: "requested changes",
  approved: "approved the course",
  exported: "exported the PDF",
};

export function formatActivityTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
