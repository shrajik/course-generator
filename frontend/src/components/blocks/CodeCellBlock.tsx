"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Play } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { executeCode } from "@/lib/api/code";
import {
  CELL,
  CELL_MONO_FONT,
  codeLines,
  outputLabel,
  outputState,
  type CodeExecution,
} from "@/lib/content/code-cell";
import { useCodeLanguages } from "@/lib/editor/useCodeLanguages";
import { cn } from "@/lib/utils/cn";
import { EditableText } from "./EditableText";
import { Caption, readText } from "./parts";
import type { BlockViewProps } from "./types";

/**
 * An executable code cell, laid out like a Colab / Jupyter cell.
 *
 *   head row     language ▾                                  ▶ Run
 *   code box     light grey, bordered, numbered lines
 *   output box   white, bordered - a SEPARATE box below, never inside the code
 *
 * Code and output are two independent boxes in normal flow. Nothing here is
 * absolutely positioned, so each box takes the height of its own content and
 * the block grows naturally.
 *
 * Every size comes from CELL (lib/content/code-cell.ts), which mirrors
 * backend/app/course/document/code_cell.py: that is what the layout engine
 * reserves room with and what the PDF draws with. Keeping the editor on the
 * same numbers is what stops the cell from outgrowing its slot and being
 * painted over the block below it.
 *
 * The cell is language-agnostic. It holds a language id and some code, and
 * "Run" sends both to one endpoint; the output box shows the normalised result
 * (status, stdout, stderr, time) and knows nothing about how it was produced.
 *
 * State, kept apart:
 *   stored   content.code / content.language / content.execution   (saved)
 *   running  a transient flag and error                            (never saved)
 *   display  derived: output is shown only while it still belongs to the code
 */
