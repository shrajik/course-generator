"use client";

import { useState } from "react";
import {
  AlignCenter,
  AlignJustify,
  AlignLeft,
  AlignRight,
  ArrowDown,
  ArrowDownToLine,
  ArrowUp,
  ArrowUpToLine,
  ImagePlus,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { BLOCK_LABELS, isTextBlock } from "@/lib/editor/blocks";
import { useEditor } from "@/lib/editor/store";
import type { TextAlign } from "@/lib/types/document";
import { cn } from "@/lib/utils/cn";

const FONTS = ["Inter", "Georgia", "Segoe UI", "Helvetica", "JetBrains Mono"];
const WEIGHTS: Array<{ label: string; value: number }> = [
  { label: "Regular", value: 400 },
  { label: "Medium", value: 500 },
  { label: "Semibold", value: 600 },
  { label: "Bold", value: 700 },
  { label: "Extrabold", value: 800 },
];
const ALIGNMENTS: Array<{ value: TextAlign; Icon: typeof AlignLeft }> = [
  { value: "left", Icon: AlignLeft },
  { value: "center", Icon: AlignCenter },
  { value: "right", Icon: AlignRight },
  { value: "justify", Icon: AlignJustify },
];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-b border-line px-4 py-4 last:border-b-0">
      <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-400">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] text-ink-500">{label}</span>
      {children}
    </label>
  );
}

const inputClass =
  "h-8 w-full rounded-[7px] border border-line bg-white px-2 text-[12px] text-ink outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";

interface PropertiesPanelProps {
  onReplaceImage: (blockId: string, description: string) => void;
  busy: boolean;
}

