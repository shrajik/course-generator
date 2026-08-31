"use client";

import { EditableText } from "./EditableText";
import { BlockLabel, readText } from "./parts";
import { asStringArray } from "@/lib/editor/blocks";
import { accentColor } from "@/lib/editor/style";
import type { QuizQuestion } from "@/lib/types/document";
import type { BlockViewProps } from "./types";

export function QuizBlock({ block, editable, onEdit }: BlockViewProps) {
  const questions = Array.isArray(block.content.questions)
    ? (block.content.questions as QuizQuestion[])
    : [];

  return (
    <div>
      <BlockLabel
        text={readText(block.content, "title") || "Knowledge Check"}
        color={accentColor(block)}
        editable={editable}
        onCommit={(value) => onEdit("title", value)}
      />
      <div className="space-y-3">
        {questions.map((question, index) => (
          <div key={index}>
            <div className="flex gap-1.5 font-semibold">
              <span className="tabular-nums">{index + 1}.</span>
              <EditableText
                as="span"
                value={question?.question ?? ""}
                editable={editable}
                onCommit={(value) => onEdit(`questions.${index}.question`, value)}
                placeholder="Question"
              />
            </div>
            {asStringArray(question?.options).length > 0 ? (
              <ol className="mt-1 list-[upper-alpha] space-y-0.5 pl-6">
                {asStringArray(question?.options).map((option, optionIndex) => (
                  <li key={optionIndex}>
                    <EditableText
                      as="span"
                      value={option}
                      editable={editable}
                      multiline={false}
                      onCommit={(value) =>
                        onEdit(`questions.${index}.options.${optionIndex}`, value)
                      }
                    />
                  </li>
                ))}
              </ol>
            ) : null}
            {question?.answer ? (
              <p className="mt-1 text-[0.92em] text-ink-500">
                <span className="font-semibold">Answer:</span>{" "}
                <EditableText
                  as="span"
                  value={question.answer}
                  editable={editable}
                  multiline={false}
                  onCommit={(value) => onEdit(`questions.${index}.answer`, value)}
                />
                {question.explanation ? (
                  <>
                    {" — "}
                    <EditableText
                      as="span"
                      value={question.explanation}
                      editable={editable}
                      onCommit={(value) => onEdit(`questions.${index}.explanation`, value)}
                    />
                  </>
                ) : null}
              </p>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}
