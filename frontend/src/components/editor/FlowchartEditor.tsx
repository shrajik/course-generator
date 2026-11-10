"use client";

/**
 * Edit Visuals, Part 1: a modal editor for a flow_chart/process/etc.
 * diagram's own structured spec - node/edge add/delete/reorder, a per-node
 * role (start/process/decision/end) and color override, and a live preview
 * rendered server-side (deterministic, no AI call, zero disk write - see
 * DocumentService.preview_visual_spec). Save/Reset/Cancel.
 *
 * Self-contained: reads/writes the editor document via useEditor() directly
 * (same reconcile-after-save pattern CourseEditor's own
 * handleUpdateConceptVisualSpec already uses for concept_experience), so no
 * new props thread through CourseEditor/PropertiesPanel - mirrors this
 * codebase's established "self-contained toolbar/panel component" pattern
 * (PhaseApprovalButton, SamIterationButton).
 *
 * A modal, not the Properties sidebar: node/edge graph editing needs more
 * room than the sidebar's ~260px gives the sibling concept_experience
 * editor (ConceptVisualFieldsEditor) for its simpler flat field list.
 */

import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { Modal } from "@/components/ui/Modal";
import { ApiError } from "@/lib/api/client";
import { getDocument, previewVisualSpec, reconstructDiagramSpec, updateDiagramSpec } from "@/lib/api/documents";
import { blankEdge, blankNode, dropDanglingEdges, moveItem, nextNodeId } from "@/lib/editor/flowchartSpec";
import { useEditor } from "@/lib/editor/store";
import type { DiagramEdge, DiagramNode, FlowchartSpec } from "@/lib/types/document";

const ROLE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Auto" },
  { value: "start", label: "Start" },
  { value: "process", label: "Process" },
  { value: "decision", label: "Decision" },
  { value: "end", label: "End" },
];

const TEXT_SIZE_OPTIONS: { value: number; label: string }[] = [
  { value: 1, label: "Normal" },
  { value: 1.25, label: "Large" },
  { value: 1.5, label: "Extra large" },
];

const inputClass =
  "w-full rounded-[7px] border border-line bg-white px-2 py-1.5 text-[12px] text-ink outline-none " +
  "transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";

function nodeLabelFor(nodes: DiagramNode[], id: string): string {
  const node = nodes.find((n) => n.id === id);
  return node ? node.label || node.id : id;
}

