import { cn } from "@/lib/utils/cn";

interface StepHeaderProps {
  step: number;
  title: string;
  subtitle?: string;
  className?: string;
  children?: React.ReactNode;
}

/** The numbered violet badge + title/subtitle pattern used on every screen. */
export function StepHeader({ step, title, subtitle, className, children }: StepHeaderProps) {
  return (
    <div className={cn("flex items-start justify-between gap-4", className)}>
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-[7px] bg-brand-600 text-[12px] font-semibold text-white">
          {step}
        </span>
        <div>
          <h1 className="text-[19px] font-bold leading-tight tracking-[-0.01em] text-ink">
            {title}
          </h1>
          {subtitle ? <p className="mt-1 text-[12.5px] text-ink-500">{subtitle}</p> : null}
        </div>
      </div>
      {children ? <div className="flex shrink-0 items-center gap-2">{children}</div> : null}
    </div>
  );
}
