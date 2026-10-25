"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  AlertTriangle,
  ArrowRight,
  Building2,
  Eye,
  FileText,
  Globe2,
  Layers3,
  Loader2,
  Lock,
  Sparkles,
  Trash2,
  Upload,
} from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { listTemplates, type TemplateSummary } from "@/lib/api/courses";
import {
  classifyTemplateType,
  deleteCourseTemplateDocument,
  listCourseTemplateDocuments,
  setCourseTemplateVisibility,
  uploadCourseTemplateDocument,
  type CourseTemplateDocumentSummary,
  type CourseTemplateType,
  type TemplateVisibility,
} from "@/lib/api/course-templates";
import { AdminAlert, AdminEmpty, AdminLoading } from "@/components/admin/AdminUI";
import { BlueprintPreview } from "@/components/templates/BlueprintPreview";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useAuth } from "@/lib/auth/auth-provider";
import { useCourseDraft } from "@/lib/state/course-draft";

const DEFAULT_TEMPLATE_ID = "__default__";

function templateTypeLabel(type: string): string {
  return type === "technical" ? "Technical" : "Non-Technical";
}

function asTemplateType(kind: string): CourseTemplateType {
  return kind === "technical" ? "technical" : "non_technical";
}

export default function TemplatesPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selecting = searchParams.get("select") === "1";
  const { user } = useAuth();
  const canUpload = Boolean(user);
  const { draft, update } = useCourseDraft();

  const [templates, setTemplates] = useState<CourseTemplateDocumentSummary[]>([]);
  const [adminTemplates, setAdminTemplates] = useState<TemplateSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [classifying, setClassifying] = useState(false);
  const [previewing, setPreviewing] = useState<CourseTemplateDocumentSummary | null>(null);
  const [deleting, setDeleting] = useState<CourseTemplateDocumentSummary | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    Promise.all([
      listCourseTemplateDocuments(),
      // Admin-made templates; a failure here must not hide the uploads.
      listTemplates()
        .then((response) => response.templates.filter((template) => template.source === "admin"))
        .catch(() => [] as TemplateSummary[]),
    ])
      .then(([uploaded, admin]) => {
        setTemplates(uploaded);
        setAdminTemplates(admin);
      })
      .catch((caught) => setError(caught instanceof ApiError ? caught.message : "Templates could not be loaded."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleUse = (template: CourseTemplateDocumentSummary) => {
    if (!template.usable || !template.template_id) return;
    update({
      selectedTemplate: {
        id: template.id,
        name: template.name,
        templateType: template.template_type,
        templateId: template.template_id,
      },
      template: template.template_type,
    });
    router.push("/");
  };

  const handleUseAdmin = (template: TemplateSummary) => {
    const templateType = asTemplateType(template.kind);
    update({
      selectedTemplate: {
        id: template.template_id,
        name: template.name,
        templateType,
        templateId: template.template_id,
      },
      template: templateType,
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

  const toggleVisibility = async (template: CourseTemplateDocumentSummary) => {
    const next: TemplateVisibility = template.visibility === "shared" ? "private" : "shared";
    setBusyId(template.id);
    setError(null);
    try {
      const updated = await setCourseTemplateVisibility(template.id, next);
      setTemplates((items) => items.map((item) => (item.id === template.id ? { ...item, ...updated } : item)));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not change who can see this template.");
    } finally {
      setBusyId(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setBusyId(deleting.id);
    setError(null);
    try {
      await deleteCourseTemplateDocument(deleting.id);
      setTemplates((items) => items.filter((item) => item.id !== deleting.id));
      // A deleted template can no longer start a course.
      if (draft.selectedTemplate?.id === deleting.id) update({ selectedTemplate: null });
      setDeleting(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not delete the template.");
    } finally {
      setBusyId(null);
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
              : "Blueprints that decide how a course is structured, written and illustrated - for any topic."}
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

          {adminTemplates.map((template) => (
            <article key={template.template_id} className="creator-template-card flex flex-col gap-3">
              <div className="flex items-center gap-2.5">
                <span className="creator-list-icon">
                  <Building2 size={16} />
                </span>
                <div>
                  <h3 className="text-[13.5px] font-semibold text-ink">{template.name}</h3>
                  <span className="mt-0.5 inline-block rounded-full bg-brand-50 px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-brand-600">
                    Organisation · {templateTypeLabel(template.kind)}
                  </span>
                </div>
              </div>
              <p className="line-clamp-3 text-[12px] leading-5 text-ink-500">
                {template.description || "Made by an admin."}
              </p>
              <p className="text-[11px] text-ink-400">
                {template.sections.length} sections{template.has_blueprint ? " · with generation rules" : ""}
              </p>
              <div className="mt-auto flex items-center justify-end pt-1">
                <Button size="sm" onClick={() => handleUseAdmin(template)}>
                  Use Template
                  <ArrowRight size={13} />
                </Button>
              </div>
            </article>
          ))}
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
                    ? "Upload a DOCX or Markdown file that describes how a course should be taught and illustrated - or use the Default Template above."
                    : "No one has shared a template yet - the Default Template above still works."}
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
                        <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                          <span className="inline-block rounded-full bg-brand-50 px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-brand-600">
                            {templateTypeLabel(template.template_type)}
                          </span>
                          <span
                            className="inline-flex items-center gap-1 rounded-full bg-canvas px-2 py-0.5 text-[10.5px] font-medium text-ink-500"
                            title={template.visibility === "shared" ? "Everyone can use it" : "Only the uploader and admins can see it"}
                          >
                            {template.visibility === "shared" ? <Globe2 size={10} /> : <Lock size={10} />}
                            {template.visibility === "shared" ? "Shared" : "Private"}
                          </span>
                        </div>
                      </div>
                    </div>
                    {template.can_manage ? (
                      <button
                        type="button"
                        onClick={() => setDeleting(template)}
                        disabled={busyId === template.id}
                        aria-label={`Delete ${template.name}`}
                        className="rounded-[7px] p-1.5 text-ink-400 transition-colors hover:bg-red-50 hover:text-danger"
                      >
                        <Trash2 size={14} />
                      </button>
                    ) : null}
                  </div>
                  {template.description ? (
                    <p className="line-clamp-3 text-[12px] leading-5 text-ink-500">{template.description}</p>
                  ) : (
                    <p className="text-[12px] italic leading-5 text-ink-400">No description provided.</p>
                  )}
                  <TemplateRulesSummary template={template} />
                  <div className="mt-auto flex flex-wrap items-center justify-between gap-2 pt-1">
                    <div className="flex items-center gap-1">
                      {template.blueprint ? (
                        <Button variant="ghost" size="sm" onClick={() => setPreviewing(template)}>
                          <Eye size={13} />
                          View rules
                        </Button>
                      ) : null}
                      {template.can_manage ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void toggleVisibility(template)}
                          disabled={busyId === template.id}
                        >
                          {template.visibility === "shared" ? "Make private" : "Share"}
                        </Button>
                      ) : null}
                    </div>
                    <Button
                      size="sm"
                      onClick={() => handleUse(template)}
                      disabled={!template.usable}
                      title={template.usable ? undefined : template.unusable_reason}
                    >
                      Use Template
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
          onUploaded={(uploaded) => {
            setUploadOpen(false);
            setPreviewing(uploaded);
            load();
          }}
        />
      ) : null}

      <Modal
        open={previewing !== null}
        onClose={() => setPreviewing(null)}
        title={previewing ? `What the system understood: ${previewing.name}` : ""}
        subtitle="The rules every course made from this template will follow."
        width="max-w-3xl"
        footer={
          <Button size="sm" variant="outline" onClick={() => setPreviewing(null)}>
            Close
          </Button>
        }
      >
        {previewing ? <BlueprintPreview template={previewing} /> : null}
      </Modal>

      <Modal
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete template?"
        subtitle={deleting?.name}
        width="max-w-md"
        footer={
          <>
            <Button size="sm" variant="outline" onClick={() => setDeleting(null)} disabled={busyId !== null}>
              Cancel
            </Button>
            <Button size="sm" variant="danger" onClick={() => void confirmDelete()} disabled={busyId !== null}>
              Delete
            </Button>
          </>
        }
      >
        <p className="text-[12.5px] leading-5 text-ink-500">
          Nobody will be able to start a new course from it. Courses already made from it keep working and keep their
          layout.
        </p>
      </Modal>
    </div>
  );
}

/** A one-glance summary of the processed blueprint on each card, so a
 * template that will drive generation is distinguishable from one that
 * cannot. */
function TemplateRulesSummary({ template }: { template: CourseTemplateDocumentSummary }) {
  if (!template.usable || !template.blueprint) {
    return (
      <p className="flex items-start gap-1.5 rounded-lg bg-canvas px-2.5 py-2 text-[11px] leading-4 text-amber-700">
        <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden />
        <span>{template.unusable_reason || "This template cannot be used."}</span>
      </p>
    );
  }
  const blueprint = template.blueprint;
  const required = blueprint.visuals.filter((rule) => rule.required && rule.support !== "none").length;
  const count = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const facts = [
    template.sections.length ? count(template.sections.length, "section") : null,
    blueprint.visuals.length ? `${count(blueprint.visuals.length, "visual rule")}${required ? ` (${required} required)` : ""}` : null,
    blueprint.examples.length ? count(blueprint.examples.length, "example") : null,
    blueprint.design.applied ? "own design" : null,
  ].filter(Boolean) as string[];
  const attention = blueprint.problems.length + blueprint.warnings.length;
  return (
    <div className="space-y-1.5">
      {facts.length ? <p className="text-[11px] leading-4 text-ink-500">{facts.join(" · ")}</p> : null}
      {attention ? (
        <p className="flex items-start gap-1.5 text-[11px] leading-4 text-amber-700">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden />
          <span>
            {attention} note{attention === 1 ? "" : "s"} - see View rules.
          </span>
        </p>
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
  onUploaded: (uploaded: CourseTemplateDocumentSummary) => void;
}) {
  const [name, setName] = useState("");
  const [templateType, setTemplateType] = useState<CourseTemplateType>("technical");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<TemplateVisibility>("private");
  const [applyDesign, setApplyDesign] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setName("");
      setTemplateType("technical");
      setDescription("");
      setVisibility("private");
      setApplyDesign(false);
      setFile(null);
      setError(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }, [open]);

  const isDocx = Boolean(file?.name.toLowerCase().endsWith(".docx"));
  const canSubmit = name.trim().length > 0 && Boolean(file) && !busy;

  const handleSubmit = async () => {
    if (!file || !canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const uploaded = await uploadCourseTemplateDocument({
        name: name.trim(),
        templateType,
        description: description.trim(),
        visibility,
        applyDesign: isDocx && applyDesign,
        file,
      });
      onUploaded(uploaded);
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
      subtitle="A DOCX or Markdown file that describes how a course should be taught and illustrated"
      footer={
        <>
          <Button variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" onClick={handleSubmit} disabled={!canSubmit}>
            {busy ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                Reading template…
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

        <div className="grid gap-4 sm:grid-cols-2">
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
            <label htmlFor="template-visibility" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
              Who can use it
            </label>
            <select
              id="template-visibility"
              className="field"
              value={visibility}
              onChange={(event) => setVisibility(event.target.value as TemplateVisibility)}
            >
              <option value="private">Only me (and admins)</option>
              <option value="shared">Everyone</option>
            </select>
          </div>
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
          <p className="mt-1.5 text-[11px] leading-4 text-ink-400">
            Use headings for the parts of a chapter, write instructions as instructions (&ldquo;Add a labelled
            flowchart after the explanation&rdquo;), and put general rules under a heading such as
            &ldquo;Writing rules&rdquo; or &ldquo;Visual rules&rdquo;. Sample text is treated as an example of the
            pattern, never copied. Max 10 MB.
          </p>
        </div>

        {isDocx ? (
          <label className="flex items-start gap-2.5 text-[12.5px] text-ink-700">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={applyDesign}
              onChange={(event) => setApplyDesign(event.target.checked)}
            />
            <span>
              Also apply this document&rsquo;s design
              <span className="block text-[11px] leading-4 text-ink-400">
                Fonts, colours, page size and header/footer. Leave off to learn only the structure and teaching
                patterns - recommended for designed ebooks and Canva files.
              </span>
            </span>
          </label>
        ) : null}

        {error ? (
          <p className="rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-900">
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
