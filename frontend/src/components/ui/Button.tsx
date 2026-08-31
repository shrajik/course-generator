"use client";

import { forwardRef } from "react";
import { cn } from "@/lib/utils/cn";

type Variant = "primary" | "outline" | "ghost" | "danger";
type Size = "sm" | "md";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-brand-600 text-white border border-brand-600 hover:bg-brand-700 hover:border-brand-700 shadow-[0_1px_2px_rgba(76,29,149,0.24)]",
  outline: "bg-white text-ink border border-line hover:bg-brand-50 hover:border-brand-200",
  ghost: "bg-transparent text-ink-500 border border-transparent hover:bg-brand-50 hover:text-ink",
  danger: "bg-white text-danger border border-line hover:bg-red-50 hover:border-red-200",
};

const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-[12.5px] gap-1.5 rounded-[8px]",
  md: "h-10 px-4 text-[13.5px] gap-2 rounded-[10px]",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", className, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center font-medium transition-colors",
        "focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand-100",
        "disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...props}
    />
  );
});
