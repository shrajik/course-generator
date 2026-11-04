"use client";

import { useEffect, useState } from "react";
import { AdminLoading, AdminPageHeader } from "@/components/admin/AdminUI";
import { KnowledgeWorkspace } from "@/components/admin/knowledge/KnowledgeWorkspace";
import { LegacyKnowledge } from "@/components/admin/knowledge/LegacyKnowledge";
import { kbStatus } from "@/lib/api/kb";

/**
 * The knowledge base. Courses read either the older file library or the new PostgreSQL knowledge base,
 * whichever the backend is switched to (KB_BACKEND). The screen shows the one courses actually use, so an
 * admin never edits a library nothing reads. `?view=new` opens the new one early, to try it before the switch.
 */
export default function AdminKnowledgePage() {
  const [backend, setBackend] = useState<"files" | "postgres" | null>(null);
  const [preview, setPreview] = useState(false);

  useEffect(() => {
    setPreview(new URLSearchParams(window.location.search).get("view") === "new");
    kbStatus().then((status) => setBackend(status.backend)).catch(() => setBackend("files"));
  }, []);

  if (backend === null) return <AdminLoading label="Loading the knowledge base" />;
  const modern = backend === "postgres" || preview;
  return modern ? (
    <div className="space-y-5">
      <AdminPageHeader
        eyebrow="Knowledge base"
        title="Knowledge"
        description={
          backend === "postgres"
            ? "Everything courses can draw on. Only published knowledge is used; drafts wait for your review."
            : "Preview of the new knowledge base. Courses still read the older file library until the backend is switched over, so what you publish here is not used yet."
        }
      />
      <KnowledgeWorkspace />
    </div>
  ) : (
    <LegacyKnowledge />
  );
}
