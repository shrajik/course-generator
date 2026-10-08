"use client";

import { EditableText } from "./EditableText";
import { readText } from "./parts";
import type { BlockViewProps } from "./types";

export function ParagraphBlock({ block, editable, onEdit, wrapAround }: BlockViewProps) {
  return (
    <>
      {wrapAround ? (
        // A section-intro illustration sits in this corner (see
        // lib/editor/pairing.ts). The floated spacer makes the text flow around
        // it instead of running underneath, exactly as the PDF's CSS float does.
        <div
          aria-hidden
          style={{ float: "left", width: wrapAround.width, height: wrapAround.height }}
        />
      ) : null}
      <EditableText
        as="div"
        value={readText(block.content, "text")}
        editable={editable}
        onCommit={(value) => onEdit("text", value)}
        placeholder="Paragraph text"
        className="whitespace-pre-wrap"
      />
    </>
  );
}
