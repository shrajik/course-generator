"use client";

import { EditableText } from "./EditableText";
import { Caption, readText } from "./parts";
import { asStringArray } from "@/lib/editor/blocks";
import type { TableRow } from "@/lib/types/document";
import type { BlockViewProps } from "./types";

export function TableBlock({ block, editable, onEdit }: BlockViewProps) {
  const columns = asStringArray(block.content.columns);
  const rows = Array.isArray(block.content.rows) ? (block.content.rows as TableRow[]) : [];
  const border = block.style.border_color ?? "#E2E8F0";
  const headerBg = block.style.accent_color ?? "#F1F5F9";

  return (
    <div>
      <table className="w-full border-collapse text-left">
        {columns.length > 0 ? (
          <thead>
            <tr>
              {columns.map((column, index) => (
                <th
                  key={index}
                  className="px-2.5 py-2 align-top font-bold"
                  style={{ background: headerBg, borderBottom: `1px solid ${border}` }}
                >
                  <EditableText
                    as="span"
                    value={column}
                    editable={editable}
                    multiline={false}
                    onCommit={(value) => onEdit(`columns.${index}`, value)}
                  />
                </th>
              ))}
            </tr>
          </thead>
        ) : null}
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {asStringArray(row?.cells).map((cell, cellIndex) => (
                <td
                  key={cellIndex}
                  className="px-2.5 py-2 align-top"
                  style={{ borderBottom: `1px solid ${border}` }}
                >
                  <EditableText
                    as="span"
                    value={cell}
                    editable={editable}
                    onCommit={(value) =>
                      onEdit(`rows.${rowIndex}.cells.${cellIndex}`, value)
                    }
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <Caption
        text={readText(block.content, "caption")}
        editable={editable}
        onCommit={(value) => onEdit("caption", value)}
      />
    </div>
  );
}
