"use client";

import { useEffect, useState } from "react";
import { assetPath } from "@/lib/api/documents";
import { requestText } from "@/lib/api/client";

export function isSvgPath(path: string | null | undefined): boolean {
  return !!path && path.toLowerCase().endsWith(".svg");
}

/**
 * Fetches and returns the raw markup of a diagram's SVG asset so a caller can
 * inline it into the DOM - the hover/focus tooltips and highlighting baked
 * into a generated diagram (see backend app/render/diagram_renderer.py) only
 * work once the SVG is real DOM, not referenced via `<img src>`. Returns null
 * (letting the caller fall back to `<img>`) while disabled, loading or on
 * fetch failure.
 */
export function useInlineDiagramSvg(
  documentId: string | null | undefined,
  path: string | null | undefined,
  enabled: boolean,
): string | null {
  const [markup, setMarkup] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !documentId || !path) {
      setMarkup(null);
      return;
    }
    const relative = assetPath(documentId, path);
    if (!relative) {
      setMarkup(null);
      return;
    }
    const controller = new AbortController();
    requestText(relative, controller.signal)
      .then((text) => setMarkup(text))
      .catch(() => setMarkup(null));
    return () => controller.abort();
  }, [enabled, documentId, path]);

  return markup;
}
