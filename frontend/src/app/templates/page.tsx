"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, CheckCircle2, FileText, Layers3, Loader2, Sparkles, Upload } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  classifyTemplateType,
  listCourseTemplateDocuments,
  uploadCourseTemplateDocument,
  type CourseTemplateDocumentSummary,
  type CourseTemplateType,
} from "@/lib/api/course-templates";
import { AdminAlert, AdminEmpty, AdminLoading } from "@/components/admin/AdminUI";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useAuth } from "@/lib/auth/auth-provider";
import { useCourseDraft } from "@/lib/state/course-draft";

const DEFAULT_TEMPLATE_ID = "__default__";

function templateTypeLabel(type: CourseTemplateType): string {
  return type === "technical" ? "Technical" : "Non-Technical";
}

export default function TemplatesPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selecting = searchParams.get("select") === "1";
  const { user } = useAuth();
  const canUpload = Boolean(user);
  const { draft, update } = useCourseDraft();

  const [templates, setTemplates] = useState<CourseTemplateDocumentSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [classifying, setClassifying] = useState(false);

  const load = () => {
    setLoading(true);
    listCourseTemplateDocuments()
      .then((items) => setTemplates(items))
      .catch((caught) => setError(caught instanceof ApiError ? caught.message : "Templates could not be loaded."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleUse = (template: CourseTemplateDocumentSummary) => {
    update({
      selectedTemplate: { id: template.id, name: template.name, templateType: template.template_type },
      template: template.template_type,
    });
    router.push("/");
  };

  /**
   * The Default Template has no type of its own - if the title (and
   * audience) are already typed, resolve it now so the "Selected Template"
   * card on Create Course shows the real answer immediately. If not typed
   * yet, store it as "auto" and CreateCourseForm resolves it right before
   * Continue, once a title is guaranteed to exist.
   */
  const handleUseDefault = async () => {
    const courseTitle = draft.courseTitle.trim();
    if (!courseTitle) {
      update({ selectedTemplate: { id: DEFAULT_TEMPLATE_ID, name: "Default Template", templateType: "auto" } });
      router.push("/");
      return;
    }
    setClassifying(true);
    try {
      const classification = await classifyTemplateType({
        courseTitle,
        targetAudience: draft.targetAudience,
      });
      update({
        selectedTemplate: {
          id: DEFAULT_TEMPLATE_ID,
          name: "Default Template",
          templateType: classification.template_type,
        },
        template: classification.template_type,
      });
      router.push("/");
    } catch {
      // Don't block the user on a classification hiccup - fall back to
      // "auto" so CreateCourseForm retries right before Continue.
      update({ selectedTemplate: { id: DEFAULT_TEMPLATE_ID, name: "Default Template", templateType: "auto" } });
      router.push("/");
    } finally {
      setClassifying(false);
    }
  };

  return (
    <div className="creator-page mx-auto w-full max-w-[1120px]">
      <div className="creator-page-heading">
        <div>
          <p className="creator-eyebrow">Workspace</p>
          <h1 className="creator-page-title">Templates</h1>
          <p className="creator-page-description">
            {selecting
              ? "Choose a template to use for the course you're creating, or go back and start from scratch."
              : "Reference documents your team can start a new course from."}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {selecting ? (
            <Button variant="outline" size="sm" onClick={() => router.push("/")}>
              Back to Create Course
            </Button>
          ) : null}
          {canUpload ? (
            <Button size="sm" onClick={() => setUploadOpen(true)}>
              <Upload size={13} />
              Upload Template
            </Button>
          ) : null}
        </div>
      </div>

      <section className="mt-5">
        {error ? <AdminAlert>{error}</AdminAlert> : null}

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <article className="creator-template-card flex flex-col gap-3 border-brand-200 bg-brand-50/40">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2.5">
                <span className="creator-list-icon bg-brand-100 text-brand-600">
                  <Sparkles size={16} />
                </span>
                <div>
                  <h3 className="text-[13.5px] font-semibold text-ink">Default Template</h3>
                  <span className="mt-0.5 inline-block rounded-full bg-brand-100 px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-brand-600">
                    AI-matched
                  </span>
                </div>
              </div>
            </div>
            <p className="text-[12px] leading-5 text-ink-500">
              Always available - no upload needed. We automatically detect whether your course is
              Technical or Non-Technical from its title and audience.
            </p>
            <div className="mt-auto flex items-center justify-end pt-1">
              <Button size="sm" onClick={() => void handleUseDefault()} disabled={classifying}>
                {classifying ? (
                  <>
                    <Loader2 size={13} className="animate-spin" />
                    Detecting…
                  </>
                ) : (
                  <>
                    Use Template
                    <ArrowRight size={13} />
                  </>
                )}
              </Button>
            </div>
          </article>
        </div>

        <div className="mt-5">
          {loading ? (
            <div className="creator-content-panel">
              <AdminLoading label="Loading templates…" />
            </div>
          ) : templates.length === 0 ? (
            <div className="creator-content-panel">
              <AdminEmpty>
                <span className="creator-empty-icon">
                  <Layers3 size={22} />
                </span>
                <h2 className="text-[15px] font-semibold text-ink">No uploaded templates yet</h2>
                <p className="max-w-sm text-[12px] leading-5 text-ink-500">
                  {canUpload
                    ? "Upload a DOCX or Markdown reference document so others can start new courses from it - or use the Default Template above."
                    : "No one has uploaded a reference template yet - the Default Template above still works."}
                </p>
              </AdminEmpty>
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {templates.map((template) => (
              <article key={template.id} className="creator-template-card flex flex-col gap-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2.5">
                    <span className="creator-list-icon">
                      <FileText size={16} />
                    </span>
                    <div>
                      <h3 className="text-[13.5px] font-semibold text-ink">{template.name}</h3>
                      <span className="mt-0.5 inline-block rounded-full bg-brand-50 px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-brand-600">
                        {templateTypeLabel(template.template_type)}
                      </span>
                    </div>
                  </div>
                </div>
                {template.description ? (
                  <p className="line-clamp-3 text-[12px] leading-5 text-ink-500">{template.description}</p>
                ) : (
                  <p className="text-[12px] italic leading-5 text-ink-400">No description provided.</p>
                )}
                <div className="mt-auto flex items-center justify-between pt-1">
                  <span className="text-[11px] text-ink-400">
                    Source: {template.source_format === "docx" ? "DOCX" : "Markdown"}
                  </span>
                  <Button size="sm" onClick={() => handleUse(template)}>
                    {selecting ? "Use Template" : "Use Template"}
                    <ArrowRight size={13} />
                  </Button>
                </div>
              </article>
              ))}
            </div>
          )}
        </div>
      </section>

      {canUpload ? (
        <UploadTemplateModal
          open={uploadOpen}
          onClose={() => setUploadOpen(false)}
          onUploaded={() => {
            setUploadOpen(false);
            load();
          }}
        />
      ) : null}
    </div>
  );
}

function UploadTemplateModal({
  open,
  onClose,
  onUploaded,
}: {
  open: boolean;
  onClose: () => void;
  onUploaded: () => void;
}) {
  const [name, setName] = useState("");
  const [templateType, setTemplateType] = useState<CourseTemplateType>("technical");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setName("");
      setTemplateType("technical");
      setDescription("");
      setFile(null);
      setError(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }, [open]);

  const canSubmit = name.trim().length > 0 && Boolean(file) && !busy;

  const handleSubmit = async () => {
    if (!file || !canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      await uploadCourseTemplateDocument({ name: name.trim(), templateType, description: description.trim(), file });
      onUploaded();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not upload the template.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Upload Template"
      subtitle="Upload DOCX or Markdown template"
      footer={
        <>
          <Button variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={handleSubmit} disabled={!canSubmit}>
            {busy ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                Uploading…
              </>
            ) : (
              <>
                <Sparkles size={13} />
                Upload
              </>
            )}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div>
          <label htmlFor="template-name" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
            Template Name
          </label>
          <input
            id="template-name"
            className="field"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Underwriting Essentials"
          />
        </div>

        <div>
          <label htmlFor="template-type" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
            Template Type
          </label>
          <select
            id="template-type"
            className="field"
            value={templateType}
            onChange={(event) => setTemplateType(event.target.value as CourseTemplateType)}
          >
            <option value="technical">Technical</option>
            <option value="non_technical">Non-Technical</option>
          </select>
        </div>

        <div>
          <label htmlFor="template-description" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
            Description <span className="text-ink-400">(optional)</span>
          </label>
          <textarea
            id="template-description"
            className="field min-h-[72px] resize-y"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="What this template is for and when to use it…"
          />
        </div>

        <div>
          <label htmlFor="template-file" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
            Template File
          </label>
          <input
            id="template-file"
            ref={fileInputRef}
            type="file"
            accept=".docx,.md,.markdown"
            className="field"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <p className="mt-1.5 text-[11px] text-ink-400">Upload DOCX or Markdown template</p>
        </div>

        {error ? (
          <p className="rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-900">
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
