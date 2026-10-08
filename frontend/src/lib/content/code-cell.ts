/**
 * Code cell content and the rule that decides whether its output may be shown.
 *
 * A cell stores the result of its last run in `execution`, together with
 * `source` - the exact code that produced it. Output is only ever presented
 * while `source` still equals the cell's current code and language. The
 * moment either changes, the output no longer belongs to the cell and must not
 * be shown as if it did.
 *
 * Mirrors `execution_is_current` in backend/app/schemas/blocks.py, which the
 * PDF renderer uses; the two must agree or the editor and the export would
 * disagree about what a cell's output is.
 */

export type ExecutionStatus = "success" | "error" | "timeout";

export interface CodeExecution {
  status: ExecutionStatus;
  language: string;
  stdout: string;
  stderr: string;
  execution_time: number;
  phase?: "compile" | "run" | null;
  exit_code?: number | null;
  truncated?: boolean;
  /** The exact code this result came from. */
  source: string;
}

export interface CodeCellContent {
  language: string;
  code: string;
  caption?: string;
  execution?: CodeExecution | null;
}

/** Narrow an untyped stored value to an execution record, or null. */
export function readExecution(value: unknown): CodeExecution | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (record.status !== "success" && record.status !== "error" && record.status !== "timeout") {
    return null;
  }
  return {
    status: record.status,
    language: typeof record.language === "string" ? record.language : "",
    stdout: typeof record.stdout === "string" ? record.stdout : "",
    stderr: typeof record.stderr === "string" ? record.stderr : "",
    execution_time: typeof record.execution_time === "number" ? record.execution_time : 0,
    phase: record.phase === "compile" || record.phase === "run" ? record.phase : null,
    exit_code: typeof record.exit_code === "number" ? record.exit_code : null,
    truncated: record.truncated === true,
    source: typeof record.source === "string" ? record.source : "",
  };
}

export type OutputState =
  /** Never run. */
  | { kind: "none" }
  /** Ran, and the code has not changed since: safe to show. */
  | { kind: "current"; execution: CodeExecution }
  /** Ran, but the code or language has changed since: must not be shown. */
  | { kind: "stale" };

export function outputState(content: {
  code?: unknown;
  language?: unknown;
  execution?: unknown;
}): OutputState {
  const execution = readExecution(content.execution);
  if (execution === null) return { kind: "none" };

  const code = typeof content.code === "string" ? content.code : "";
  const language = typeof content.language === "string" ? content.language : "";
  const sameCode = execution.source === code;
  const sameLanguage = (execution.language || language) === language;
  return sameCode && sameLanguage ? { kind: "current", execution } : { kind: "stale" };
}

/** A one-line description of how a run ended, for the output header. */
export function describeOutcome(execution: CodeExecution): string {
  if (execution.status === "timeout") return "Timed out";
  if (execution.status === "error") {
    return execution.phase === "compile" ? "Compilation failed" : "Error";
  }
  return "Success";
}

// --- geometry ---------------------------------------------------------------
// Every number that decides how tall a cell is. The same values live in
// backend/app/course/document/code_cell.py, which is what the layout engine
// reserves room with and what the PDF draws with; if the two disagree the
// next block is painted over this one. Change them together.

export const CELL = {
  headH: 30,
  border: 1,
  font: 12.5,
  rowH: 18,
  codePadY: 8,
  gutterW: 40,
  textPadR: 12,
  outGap: 8,
  outPadY: 8,
  outPadX: 12,
  outHeadH: 20,
  outHeadGap: 6,
  streamGap: 4,
  /** Lines of output shown before scrolling - what the PDF prints. */
  maxOutputLines: 40,
} as const;

/** Same stack as the PDF's theme.mono_font_family, so a line wraps at the same
 * place in both. */
export const CELL_MONO_FONT = '"JetBrains Mono", Consolas, Menlo, monospace';

/** The rows a cell shows, one numbered row each (mirrors `code_lines`). */
export function codeLines(code: string): string[] {
  const lines = (code ?? "").replace(/\r\n?/g, "\n").split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

const STATUS_WORD: Record<ExecutionStatus, string> = {
  success: "SUCCESS",
  error: "ERROR",
  timeout: "TIMEOUT",
};

/** "OUTPUT · SUCCESS · 0.02s" - the header of a current output box. */
export function outputLabel(execution: CodeExecution): string {
  return `OUTPUT · ${STATUS_WORD[execution.status]} · ${execution.execution_time.toFixed(2)}s`;
}
