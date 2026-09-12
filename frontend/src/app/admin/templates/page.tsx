"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { Archive, ArchiveRestore, FileText, History, Layers3, Loader2, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import {
  AdminAlert,
  AdminEmpty,
  AdminLoading,
  AdminPageHeader,
  AdminPanel,
  AdminStatusBadge,
} from "@/components/admin/AdminUI";
import {
  archiveAdminTemplate,
  createAdminTemplate,
  getTemplateConfig,
  listAdminTemplateVersions,
  listAdminTemplates,
  unarchiveAdminTemplate,
  updateAdminTemplate,
  type TemplateConfig,
  type TemplateRecord,
} from "@/lib/api/templates";

const inputClass =
  "h-10 w-full rounded-[8px] border border-line bg-white px-3 text-[13px] text-ink outline-none placeholder:text-ink-400 transition-colors focus:border-brand-500";
const selectClass = inputClass;
const textareaClass =
  "w-full rounded-[8px] border border-line bg-white p-3 font-mono text-[12px] text-ink outline-none transition-colors focus:border-brand-500";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

interface FormState {
  mode: "create" | "edit";
  slug?: string;
  name: string;
  kind: "technical" | "non_technical";
  description: string;
  configText: string;
  seedFrom: string;
}

const SEED_OPTIONS = ["technical_v1", "non_technical_v1"];

function emptyForm(): FormState {
  return { mode: "create", name: "", kind: "technical", description: "", configText: "", seedFrom: "technical_v1" };
}

