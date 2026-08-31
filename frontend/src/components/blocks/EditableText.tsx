"use client";

import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils/cn";

interface EditableTextProps {
  value: string;
  editable: boolean;
  onCommit: (value: string) => void;
  className?: string;
  style?: React.CSSProperties;
  placeholder?: string;
  as?: "div" | "span" | "p" | "h1" | "h2" | "h3" | "h4" | "li" | "td" | "pre";
  multiline?: boolean;
}

/**
 * A contentEditable text run. React never re-renders the node while it has
 * focus, which keeps the caret stable; the value is committed on blur.
 */
export function EditableText({
  value,
  editable,
  onCommit,
  className,
  style,
  placeholder,
  as = "div",
  multiline = true,
}: EditableTextProps) {
  const ref = useRef<HTMLElement>(null);
  const Tag = as as keyof React.JSX.IntrinsicElements;

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (document.activeElement === node) return;
    if (node.innerText !== value) node.innerText = value;
  }, [value]);

  if (!editable) {
    return (
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <Tag className={className} style={style} {...({} as any)}>
        {value}
      </Tag>
    );
  }

  return (
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <Tag
      {...({
        ref,
        contentEditable: true,
        suppressContentEditableWarning: true,
        spellCheck: true,
        "data-placeholder": placeholder,
        className: cn("cursor-text focus:bg-brand-50/40", className),
        style,
        onBlur: (event: React.FocusEvent<HTMLElement>) => {
          const next = event.currentTarget.innerText.replace(/ /g, " ");
          if (next !== value) onCommit(next);
        },
        onKeyDown: (event: React.KeyboardEvent<HTMLElement>) => {
          if (event.key === "Escape") {
            event.currentTarget.innerText = value;
            event.currentTarget.blur();
            return;
          }
          if (event.key === "Enter" && !multiline) {
            event.preventDefault();
            event.currentTarget.blur();
          }
          // Keep editor shortcuts from firing while typing.
          event.stopPropagation();
        },
        onPaste: (event: React.ClipboardEvent<HTMLElement>) => {
          event.preventDefault();
          const text = event.clipboardData.getData("text/plain");
          document.execCommand("insertText", false, text);
        },
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any)}
    />
  );
}
