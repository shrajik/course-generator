"use client";

import { useEffect, useState } from "react";
import { FileText, Layers3, Loader2, Sparkles } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { listTemplates, type TemplateSummary } from "@/lib/api/courses";
import { AdminAlert, AdminEmpty } from "@/components/admin/AdminUI";

export default function TemplatesPage() {
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    listTemplates().then((response) => { if (active) setTemplates(response.templates); }).catch((caught) => { if (active) setError(caught instanceof ApiError ? caught.message : "Templates could not be loaded."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  return <div className="creator-page mx-auto w-full max-w-[1120px]"><div className="creator-page-heading"><div><p className="creator-eyebrow">Course design</p><h1 className="creator-page-title">Templates</h1><p className="creator-page-description">Choose a structure that gives your next course a clear starting point.</p></div><Sparkles size={22} className="text-brand-500" /></div>{error ? <AdminAlert>{error}</AdminAlert> : null}{loading ? <div className="creator-loading mt-5"><Loader2 size={18} className="animate-spin text-brand-500" /> Loading templates</div> : templates.length === 0 ? <AdminEmpty><Layers3 size={24} className="text-brand-300" /><span>No templates are available yet.</span></AdminEmpty> : <div className="mt-5 grid gap-4 md:grid-cols-2">{templates.map((template) => <article key={template.template_id} className="creator-template-card"><span className="creator-list-icon"><FileText size={17} /></span><h2 className="mt-4 text-[16px] font-semibold text-ink">{template.name}</h2><p className="mt-2 text-[13px] leading-5 text-ink-500">{template.description}</p><div className="mt-5 flex items-center justify-between border-t border-line pt-3 text-[11px] text-ink-400"><span>{template.sections.length} sections</span><span className="font-mono">{template.template_id}</span></div></article>)}</div>}</div>;
}
