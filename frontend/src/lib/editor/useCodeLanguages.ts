"use client";

import { useEffect, useState } from "react";
import { listCodeLanguages, type CodeLanguage } from "@/lib/api/code";

/**
 * The languages the executor can actually run.
 *
 * Shared across every code cell on the page: a document can hold dozens, and
 * each asking the backend on mount would be dozens of identical requests. One
 * in-flight request is reused, and the answer is cached briefly (the list only
 * changes when the executor image is rebuilt).
 */

interface Snapshot {
  languages: CodeLanguage[];
  /** null until the first answer arrives. */
  available: boolean | null;
}

const TTL_MS = 60_000;
let cached: { at: number; value: Snapshot } | null = null;
let inFlight: Promise<Snapshot> | null = null;

function load(): Promise<Snapshot> {
  if (cached && Date.now() - cached.at < TTL_MS) return Promise.resolve(cached.value);
  if (inFlight) return inFlight;

  inFlight = listCodeLanguages()
    .then((response): Snapshot => ({ languages: response.languages, available: response.available }))
    // A failed request means "could not ask", which is different from "there
    // are none" - the editor words the two differently.
    .catch((): Snapshot => ({ languages: [], available: false }))
    .then((value) => {
      // An unreachable executor is not cached: it should be retried promptly.
      if (value.available) cached = { at: Date.now(), value };
      inFlight = null;
      return value;
    });
  return inFlight;
}

export function useCodeLanguages(): Snapshot {
  const [snapshot, setSnapshot] = useState<Snapshot>(
    () => cached?.value ?? { languages: [], available: null },
  );

  useEffect(() => {
    let active = true;
    void load().then((value) => {
      if (active) setSnapshot(value);
    });
    return () => {
      active = false;
    };
  }, []);

  return snapshot;
}
