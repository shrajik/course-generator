"use client";

/**
 * What the system understood from an uploaded template: the chapter
 * structure, writing rules, visual rules, worked examples, reusable pictures
 * and design - plus everything it could not use. Shown after an upload and
 * from "View rules" on a template card, so the uploader can check the
 * blueprint before any course is generated from it.
 */

import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Image as ImageIcon, Info, XCircle } from "lucide-react";
import { requestBlob } from "@/lib/api/client";
import type {
  BlueprintVisualRule,
  CourseTemplateDocumentSummary,
  TemplateBlueprint,
} from "@/lib/api/course-templates";

const KIND_LABEL: Record<BlueprintVisualRule["kind"], string> = {
  any: "Visual (best fit)",
  diagram: "Diagram",
  illustration: "Illustration",
  concept_experience: "Concept visual",
  chart: "Chart",
  photo: "Photo-style picture",
  screenshot: "Screenshot",
};

const PLACEMENT_LABEL: Record<BlueprintVisualRule["placement"], string> = {
  anywhere: "where it fits",
  after_text: "after the explanation",
  before_text: "before the text",
  end_of_section: "at the end of the section",
};

function where(rule: BlueprintVisualRule, labels: Map<string, string>): string {
  if (rule.scope === "section") return `In "${labels.get(rule.section_key) ?? rule.section_key}"`;
  if (rule.scope === "each_section") return "In every section";
  return "In every chapter";
}

function Heading({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-2 mt-5 text-[12px] font-semibold uppercase tracking-wide text-ink-500 first:mt-0">{children}</h3>;
}

function Rule({ label, value, inferred }: { label: string; value: string; inferred?: boolean }) {
  return (
    <li className="text-[12.5px] leading-5 text-ink-700">
      <span className="font-medium text-ink">{label}:</span> {value}
      {inferred ? (
        <span className="ml-1.5 rounded-full bg-canvas px-1.5 py-0.5 text-[10px] font-medium text-ink-500">
          inferred from examples
        </span>
      ) : null}
    </li>
  );
}

function textRuleRows(blueprint: TemplateBlueprint): { label: string; value: string; inferred?: boolean }[] {
  const text = blueprint.text;
  const inferred = new Set(text.inferred);
  const rows: { label: string; value: string; inferred?: boolean }[] = [];
  const one = (label: string, key: keyof typeof text) => {
    const value = text[key];
    if (typeof value === "string" && value) rows.push({ label, value, inferred: inferred.has(key) });
  };
  const many = (label: string, key: keyof typeof text) => {
    const value = text[key];
    if (Array.isArray(value)) for (const item of value) rows.push({ label, value: item, inferred: inferred.has(key) });
  };
  one("Tone", "tone");
  one("Reading level", "reading_level");
  one("Depth", "depth");
  if (text.words_per_chapter) rows.push({ label: "Chapter length", value: `about ${text.words_per_chapter} words` });
  one("Paragraphs", "paragraph_length");
  one("Sections", "section_length");
  many("Explanations", "explanation_patterns");
  many("Examples", "examples");
  many("Terminology", "terminology");
  many("Assessments", "assessments");
  many("Openings", "introductions");
  many("Endings", "conclusions");
  many("Always", "include");
  many("Avoid", "avoid");
  many("Also", "other");
  return rows;
}

function TemplatePicture({ templateId, assetId, alt }: { templateId: string; assetId: string; alt: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    requestBlob(`/api/course-template-documents/${templateId}/assets/${assetId}`)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setSrc(url);
      })
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [templateId, assetId]);
  if (failed) {
    return (
      <div className="flex h-20 w-28 items-center justify-center rounded-md bg-canvas text-ink-400">
        <ImageIcon size={18} aria-label="Picture unavailable" />
      </div>
    );
  }
  return src ? (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={alt} className="h-20 w-28 rounded-md border border-line object-contain" />
  ) : (
    <div className="h-20 w-28 animate-pulse rounded-md bg-canvas" />
  );
}

