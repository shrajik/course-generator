"use client";

import {
  Blocks,
  Image as ImageIcon,
  LayoutGrid,
  Shapes,
  Sparkles,
  Type,
  Upload,
} from "lucide-react";
import { useEditor } from "@/lib/editor/store";
import { cn } from "@/lib/utils/cn";

export type RailPanel = "pages" | "blocks" | "uploads";

interface LeftRailProps {
  panel: RailPanel;
  onPanelChange: (panel: RailPanel) => void;
  onFocusAssistant: () => void;
}

export function LeftRail({ panel, onPanelChange, onFocusAssistant }: LeftRailProps) {
  const editor = useEditor();
  const anchor = editor.selectedIds[0] ?? null;

  const items = [
    {
      id: "pages",
      label: "Pages",
      Icon: LayoutGrid,
      active: panel === "pages",
      onClick: () => onPanelChange("pages"),
    },
    {
      id: "text",
      label: "Text",
      Icon: Type,
      active: false,
      onClick: () => editor.insertBlock("paragraph", anchor),
    },
    {
      id: "image",
      label: "Image",
      Icon: ImageIcon,
      active: false,
      onClick: () => editor.insertBlock("image", anchor),
    },
    {
      id: "shapes",
      label: "Shapes",
      Icon: Shapes,
      active: false,
      onClick: () => editor.insertBlock("divider", anchor),
    },
    {
      id: "blocks",
      label: "Blocks",
      Icon: Blocks,
      active: panel === "blocks",
      onClick: () => onPanelChange("blocks"),
    },
    {
      id: "uploads",
      label: "Uploads",
      Icon: Upload,
      active: panel === "uploads",
      onClick: () => onPanelChange("uploads"),
    },
    {
      id: "assistant",
      label: "AI Assistant",
      Icon: Sparkles,
      active: false,
      onClick: onFocusAssistant,
    },
  ];

  return (
    <nav className="flex w-[104px] shrink-0 flex-col gap-1 border-r border-line bg-white p-2.5">
      {items.map(({ id, label, Icon, active, onClick }) => (
        <button
          key={id}
          type="button"
          onClick={onClick}
          className={cn(
            "flex items-center gap-2 rounded-[8px] px-2 py-2 text-[11.5px] font-medium transition-colors",
            active
              ? "bg-brand-100 text-brand-700"
              : "text-ink-500 hover:bg-brand-50 hover:text-ink",
          )}
        >
          <Icon size={14} className={active ? "text-brand-600" : "text-ink-400"} />
          {label}
        </button>
      ))}
    </nav>
  );
}
