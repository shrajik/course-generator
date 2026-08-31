"use client";

import { cn } from "@/lib/utils/cn";

interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
  tone?: "default" | "danger";
}

export function IconButton({
  active = false,
  tone = "default",
  className,
  ...props
}: IconButtonProps) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-[7px] transition-colors",
        "focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand-100",
        "disabled:cursor-not-allowed disabled:opacity-40",
        active
          ? "bg-brand-100 text-brand-700"
          : tone === "danger"
            ? "text-ink-400 hover:bg-red-50 hover:text-danger"
            : "text-ink-400 hover:bg-brand-50 hover:text-brand-700",
        className,
      )}
      {...props}
    />
  );
}