export function BlueprintPreview({ template }: { template: CourseTemplateDocumentSummary }) {
  const blueprint = template.blueprint;
  if (!blueprint) {
    return (
      <p className="rounded-lg bg-canvas px-3 py-2.5 text-[12.5px] leading-5 text-ink-500">
        {template.unusable_reason || "This template has no processed rules."}
      </p>
    );
  }

  const sectionLabels = new Map(template.sections.map((section) => [section.key, section.label]));
  const rules = textRuleRows(blueprint);
  const attention = [...blueprint.problems, ...blueprint.warnings];
  const extracted = blueprint.design.extracted as Record<string, unknown>;
  const swatches = ["text_color", "heading_color", "accent_color", "page_background"]
    .map((key) => [key, extracted[key]] as const)
    .filter((entry): entry is readonly [string, string] => typeof entry[1] === "string" && entry[1] !== "");

  return (
    <div className="space-y-1 text-ink">
      <p className="text-[12px] leading-5 text-ink-500">
        This is what the system understood from <span className="font-medium text-ink">{template.name}</span>. Every
        course made from this template follows these rules for its own topic; the examples guide the writing pattern
        and are never copied.
      </p>

      {attention.length || blueprint.ignored.length ? (
        <>
          <Heading>Needs your attention</Heading>
          <ul className="space-y-1.5">
            {attention.map((item) => (
              <li key={item} className="flex gap-2 text-[12.5px] leading-5 text-amber-800">
                <AlertTriangle size={13} className="mt-1 shrink-0" aria-hidden />
                <span>{item}</span>
              </li>
            ))}
            {blueprint.ignored.map((item) => (
              <li key={item} className="flex gap-2 text-[12.5px] leading-5 text-ink-500">
                <XCircle size={13} className="mt-1 shrink-0" aria-hidden />
                <span>
                  Ignored: <q>{item}</q>
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <Heading>Chapter structure</Heading>
      {template.sections.length ? (
        <ol className="space-y-2">
          {template.sections.map((section, index) => (
            <li key={section.key} className="rounded-lg border border-line px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-semibold text-ink-400">{index + 1}</span>
                <span className="text-[13px] font-medium">{section.label}</span>
                <span
                  className={
                    section.required
                      ? "rounded-full bg-brand-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-brand-600"
                      : "rounded-full bg-canvas px-1.5 py-0.5 text-[10px] font-semibold uppercase text-ink-500"
                  }
                >
                  {section.required ? "Required" : "Optional"}
                </span>
              </div>
              {section.guidance ? <p className="mt-1 text-[12px] leading-5 text-ink-500">{section.guidance}</p> : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-[12.5px] text-ink-500">No sections - chapters keep the default structure.</p>
      )}

      <Heading>Writing rules</Heading>
      {rules.length ? (
        <ul className="space-y-1">
          {rules.map((row, index) => (
            <Rule key={`${row.label}-${index}`} {...row} />
          ))}
        </ul>
      ) : (
        <p className="text-[12.5px] text-ink-500">None stated.</p>
      )}

      <Heading>Visuals</Heading>
      {blueprint.visuals.length ? (
        <ul className="space-y-2">
          {blueprint.visuals.map((rule) => (
            <li key={rule.id} className="rounded-lg border border-line px-3 py-2">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px]">
                <span className="font-semibold">{rule.asset_id ? `Template picture ${rule.asset_id}` : KIND_LABEL[rule.kind]}</span>
                {rule.diagram_kind ? <span className="text-ink-500">({rule.diagram_kind.replace(/_/g, " ")})</span> : null}
                <span className="text-ink-500">
                  · {where(rule, sectionLabels)}, {PLACEMENT_LABEL[rule.placement]}
                </span>
                <span
                  className={
                    rule.required
                      ? "rounded-full bg-brand-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-brand-600"
                      : "rounded-full bg-canvas px-1.5 py-0.5 text-[10px] font-semibold uppercase text-ink-500"
                  }
                >
                  {rule.required ? "Required" : "Optional"}
                </span>
                {rule.origin === "inferred" ? (
                  <span className="rounded-full bg-canvas px-1.5 py-0.5 text-[10px] font-medium text-ink-500">
                    from an example picture
                  </span>
                ) : null}
              </div>
              {rule.instruction ? <p className="mt-1 text-[12px] leading-5 text-ink-500">“{rule.instruction}”</p> : null}
              <p className="mt-1 text-[11.5px] leading-4 text-ink-500">
                {[
                  rule.count ? `${rule.count} per ${rule.scope === "each_section" ? "section" : rule.scope}` : null,
                  rule.labels.length ? `labels: ${rule.labels.join(", ")}` : rule.labelled ? "labelled" : null,
                  rule.caption_required ? "caption required" : null,
                  rule.aspect ? `shape: ${rule.aspect}${rule.aspect_text ? ` (${rule.aspect_text})` : ""}` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              {rule.support !== "full" ? (
                <p
                  className={
                    rule.support === "none"
                      ? "mt-1 flex gap-1.5 text-[11.5px] leading-4 text-danger"
                      : "mt-1 flex gap-1.5 text-[11.5px] leading-4 text-amber-700"
                  }
                >
                  {rule.support === "none" ? <XCircle size={12} className="mt-0.5 shrink-0" /> : <Info size={12} className="mt-0.5 shrink-0" />}
                  <span>
                    {rule.support === "none" ? "Not supported: " : "Partly supported: "}
                    {rule.note}
                  </span>
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[12.5px] text-ink-500">No visual rules - the default visual guidance applies.</p>
      )}
      {blueprint.section_intro_images === false ? (
        <p className="mt-2 flex gap-1.5 text-[12px] text-ink-500">
          <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-brand-600" />
          Decorative section-opening pictures are switched off for this template.
        </p>
      ) : null}

      {blueprint.examples.length ? (
        <>
          <Heading>Examples (pattern only)</Heading>
          <ul className="space-y-2">
            {blueprint.examples.map((example) => (
              <li key={example.id} className="flex gap-3 rounded-lg bg-canvas px-3 py-2">
                {example.kind === "image" && example.asset_id ? (
                  <TemplatePicture templateId={template.id} assetId={example.asset_id} alt={example.caption || "Example picture"} />
                ) : null}
                <div className="min-w-0">
                  <p className="text-[11px] font-medium text-ink-500">
                    {example.section_key ? sectionLabels.get(example.section_key) ?? example.section_key : "Whole chapter"}
                    {example.pattern.length ? ` · ${example.pattern.join(" → ")}` : ""}
                    {example.kind !== "text" ? ` · ${example.kind}` : ""}
                  </p>
                  {example.caption ? <p className="text-[12px] font-medium text-ink">{example.caption}</p> : null}
                  {example.text ? (
                    <p className="line-clamp-4 whitespace-pre-line text-[12px] leading-5 text-ink-500">{example.text}</p>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {blueprint.assets.some((asset) => asset.role === "reusable") ? (
        <>
          <Heading>Pictures reused in courses</Heading>
          <ul className="flex flex-wrap gap-3">
            {blueprint.assets
              .filter((asset) => asset.role === "reusable")
              .map((asset) => (
                <li key={asset.id} className="w-28 text-[11px] text-ink-500">
                  <TemplatePicture templateId={template.id} assetId={asset.id} alt={asset.caption || asset.id} />
                  <p className="mt-1 line-clamp-2">{asset.caption || asset.id}</p>
                </li>
              ))}
          </ul>
        </>
      ) : null}

      <Heading>Design</Heading>
      <p className="text-[12.5px] leading-5 text-ink-500">
        {blueprint.design.applied
          ? blueprint.design.reason === "document"
            ? "The document asks for its own design, so its fonts, colours, page size and header/footer are applied."
            : "Applied: courses use this document's fonts, colours, page size and header/footer."
          : blueprint.source_format === "md"
            ? "Markdown carries no design; courses use the standard look."
            : "Not applied: courses use the standard look. The document's design was read but is not copied."}
      </p>
      {swatches.length || typeof extracted.font_family === "string" ? (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-[11.5px] text-ink-500">
          {typeof extracted.font_family === "string" ? <span>Font: {String(extracted.font_family).split(",")[0]}</span> : null}
          {swatches.map(([key, color]) => (
            <span key={key} className="flex items-center gap-1">
              <span className="h-3.5 w-3.5 rounded border border-line" style={{ background: color }} />
              {key.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
