import { Check, Square, X } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import { stageStatusLabel, type GenerationStage } from "@/lib/generation/stages";

function StageIcon({ status }: { status: GenerationStage["status"] }) {
  if (status === "completed") {
    return (
      <span className="flex h-[18px] w-[18px] items-center justify-center rounded-full bg-success text-white">
        <Check size={11} strokeWidth={3.5} />
      </span>
    );
  }
  if (status === "active") {
    return (
      <span className="relative flex h-[18px] w-[18px] items-center justify-center rounded-full bg-brand-600 text-white">
        <Check size={11} strokeWidth={3.5} />
        <span className="absolute inset-0 animate-ping rounded-full bg-brand-400/40" />
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="flex h-[18px] w-[18px] items-center justify-center rounded-full bg-danger text-white">
        <X size={11} strokeWidth={3.5} />
      </span>
    );
  }
  return (
    <span className="flex h-[18px] w-[18px] items-center justify-center text-ink-300">
      <Square size={13} strokeWidth={2} />
    </span>
  );
}

export function StageList({ stages }: { stages: GenerationStage[] }) {
  return (
    <ol className="space-y-[18px]">
      {stages.map((stage) => (
        <li key={stage.id} className="flex items-start gap-3">
          <span className="mt-[1px] shrink-0">
            <StageIcon status={stage.status} />
          </span>
          <div className="min-w-0">
            <p
              className={cn(
                "text-[13px] font-medium leading-tight",
                stage.status === "pending" ? "text-ink-400" : "text-ink",
              )}
            >
              {stage.label}
            </p>
            <p
              className={cn(
                "mt-0.5 text-[11.5px]",
                stage.status === "active"
                  ? "text-brand-600"
                  : stage.status === "failed"
                    ? "text-danger"
                    : "text-ink-400",
              )}
            >
              {stageStatusLabel(stage)}
              {stage.detail ? ` · ${stage.detail}` : ""}
            </p>
          </div>
        </li>
      ))}
    </ol>
  );
}
