"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Folder, Plus } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import type { TreeNode } from "@/lib/kb/model";

interface Props {
  tree: TreeNode[];
  total: number;
  selected: string | null;
  onSelect: (id: string | null) => void;
  /** Creates a category (under `parentId` when given). Resolves once it exists. */
  onCreate: (name: string, parentId: string | null) => Promise<void>;
  onApprove: (id: string) => Promise<void>;
}

/** Browse by category. Selecting one lists everything beneath it too. */
export function CategoryTree({ tree, total, selected, onSelect, onCreate, onApprove }: Props) {
  const [adding, setAdding] = useState<string | "root" | null>(null);
  const [name, setName] = useState("");

  const submit = async (parentId: string | null) => {
    const clean = name.trim();
    if (clean) await onCreate(clean, parentId);
    setName("");
    setAdding(null);
  };

  const form = (parentId: string | null) => (
    <form
      className="flex gap-1 py-1 pl-2"
      onSubmit={(event) => {
        event.preventDefault();
        void submit(parentId);
      }}
    >
      <input
        autoFocus
        value={name}
        onChange={(event) => setName(event.target.value)}
        onBlur={() => !name && setAdding(null)}
        placeholder="Category name"
        className="h-7 min-w-0 flex-1 rounded-[6px] border border-line px-2 text-[12.5px] outline-none focus:border-brand-500"
      />
    </form>
  );

  return (
    <nav aria-label="Categories" className="text-[13px]">
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={cn("flex w-full items-center justify-between rounded-[6px] px-2 py-1.5 text-left", selected === null ? "bg-brand-50 font-medium text-brand-700" : "text-ink-700 hover:bg-brand-50")}
      >
        All knowledge <span className="text-[11.5px] text-ink-400">{total}</span>
      </button>
      <ul className="mt-1">
        {tree.map((node) => (
          <Node key={node.id} node={node} depth={0} {...{ selected, onSelect, adding, setAdding, form, onApprove }} />
        ))}
      </ul>
      {adding === "root" ? (
        form(null)
      ) : (
        <button type="button" onClick={() => setAdding("root")} className="mt-1 flex items-center gap-1.5 px-2 py-1 text-[12px] text-ink-500 hover:text-brand-700">
          <Plus size={13} /> New category
        </button>
      )}
    </nav>
  );
}

function Node({
  node, depth, selected, onSelect, adding, setAdding, form, onApprove,
}: {
  node: TreeNode;
  depth: number;
  selected: string | null;
  onSelect: (id: string | null) => void;
  adding: string | null;
  setAdding: (value: string | null) => void;
  form: (parentId: string | null) => React.ReactNode;
  onApprove: (id: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(true);
  const hasChildren = node.children.length > 0;
  return (
    <li>
      <div
        className={cn("group flex items-center gap-1 rounded-[6px] pr-1", selected === node.id ? "bg-brand-50 text-brand-700" : "text-ink-700 hover:bg-brand-50")}
        style={{ paddingLeft: 4 + depth * 14 }}
      >
        <button type="button" aria-label={open ? "Collapse" : "Expand"} onClick={() => setOpen(!open)} className={cn("p-0.5 text-ink-400", !hasChildren && "invisible")}>
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </button>
        <button type="button" onClick={() => onSelect(node.id)} className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 text-left">
          <Folder size={13} className="shrink-0 text-ink-400" />
          <span className="truncate">{node.name}</span>
          {node.is_suggested ? <span className="rounded bg-amber-100 px-1 text-[10px] font-medium text-amber-800">suggested</span> : null}
        </button>
        {node.is_suggested ? (
          <button type="button" onClick={() => void onApprove(node.id)} className="text-[11px] text-brand-700 underline">
            approve
          </button>
        ) : null}
        <span className="text-[11.5px] text-ink-400">{node.total}</span>
        <button type="button" aria-label={`Add a category under ${node.name}`} onClick={() => setAdding(node.id)} className="p-0.5 text-ink-400 opacity-0 hover:text-brand-700 group-hover:opacity-100">
          <Plus size={12} />
        </button>
      </div>
      {adding === node.id ? <div style={{ paddingLeft: 18 + depth * 14 }}>{form(node.id)}</div> : null}
      {open && hasChildren ? (
        <ul>
          {node.children.map((child) => (
            <Node key={child.id} node={child} depth={depth + 1} {...{ selected, onSelect, adding, setAdding, form, onApprove }} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}
