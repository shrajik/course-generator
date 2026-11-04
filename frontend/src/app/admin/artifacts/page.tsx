"use client";

import { AdminPageHeader } from "@/components/admin/AdminUI";
import { ArtifactMemory } from "@/components/admin/artifacts/ArtifactMemory";

export default function AdminArtifactsPage() {
  return (
    <div className="space-y-5">
      <AdminPageHeader
        eyebrow="Artifact memory"
        title="Approved artifacts"
        description="Chapters, visuals, questions and activities that AI generated and an expert approved. Approved versions are locked and reused by future courses; sent-back and pending ones never are."
      />
      <ArtifactMemory />
    </div>
  );
}