export function PropertiesPanel({ onReplaceImage, busy }: PropertiesPanelProps) {
  const editor = useEditor();
  const block = editor.selectedBlocks[0] ?? null;
  const [imagePrompt, setImagePrompt] = useState("");

  if (!block) {
    return (
      <aside className="w-[248px] shrink-0 border-l border-line bg-white">
        <div className="px-4 py-4">
          <h2 className="text-[13px] font-semibold text-ink">Properties</h2>
          <p className="mt-2 text-[11.5px] leading-relaxed text-ink-400">
            Select a block on the page to edit its text, style, position and size — or to ask
            the AI Assistant to change it.
          </p>
        </div>
      </aside>
    );
  }

  const style = block.style;
  const layout = block.layout;
  const textual = isTextBlock(block.type) || block.type === "code";
  const setStyle = (patch: Record<string, unknown>) => editor.updateStyle(block.id, patch);
  const setLayout = (patch: Record<string, number>) => editor.updateLayout(block.id, patch);

  return (
    <aside className="subtle-scroll w-[248px] shrink-0 overflow-y-auto border-l border-line bg-white">
      <div className="flex items-center justify-between px-4 pb-3 pt-4">
        <h2 className="text-[13px] font-semibold text-ink">{BLOCK_LABELS[block.type]}</h2>
        <IconButton
          tone="danger"
          aria-label="Delete block"
          onClick={() => editor.deleteBlock(block.id)}
        >
          <Trash2 size={13} />
        </IconButton>
      </div>

      {textual ? (
        <>
          <Section title="Style">
            <div className="space-y-3">
              <Field label="Font">
                <select
                  className={inputClass}
                  value={style.font_family?.split(",")[0] ?? "Inter"}
                  onChange={(event) => setStyle({ font_family: event.target.value })}
                >
                  {FONTS.map((font) => (
                    <option key={font} value={font}>
                      {font}
                    </option>
                  ))}
                </select>
              </Field>

              <div className="grid grid-cols-[64px_minmax(0,1fr)] gap-2">
                <Field label="Size">
                  <input
                    type="number"
                    min={8}
                    max={96}
                    step={0.5}
                    className={inputClass}
                    value={style.font_size ?? 16}
                    onChange={(event) => setStyle({ font_size: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Weight">
                  <select
                    className={inputClass}
                    value={style.font_weight ?? 400}
                    onChange={(event) => setStyle({ font_weight: Number(event.target.value) })}
                  >
                    {WEIGHTS.map((weight) => (
                      <option key={weight.value} value={weight.value}>
                        {weight.label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>

              <div>
                <span className="mb-1 block text-[11px] text-ink-500">Alignment</span>
                <div className="flex items-center gap-1 rounded-[7px] border border-line p-0.5">
                  {ALIGNMENTS.map(({ value, Icon }) => (
                    <button
                      key={value}
                      type="button"
                      aria-label={`Align ${value}`}
                      onClick={() => setStyle({ align: value })}
                      className={cn(
                        "flex h-6 flex-1 items-center justify-center rounded-[5px] transition-colors",
                        style.align === value
                          ? "bg-brand-100 text-brand-700"
                          : "text-ink-400 hover:bg-brand-50",
                      )}
                    >
                      <Icon size={12} />
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <span className="mb-1 block text-[11px] text-ink-500">Color</span>
                <div className="flex items-center gap-2 rounded-[7px] border border-line px-2 py-1.5">
                  <label className="relative h-4 w-4 cursor-pointer overflow-hidden rounded-[3px] border border-line">
                    <span
                      className="block h-full w-full"
                      style={{ background: style.color ?? "#1A1A1A" }}
                    />
                    <input
                      type="color"
                      aria-label="Text colour"
                      value={style.color ?? "#1A1A1A"}
                      onChange={(event) => setStyle({ color: event.target.value })}
                      className="absolute inset-0 cursor-pointer opacity-0"
                    />
                  </label>
                  <input
                    aria-label="Colour hex"
                    className="min-w-0 flex-1 bg-transparent text-[12px] uppercase text-ink outline-none"
                    value={style.color ?? "#1A1A1A"}
                    onChange={(event) => setStyle({ color: event.target.value })}
                  />
                </div>
              </div>
            </div>
          </Section>

          <Section title="Spacing">
            <div className="grid grid-cols-2 gap-2">
              <Field label="Line Height">
                <input
                  type="number"
                  step={0.05}
                  min={0.8}
                  max={3}
                  className={inputClass}
                  value={style.line_height ?? 1.6}
                  onChange={(event) => setStyle({ line_height: Number(event.target.value) })}
                />
              </Field>
              <Field label="Letter Spacing">
                <input
                  type="number"
                  step={0.1}
                  min={-2}
                  max={10}
                  className={inputClass}
                  value={style.letter_spacing ?? 0}
                  onChange={(event) => setStyle({ letter_spacing: Number(event.target.value) })}
                />
              </Field>
            </div>
          </Section>
        </>
      ) : null}

      {block.type === "image" ? (
        <Section title="Image">
          <p className="mb-2 text-[11px] leading-snug text-ink-400">
            Describe the replacement and the backend will generate it.
          </p>
          <textarea
            rows={3}
            value={imagePrompt}
            onChange={(event) => setImagePrompt(event.target.value)}
            placeholder="A labelled diagram of the request flow…"
            className="mb-2 w-full resize-none rounded-[7px] border border-line bg-white px-2 py-1.5 text-[12px] text-ink outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100"
          />
          <Button
            size="sm"
            className="w-full"
            disabled={busy || !imagePrompt.trim()}
            onClick={() => {
              onReplaceImage(block.id, imagePrompt.trim());
              setImagePrompt("");
            }}
          >
            <ImagePlus size={13} />
            {busy ? "Replacing…" : "Replace Image"}
          </Button>
        </Section>
      ) : null}

      <Section title="Position & Size">
        <div className="grid grid-cols-2 gap-2">
          <Field label="X">
            <input
              type="number"
              className={inputClass}
              value={Math.round(layout.x)}
              onChange={(event) => setLayout({ x: Number(event.target.value) })}
            />
          </Field>
          <Field label="Y">
            <input
              type="number"
              className={inputClass}
              value={Math.round(layout.y)}
              onChange={(event) => setLayout({ y: Number(event.target.value) })}
            />
          </Field>
          <Field label="W">
            <input
              type="number"
              className={inputClass}
              value={Math.round(layout.width)}
              onChange={(event) => setLayout({ width: Number(event.target.value) })}
            />
          </Field>
          <Field label="H">
            <input
              type="number"
              className={inputClass}
              value={Math.round(layout.height)}
              onChange={(event) => setLayout({ height: Number(event.target.value) })}
            />
          </Field>
        </div>
      </Section>

      <Section title="Layer">
        <div className="flex items-center gap-1.5">
          <IconButton
            aria-label="Bring to front"
            onClick={() => setLayout({ z_index: (layout.z_index || 0) + 10 })}
          >
            <ArrowUpToLine size={13} />
          </IconButton>
          <IconButton
            aria-label="Bring forward"
            onClick={() => setLayout({ z_index: (layout.z_index || 0) + 1 })}
          >
            <ArrowUp size={13} />
          </IconButton>
          <IconButton
            aria-label="Send backward"
            onClick={() => setLayout({ z_index: (layout.z_index || 0) - 1 })}
          >
            <ArrowDown size={13} />
          </IconButton>
          <IconButton
            aria-label="Send to back"
            onClick={() => setLayout({ z_index: 0 })}
          >
            <ArrowDownToLine size={13} />
          </IconButton>
        </div>
      </Section>
    </aside>
  );
}