export function FlowchartEditor({
  documentId,
  blockId,
  hasStoredSpec,
  open,
  onClose,
}: {
  documentId: string;
  blockId: string;
  /** False when the block has no `content.spec` yet (a course generated
   * before spec persistence existed) - triggers the one-time AI
   * reconstruction call instead of seeding from a prop. */
  hasStoredSpec: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const editor = useEditor();
  const [spec, setSpec] = useState<FlowchartSpec | null>(null);
  const [original, setOriginal] = useState<FlowchartSpec | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [previewSvg, setPreviewSvg] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // --- load (or reconstruct) the spec once, each time the modal opens ----
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    setSpec(null);
    setPreviewSvg(null);
    (async () => {
      try {
        const loaded = hasStoredSpec
          ? ((await getDocument(documentId)).pages
              .flatMap((p) => p.blocks)
              .find((b) => b.id === blockId)?.content.spec as FlowchartSpec | undefined)
          : await reconstructDiagramSpec(documentId, blockId);
        if (cancelled) return;
        if (!loaded) throw new Error("No diagram spec found for this block.");
        setSpec(loaded);
        setOriginal(loaded);
      } catch (caught) {
        if (!cancelled) {
          setLoadError(
            caught instanceof ApiError ? caught.message : "The diagram's content could not be loaded.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, documentId, blockId, hasStoredSpec]);

  // --- live preview, debounced, zero disk write ---------------------------
  useEffect(() => {
    if (!spec) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      previewVisualSpec(documentId, blockId, spec)
        .then((result) => {
          setPreviewSvg(result.svg ?? null);
          setPreviewError(null);
        })
        .catch((caught) => {
          setPreviewError(caught instanceof ApiError ? caught.message : "Preview failed.");
        });
    }, 400);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [spec, documentId, blockId]);

  if (!open) return null;

  const updateNode = (index: number, patch: Partial<DiagramNode>) => {
    setSpec((prev) => {
      if (!prev) return prev;
      const nodes = [...prev.nodes];
      nodes[index] = { ...nodes[index], ...patch };
      return { ...prev, nodes };
    });
  };

  const addNode = () => {
    setSpec((prev) => {
      if (!prev) return prev;
      return { ...prev, nodes: [...prev.nodes, blankNode(nextNodeId(prev.nodes))] };
    });
  };

  const deleteNode = (index: number) => {
    setSpec((prev) => {
      if (!prev) return prev;
      const nodes = prev.nodes.filter((_, i) => i !== index);
      const edges = dropDanglingEdges(prev.edges, new Set(nodes.map((n) => n.id)));
      return { ...prev, nodes, edges };
    });
  };

  const moveNode = (index: number, direction: -1 | 1) => {
    setSpec((prev) => (prev ? { ...prev, nodes: moveItem(prev.nodes, index, direction) } : prev));
  };

  const updateEdge = (index: number, patch: Partial<DiagramEdge>) => {
    setSpec((prev) => {
      if (!prev) return prev;
      const edges = [...prev.edges];
      edges[index] = { ...edges[index], ...patch };
      return { ...prev, edges };
    });
  };

  const addEdge = () => {
    setSpec((prev) => {
      if (!prev || prev.nodes.length < 2) return prev;
      return { ...prev, edges: [...prev.edges, blankEdge(prev.nodes[0].id, prev.nodes[1].id)] };
    });
  };

  const deleteEdge = (index: number) => {
    setSpec((prev) => (prev ? { ...prev, edges: prev.edges.filter((_, i) => i !== index) } : prev));
  };

  const updateLabelScale = (scale: number) => {
    setSpec((prev) => (prev ? { ...prev, label_scale: scale } : prev));
  };

  const handleReset = () => setSpec(original);

  const handleSave = async () => {
    if (!spec || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await updateDiagramSpec(documentId, blockId, spec);
      const hadLocalEdits = editor.dirty;
      if (!hadLocalEdits) {
        const authoritative = await getDocument(documentId);
        editor.reconcile(authoritative);
      }
      onClose();
    } catch (caught) {
      setSaveError(caught instanceof ApiError ? caught.message : "Could not save the diagram.");
    } finally {
      setSaving(false);
    }
  };

  const dirty = JSON.stringify(spec) !== JSON.stringify(original);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Edit diagram"
      subtitle="Change labels, roles, colors or the node/edge structure - re-rendered instantly, no AI call."
      width="max-w-4xl"
      footer={
        <>
          <Button variant="outline" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="outline" size="sm" disabled={!dirty || loading} onClick={handleReset}>
            Reset to original
          </Button>
          <Button size="sm" disabled={!spec || !dirty || saving} onClick={() => void handleSave()}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      {loading ? (
        <p className="py-10 text-center text-[12.5px] text-ink-500">
          {hasStoredSpec ? "Loading…" : "Reconstructing from the original brief…"}
        </p>
      ) : loadError ? (
        <p className="rounded-[8px] border border-red-200 bg-red-50 px-3 py-2 text-[12.5px] text-red-800">
          {loadError}
        </p>
      ) : spec ? (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="space-y-5">
            <div>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-400">
                Text size
              </h3>
              <div className="flex gap-1.5">
                {TEXT_SIZE_OPTIONS.map((option) => {
                  const active = (spec.label_scale ?? 1) === option.value;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => updateLabelScale(option.value)}
                      className={
                        "flex-1 rounded-[7px] border px-2 py-1.5 text-[12px] font-medium transition " +
                        (active
                          ? "border-brand-400 bg-brand-50 text-brand-700"
                          : "border-line bg-white text-ink-500 hover:bg-ink-50")
                      }
                    >
                      {option.label}
                    </button>
                  );
                })}
              </div>
              <p className="mt-1.5 text-[11px] text-ink-400">
                Scales every label in this diagram. Use Large or Extra large if text looks cramped in the
                preview.
              </p>
            </div>

            <div>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-400">
                Nodes
              </h3>
              <div className="space-y-2">
                {spec.nodes.map((node, index) => (
                  <div key={node.id} className="rounded-[8px] border border-line p-2.5">
                    <div className="flex items-start gap-2">
                      <div className="flex flex-col gap-0.5 pt-0.5">
                        <IconButton
                          aria-label="Move up"
                          disabled={index === 0}
                          onClick={() => moveNode(index, -1)}
                        >
                          <ArrowUp size={12} />
                        </IconButton>
                        <IconButton
                          aria-label="Move down"
                          disabled={index === spec.nodes.length - 1}
                          onClick={() => moveNode(index, 1)}
                        >
                          <ArrowDown size={12} />
                        </IconButton>
                      </div>
                      <div className="flex-1 space-y-1.5">
                        <input
                          className={inputClass}
                          value={node.label}
                          placeholder="Label"
                          onChange={(event) => updateNode(index, { label: event.target.value })}
                        />
                        <input
                          className={inputClass}
                          value={node.detail ?? ""}
                          placeholder="Detail (optional)"
                          onChange={(event) => updateNode(index, { detail: event.target.value })}
                        />
                        <div className="flex items-center gap-1.5">
                          <select
                            className={inputClass}
                            value={node.role ?? ""}
                            onChange={(event) => updateNode(index, { role: event.target.value })}
                          >
                            {ROLE_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                          <input
                            type="color"
                            className="h-[30px] w-10 shrink-0 cursor-pointer rounded-[6px] border border-line bg-white p-0.5"
                            value={/^#[0-9a-fA-F]{6}$/.test(node.color ?? "") ? (node.color as string) : "#bfdbfe"}
                            onChange={(event) => updateNode(index, { color: event.target.value })}
                            title="Custom color (overrides the role default)"
                          />
                          {node.color ? (
                            <button
                              type="button"
                              className="text-[11px] text-ink-400 underline"
                              onClick={() => updateNode(index, { color: "" })}
                            >
                              Clear color
                            </button>
                          ) : null}
                        </div>
                      </div>
                      <IconButton
                        tone="danger"
                        aria-label="Delete node"
                        onClick={() => deleteNode(index)}
                      >
                        <Trash2 size={13} />
                      </IconButton>
                    </div>
                  </div>
                ))}
              </div>
              <Button variant="outline" size="sm" className="mt-2 w-full" onClick={addNode}>
                <Plus size={13} />
                Add node
              </Button>
            </div>

            <div>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-400">
                Edges
              </h3>
              <div className="space-y-2">
                {spec.edges.map((edge, index) => (
                  <div key={index} className="flex items-center gap-1.5 rounded-[8px] border border-line p-2">
                    <select
                      className={inputClass}
                      value={edge.source}
                      onChange={(event) => updateEdge(index, { source: event.target.value })}
                    >
                      {spec.nodes.map((n) => (
                        <option key={n.id} value={n.id}>
                          {nodeLabelFor(spec.nodes, n.id)}
                        </option>
                      ))}
                    </select>
                    <span className="shrink-0 text-ink-400">→</span>
                    <select
                      className={inputClass}
                      value={edge.target}
                      onChange={(event) => updateEdge(index, { target: event.target.value })}
                    >
                      {spec.nodes.map((n) => (
                        <option key={n.id} value={n.id}>
                          {nodeLabelFor(spec.nodes, n.id)}
                        </option>
                      ))}
                    </select>
                    <input
                      className={inputClass}
                      value={edge.label ?? ""}
                      placeholder="Label"
                      onChange={(event) => updateEdge(index, { label: event.target.value })}
                    />
                    <IconButton tone="danger" aria-label="Delete edge" onClick={() => deleteEdge(index)}>
                      <Trash2 size={13} />
                    </IconButton>
                  </div>
                ))}
              </div>
              <Button
                variant="outline"
                size="sm"
                className="mt-2 w-full"
                disabled={spec.nodes.length < 2}
                onClick={addEdge}
              >
                <Plus size={13} />
                Add edge
              </Button>
            </div>

            {saveError ? (
              <p className="rounded-[8px] border border-red-200 bg-red-50 px-3 py-2 text-[12px] text-red-800">
                {saveError}
              </p>
            ) : null}
          </div>

          <div className="lg:sticky lg:top-0">
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-400">
              Preview
            </h3>
            <div className="overflow-hidden rounded-[8px] border border-line bg-white p-2">
              {previewError ? (
                <p className="text-[12px] text-red-700">{previewError}</p>
              ) : previewSvg ? (
                <div className="[&_svg]:h-auto [&_svg]:w-full" dangerouslySetInnerHTML={{ __html: previewSvg }} />
              ) : (
                <p className="py-10 text-center text-[12px] text-ink-400">Rendering…</p>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