export function CodeCellBlock({ block, editable, interactive, onEdit, onResize }: BlockViewProps) {
  const { languages, available } = useCodeLanguages();
  const rootRef = useRef<HTMLDivElement>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  // Set once the user has done something that can change this cell's height.
  // Measuring on mount would "fix" a mismatch by editing the document just by
  // opening it, which marks the course as having unsaved changes.
  const touched = useRef(false);

  const code = readText(block.content, "code");
  const language = readText(block.content, "language") || "python";
  const output = outputState(block.content);
  const current = output.kind === "current" ? output.execution : null;

  // Only languages the backend can really run are offered. A cell whose stored
  // language is not among them keeps it, labelled, rather than being silently
  // switched to something else.
  const known = languages.some((item) => item.id === language);
  const runnable = available === true && known;

  /** Run the cell, then record the result on the block (persisted with it). */
  const run = async () => {
    if (!runnable || running) return;
    touched.current = true;
    // While the code is being edited it is only committed on blur, so read
    // what is actually on screen rather than the last committed value.
    const live = editable ? rootRef.current?.querySelector("pre")?.innerText : undefined;
    const source = live ?? code;

    setRunning(true);
    setRunError(null);
    try {
      const result = await executeCode(language, source);
      // `source` snapshots the code that produced this output, which is what
      // lets the editor and the PDF tell fresh output from stale.
      onEdit("execution", { ...result, source });
    } catch (caught) {
      setRunError(caught instanceof ApiError ? caught.message : "The code could not be run.");
    } finally {
      setRunning(false);
    }
  };

  // Whatever the user does, if the cell ends up taller than the room reserved
  // for it, ask the editor to make room (and move what is below). Watching the
  // real DOM rather than estimating means any cause is covered: output
  // arriving, more lines of code, a language change, a font finishing loading.
  useEffect(() => {
    const node = rootRef.current;
    if (!node || !onResize || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (touched.current) onResize(Math.ceil(node.offsetHeight));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [onResize]);

  // Pointer events inside the controls must not start a drag or change the
  // selection of the block that contains them.
  const stop = (event: React.SyntheticEvent) => event.stopPropagation();

  const label = languages.find((item) => item.id === language)?.label ?? language;

  return (
    <div ref={rootRef} style={{ fontFamily: CELL_MONO_FONT }}>
      {/* head row ----------------------------------------------------- */}
      <div
        className="flex items-center justify-between gap-2"
        style={{ height: CELL.headH }}
        onPointerDown={stop}
        onDoubleClick={stop}
      >
        {interactive ? (
          <select
            value={language}
            onChange={(event) => {
              touched.current = true;
              onEdit("language", event.target.value);
            }}
            disabled={available !== true}
            aria-label="Code language"
            className="h-[26px] rounded-[6px] border border-[#dadce0] bg-white px-2 font-sans text-[12px] font-medium text-[#3c4043] outline-none focus:border-brand-400"
          >
            {languages.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
            {!known ? <option value={language}>{language} (unavailable)</option> : null}
          </select>
        ) : (
          <span className="font-sans text-[11px] font-bold uppercase tracking-[0.09em] text-[#5f6368]">
            {label} Code
          </span>
        )}

        {interactive ? (
          <button
            type="button"
            onClick={() => void run()}
            disabled={!runnable || running}
            title={
              available === false
                ? "Code execution is unavailable right now"
                : !known
                  ? `${language} cannot be run here`
                  : "Run this cell"
            }
            className={cn(
              "inline-flex h-[26px] items-center gap-1.5 rounded-[6px] px-2.5 font-sans text-[12px] font-semibold",
              "bg-brand-600 text-white transition-opacity hover:opacity-90",
              "disabled:cursor-not-allowed disabled:opacity-40",
            )}
          >
            {running ? (
              <Loader2 size={13} className="animate-spin" aria-hidden />
            ) : (
              <Play size={13} aria-hidden />
            )}
            {running ? "Running" : "Run"}
          </button>
        ) : null}
      </div>

      {/* code box ----------------------------------------------------- */}
      <div
        style={{
          border: `${CELL.border}px solid #dadce0`,
          borderRadius: 6,
          background: "#f8f9fa",
          padding: `${CELL.codePadY}px 0`,
          fontSize: CELL.font,
          lineHeight: `${CELL.rowH}px`,
          color: "#202124",
        }}
      >
        {editable ? (
          <div className="flex">
            {/* Same width as the number column, so text does not shift left when
                editing starts. Numbers return when the edit is committed. */}
            <div aria-hidden style={{ flex: `0 0 ${CELL.gutterW}px` }} />
            <EditableText
              as="pre"
              value={code}
              editable
              onCommit={(value) => {
                touched.current = true;
                onEdit("code", value);
              }}
              placeholder="Code"
              className="m-0 min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]"
              style={{ paddingRight: CELL.textPadR, tabSize: 4, fontFamily: "inherit" }}
            />
          </div>
        ) : (
          codeLines(code).map((line, index) => (
            <div key={index} className="flex" style={{ minHeight: CELL.rowH }}>
              <span
                aria-hidden
                className="select-none text-right text-[#9aa0a6]"
                style={{ flex: `0 0 ${CELL.gutterW}px`, width: CELL.gutterW, paddingRight: 10 }}
              >
                {index + 1}
              </span>
              <span
                className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]"
                style={{ paddingRight: CELL.textPadR, tabSize: 4 }}
              >
                {line}
              </span>
            </div>
          ))
        )}
      </div>

      <Caption
        text={readText(block.content, "caption")}
        editable={editable}
        onCommit={(value) => onEdit("caption", value)}
      />

      {/* output box --------------------------------------------------- */}
      <OutputBox
        running={running}
        runError={runError}
        state={output.kind}
        execution={current}
        interactive={!!interactive}
      />
    </div>
  );
}

const TONE = {
  success: { head: "#137333", bg: "#ffffff", border: "#e0e0e0" },
  error: { head: "#b3261e", bg: "#fdf6f5", border: "#f1d4d1" },
  timeout: { head: "#b06000", bg: "#fffaf0", border: "#f3e1b8" },
  stale: { head: "#b06000", bg: "#fffaf0", border: "#f3e1b8" },
  neutral: { head: "#5f6368", bg: "#ffffff", border: "#e0e0e0" },
} as const;

/**
 * The output area. It is always one box with a one-line header, and a body only
 * when there is real output to show - so every state that has no output (never
 * run, running, stale, could not run) is the same height, and the room
 * reserved for the cell is the room it takes.
 */
function OutputBox({
  running,
  runError,
  state,
  execution,
  interactive,
}: {
  running: boolean;
  runError: string | null;
  state: "none" | "current" | "stale";
  execution: CodeExecution | null;
  interactive: boolean;
}) {
  let tone: (typeof TONE)[keyof typeof TONE] = TONE.neutral;
  let head = "OUTPUT · NOT EXECUTED";
  let note = interactive ? "Run the cell to see its output." : "";
  let body: React.ReactNode = null;

  if (running) {
    head = "OUTPUT · RUNNING…";
    note = "";
  } else if (runError) {
    tone = TONE.error;
    head = "COULD NOT RUN";
    note = runError;
  } else if (state === "stale") {
    // Old output is deliberately not rendered at all - only the fact it is old.
    tone = TONE.stale;
    head = "OUTPUT · STALE";
    note = interactive
      ? "Code changed since last execution. Run again to update output."
      : "Code changed since the last run; output not shown.";
  } else if (execution) {
    tone = TONE[execution.status];
    head = outputLabel(execution);
    note = "";
    const stdout = execution.stdout;
    const stderr = execution.stderr;
    const empty = !stdout.trim() && !stderr.trim();
    // Long output scrolls inside its own box rather than being cut off or
    // pushing the page around. 40 rows is also the most the PDF prints, so the
    // height reserved for this cell is never less than what is shown here.
    body = (
      <div
        style={{
          marginTop: CELL.outHeadGap,
          maxHeight: CELL.maxOutputLines * CELL.rowH,
          overflowY: "auto",
        }}
      >
        {stdout.trim() ? <Stream text={stdout} /> : null}
        {stderr.trim() ? <Stream text={stderr} error gap={!!stdout.trim()} /> : null}
        {empty ? <Stream text="(no output)" muted /> : null}
        {execution.truncated ? <Stream text="Output was cut short." muted /> : null}
      </div>
    );
  }

  return (
    <div
      style={{
        marginTop: CELL.outGap,
        border: `${CELL.border}px solid ${tone.border}`,
        borderRadius: 6,
        background: tone.bg,
        padding: `${CELL.outPadY}px ${CELL.outPadX}px`,
        fontSize: CELL.font,
        lineHeight: `${CELL.rowH}px`,
        color: "#202124",
      }}
    >
      <div
        className="overflow-hidden text-ellipsis whitespace-nowrap font-sans"
        style={{
          height: CELL.outHeadH,
          lineHeight: `${CELL.outHeadH}px`,
          fontSize: 10.5,
          fontWeight: 700,
          letterSpacing: "0.06em",
          color: tone.head,
        }}
        title={note ? `${head} - ${note}` : head}
      >
        {running ? <Loader2 size={11} className="mr-1 inline animate-spin align-[-1px]" aria-hidden /> : null}
        {head}
        {note ? (
          <span style={{ fontWeight: 400, letterSpacing: 0, color: "#80868b" }}> &mdash; {note}</span>
        ) : null}
      </div>
      {body}
    </div>
  );
}

function Stream({
  text,
  error,
  muted,
  gap,
}: {
  text: string;
  error?: boolean;
  muted?: boolean;
  gap?: boolean;
}) {
  return (
    <pre
      className="m-0 whitespace-pre-wrap [overflow-wrap:anywhere]"
      style={{
        fontFamily: "inherit",
        marginTop: gap ? CELL.streamGap : 0,
        color: error ? "#b3261e" : muted ? "#80868b" : "#202124",
        fontStyle: muted ? "italic" : undefined,
      }}
    >
      {text}
    </pre>
  );
}
