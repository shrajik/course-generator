"use client";

import { EditableText } from "./EditableText";
import { Caption, readText } from "./parts";
import { codePanelStyle } from "@/lib/editor/style";
import type { BlockViewProps } from "./types";

export function CodeBlock({ block, editable, onEdit }: BlockViewProps) {
  return (
    <div>
      <div style={codePanelStyle(block.style)}>
        <EditableText
          as="pre"
          value={readText(block.content, "code")}
          editable={editable}
          onCommit={(value) => onEdit("code", value)}
          placeholder="Code"
          className="m-0 whitespace-pre-wrap break-words font-mono"
        />
      </div>
      <Caption
        text={readText(block.content, "caption")}
        editable={editable}
        onCommit={(value) => onEdit("caption", value)}
      />
    </div>
  );
}
