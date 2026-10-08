/**
 * Code execution API.
 *
 * Language-agnostic by design: there is one endpoint, and the editor only ever
 * sends a language id and some code. Which runtime handles it, and how it is
 * sandboxed, is the backend's business - so supporting a new language needs no
 * change here.
 */

import { request } from "./client";
import type { ExecutionStatus } from "@/lib/content/code-cell";

export interface CodeLanguage {
  id: string;
  label: string;
  highlight: string;
  file_extension: string;
  version: string;
  aliases: string[];
}

export interface CodeLanguagesResponse {
  languages: CodeLanguage[];
  /** False when the executor could not be reached at all. */
  available: boolean;
}

/** The executor's normalised result - identical for every language. */
export interface ExecuteResult {
  status: ExecutionStatus;
  language: string;
  stdout: string;
  stderr: string;
  execution_time: number;
  phase?: "compile" | "run" | null;
  exit_code?: number | null;
  truncated?: boolean;
}

export function listCodeLanguages(): Promise<CodeLanguagesResponse> {
  return request<CodeLanguagesResponse>("/api/code/languages");
}

export function executeCode(language: string, code: string): Promise<ExecuteResult> {
  return request<ExecuteResult>("/api/code/execute", {
    method: "POST",
    body: { language, code },
  });
}