export default function AdminTemplatesPage() {
  const [templates, setTemplates] = useState<TemplateRecord[]>([]);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [versions, setVersions] = useState<{ slug: string; items: TemplateRecord[] } | null>(null);

  const load = useCallback(() => {
    setIsLoading(true);
    listAdminTemplates({ include_archived: includeArchived, limit: 100 })
      .then((response) => {
        setTemplates(response.templates);
        setError(null);
      })
      .catch((caught) => setError(caught instanceof ApiError ? caught.message : "Templates could not be loaded."))
      .finally(() => setIsLoading(false));
  }, [includeArchived]);

  useEffect(() => load(), [load]);

  const openCreate = async () => {
    setFormError(null);
    const seeded = emptyForm();
    try {
      const config = await getTemplateConfig(seeded.seedFrom);
      seeded.configText = JSON.stringify(config, null, 2);
    } catch {
      // Seeding is a convenience only - an empty editor still works.
    }
    setForm(seeded);
  };

  const openEdit = (template: TemplateRecord) => {
    setFormError(null);
    setForm({
      mode: "edit",
      slug: template.slug,
      name: template.name,
      kind: template.kind,
      description: template.description,
      configText: JSON.stringify(template.config, null, 2),
      seedFrom: template.template_id,
    });
  };

  const reseed = async (templateId: string) => {
    if (!form) return;
    try {
      const config = await getTemplateConfig(templateId);
      setForm({ ...form, configText: JSON.stringify(config, null, 2), seedFrom: templateId });
    } catch (caught) {
      setFormError(caught instanceof ApiError ? caught.message : "Could not load that template.");
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!form) return;
    setFormError(null);

    let config: TemplateConfig;
    try {
      config = JSON.parse(form.configText) as TemplateConfig;
    } catch {
      setFormError("Config is not valid JSON.");
      return;
    }

    setSaving(true);
    try {
      if (form.mode === "create") {
        await createAdminTemplate({ name: form.name, kind: form.kind, description: form.description, config });
      } else if (form.slug) {
        await updateAdminTemplate(form.slug, { name: form.name, description: form.description, config });
      }
      setForm(null);
      load();
    } catch (caught) {
      setFormError(caught instanceof ApiError ? caught.message : "The template could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  const toggleArchive = async (template: TemplateRecord) => {
    try {
      if (template.is_archived) await unarchiveAdminTemplate(template.slug);
      else await archiveAdminTemplate(template.slug);
      load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not update the template.");
    }
  };

  const showVersions = async (slug: string) => {
    try {
      const items = await listAdminTemplateVersions(slug);
      setVersions({ slug, items });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Versions could not be loaded.");
    }
  };

  return (
    <div className="admin-page mx-auto flex w-full max-w-[1240px] flex-col gap-5">
      <AdminPageHeader
        eyebrow="AI memory layer"
        title="Templates"
        description="Create, edit and version course templates. Editing never changes a course that already used an earlier version."
      />

      {error ? <AdminAlert>{error}</AdminAlert> : null}

      <AdminPanel className="flex flex-wrap items-center justify-between gap-3 p-4">
        <label className="flex items-center gap-2 text-[13px] text-[#BEB6AD]">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => setIncludeArchived(event.target.checked)}
          />
          Show archived
        </label>
        <Button type="button" size="sm" onClick={openCreate} className="gap-1.5">
          <Plus size={14} />
          New template
        </Button>
      </AdminPanel>

      <AdminPanel>
        {isLoading ? (
          <AdminLoading label="Loading templates" />
        ) : templates.length === 0 ? (
          <AdminEmpty>
            <Layers3 size={24} className="text-brand-300" />
            <span>No templates yet - create one to get started.</span>
          </AdminEmpty>
        ) : (
          <div className="overflow-x-auto">
            <table className="admin-table w-full min-w-[900px] text-left text-[13px]">
              <thead className="bg-[#181614] text-[12px] uppercase tracking-[0.08em] text-[#8E8780]">
                <tr>
                  <th className="px-4 py-3 font-medium">Name</th>
                  <th className="px-4 py-3 font-medium">Kind</th>
                  <th className="px-4 py-3 font-medium">Template ID</th>
                  <th className="px-4 py-3 font-medium">Version</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium">Updated</th>
                  <th className="px-4 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#302D29]">
                {templates.map((template) => (
                  <tr key={template.id} className="text-[#D8D0C7]">
                    <td className="px-4 py-3 font-medium text-[#F4EFE7]">
                      <span className="block truncate">{template.name}</span>
                      <span className="mt-1 block text-[12px] font-normal text-[#8E8780]">
                        {template.description || "No description"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-[#A9A29A]">{template.kind}</td>
                    <td className="px-4 py-3 font-mono text-[12px] text-[#A9A29A]">{template.template_id}</td>
                    <td className="px-4 py-3 text-[#A9A29A]">v{template.version}</td>
                    <td className="px-4 py-3">
                      <AdminStatusBadge tone={template.is_archived ? "danger" : "success"}>
                        {template.is_archived ? "archived" : "current"}
                      </AdminStatusBadge>
                    </td>
                    <td className="px-4 py-3 text-[#A9A29A]">{formatDate(template.updated_at)}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => openEdit(template)}
                          className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                          title="Edit (creates a new version)"
                        >
                          <FileText size={14} />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => showVersions(template.slug)}
                          className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                          title="Version history"
                        >
                          <History size={14} />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => toggleArchive(template)}
                          className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                          title={template.is_archived ? "Unarchive" : "Archive"}
                        >
                          {template.is_archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </AdminPanel>

      {versions ? (
        <AdminPanel className="p-5">
          <div className="flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-[#F7EEE2]">Versions of &apos;{versions.slug}&apos;</h3>
            <Button type="button" variant="ghost" size="sm" onClick={() => setVersions(null)}>
              <X size={14} />
            </Button>
          </div>
          <ul className="mt-3 space-y-2">
            {versions.items.map((v) => (
              <li key={v.id} className="flex items-center justify-between border-b border-[#302D29] py-2 text-[13px] text-[#D8D0C7]">
                <span className="font-mono text-[12px]">{v.template_id}</span>
                <span>{v.description || "No description"}</span>
                <span className="flex items-center gap-2">
                  {v.is_current ? <AdminStatusBadge tone="success">current</AdminStatusBadge> : null}
                  <span className="text-[#8E8780]">{formatDate(v.created_at)}</span>
                </span>
              </li>
            ))}
          </ul>
        </AdminPanel>
      ) : null}

      {form ? (
        <AdminPanel className="p-5">
          <div className="flex items-center justify-between">
            <h3 className="text-[15px] font-semibold text-[#F7EEE2]">
              {form.mode === "create" ? "New template" : `Edit '${form.slug}' (creates a new version)`}
            </h3>
            <Button type="button" variant="ghost" size="sm" onClick={() => setForm(null)}>
              <X size={14} />
            </Button>
          </div>
          {formError ? <div className="mt-3"><AdminAlert>{formError}</AdminAlert></div> : null}
          <form onSubmit={submit} className="mt-4 grid gap-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <input
                required
                placeholder="Name"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                className={inputClass}
              />
              {form.mode === "create" ? (
                <select
                  value={form.kind}
                  onChange={(event) => setForm({ ...form, kind: event.target.value as "technical" | "non_technical" })}
                  className={selectClass}
                >
                  <option value="technical">Technical</option>
                  <option value="non_technical">Non-technical</option>
                </select>
              ) : (
                <div className={`${inputClass} flex items-center text-ink-400`}>{form.kind} (fixed)</div>
              )}
            </div>
            <input
              placeholder="Description"
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
              className={inputClass}
            />
            <div className="flex items-center gap-2">
              <span className="text-[12px] text-ink-400">Start config from:</span>
              {[...SEED_OPTIONS, ...(form.mode === "edit" && form.slug ? [] : [])].map((id) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => reseed(id)}
                  className="rounded-full border border-line px-2.5 py-1 text-[11px] text-ink-500 hover:border-brand-500 hover:text-brand-600"
                >
                  {id}
                </button>
              ))}
            </div>
            <textarea
              required
              rows={16}
              value={form.configText}
              onChange={(event) => setForm({ ...form, configText: event.target.value })}
              className={textareaClass}
              spellCheck={false}
            />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" size="sm" onClick={() => setForm(null)}>
                Cancel
              </Button>
              <Button type="submit" size="sm" disabled={saving} className="gap-1.5">
                {saving ? <Loader2 size={14} className="animate-spin" /> : null}
                {form.mode === "create" ? "Create" : "Save as new version"}
              </Button>
            </div>
          </form>
        </AdminPanel>
      ) : null}
    </div>
  );
}
