"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowRight, Clock3, FileClock, Loader2 } from "lucide-react";
import { AdminEmpty } from "@/components/admin/AdminUI";
import { getWorkspaceActivity } from "@/lib/api/courses";
import { ApiError } from "@/lib/api/client";
import { ACTIVITY_LABELS, formatActivityTime } from "@/lib/activity";
import type { WorkspaceActivityEntry } from "@/lib/types/course";

export default function HistoryPage() {
  const [activities, setActivities] = useState<WorkspaceActivityEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getWorkspaceActivity()
      .then((response) => setActivities(response.activities))
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setError(caught instanceof ApiError ? caught.message : "Could not load activity.");
        setActivities([]);
      });
    return () => controller.abort();
  }, []);

  return (
    <div className="creator-page mx-auto w-full max-w-[1120px]">
      <div className="creator-page-heading">
        <div>
          <p className="creator-eyebrow">Workspace</p>
          <h1 className="creator-page-title">History</h1>
          <p className="creator-page-description">
            Review generation and editing activity from your courses.
          </p>
        </div>
        <Clock3 size={22} className="text-brand-500" />
      </div>

      <section className="creator-content-panel mt-5">
        {activities === null ? (
          <div className="flex items-center justify-center gap-2 py-16 text-[13px] text-ink-400">
            <Loader2 size={16} className="animate-spin" />
            Loading activity…
          </div>
        ) : activities.length === 0 ? (
          <AdminEmpty>
            <span className="creator-empty-icon">
              <FileClock size={22} />
            </span>
            <h2 className="text-[15px] font-semibold text-ink">No history to show yet</h2>
            <p className="max-w-sm text-[12px] leading-5 text-ink-500">
              {error ??
                "Once you create or edit a course, its activity will show up here."}
            </p>
            <Link href="/" className="creator-text-link">
              Create your first course <ArrowRight size={13} />
            </Link>
          </AdminEmpty>
        ) : (
          <ul className="divide-y divide-line">
            {activities.map((entry) => (
              <li key={entry.id} className="flex items-start gap-3 px-5 py-3.5">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-400" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] text-ink-700">
                    <span className="font-medium text-ink">{entry.user_email ?? "Someone"}</span>{" "}
                    {ACTIVITY_LABELS[entry.action]} in{" "}
                    <Link
                      href={`/editor/${entry.document_id}`}
                      className="font-medium text-brand-600 hover:underline"
                    >
                      {entry.course_title}
                    </Link>
                  </p>
                  {entry.message ? (
                    <p className="mt-0.5 truncate text-[12px] text-ink-500" title={entry.message}>
                      “{entry.message}”
                    </p>
                  ) : null}
                  <p className="mt-0.5 text-[11.5px] text-ink-400">
                    {formatActivityTime(entry.created_at)}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
