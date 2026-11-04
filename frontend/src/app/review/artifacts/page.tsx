"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { ArtifactMemory } from "@/components/admin/artifacts/ArtifactMemory";
import { AdminLoading } from "@/components/admin/AdminUI";
import { useAuth } from "@/lib/auth/auth-provider";

/**
 * Artifact review for subject-matter experts: the queue of generated chapters, visuals and question sets
 * waiting for approval, and everything already approved. Same people who approve a course (reviewers and
 * admins); archiving stays an admin action. A static route, so it takes precedence over /review/[documentId].
 */
export default function ReviewArtifactsPage() {
  const router = useRouter();
  const { user, isLoading } = useAuth();

  useEffect(() => {
    if (!isLoading && !user) router.replace("/login");
  }, [isLoading, user, router]);

  if (isLoading || !user) return <AdminLoading label="Loading" />;
  const allowed = user.role === "editor_reviewer" || user.role === "admin";
  return (
    <main className="mx-auto max-w-[1200px] space-y-4 px-6 py-6">
      <header>
        <p className="text-[12px] font-medium uppercase tracking-wide text-brand-700">Artifact review</p>
        <h1 className="text-[22px] font-semibold text-ink">Approved artifacts</h1>
        <p className="mt-1 text-[13px] text-ink-500">
          Approve generated chapters, visuals and question sets so future courses can reuse them. An approved version is locked; a change is a new version that needs its own approval.
        </p>
        <Link href="/courses" className="mt-1 inline-block text-[12.5px] text-brand-700 underline">Back to courses</Link>
      </header>
      {allowed ? (
        <ArtifactMemory canManage={user.role === "admin"} />
      ) : (
        <p className="rounded-[8px] border border-line bg-white p-4 text-[13px] text-ink-700">Artifact review is for reviewers and admins.</p>
      )}
    </main>
  );
}
