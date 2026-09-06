"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, Loader2 } from "lucide-react";

export function AdminPageHeader({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <section className="admin-hero">
      <div>
        <p className="admin-eyebrow">{eyebrow}</p>
        <h2 className="admin-title">{title}</h2>
        <p className="admin-description">{description}</p>
      </div>
      <div className="admin-hero-mark" aria-hidden="true" />
    </section>
  );
}

export function AdminPanel({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <section className={`admin-panel ${className}`}>{children}</section>;
}

export function AdminAlert({ children }: { children: React.ReactNode }) {
  return (
    <section className="admin-alert" role="alert">
      <CircleAlert size={17} />
      <span>{children}</span>
    </section>
  );
}

export function AdminLoading({ label }: { label: string }) {
  return (
    <div className="admin-state">
      <Loader2 size={18} className="animate-spin text-brand-500" />
      <span>{label}</span>
    </div>
  );
}

export function AdminEmpty({ children }: { children: React.ReactNode }) {
  return <div className="admin-empty">{children}</div>;
}

export function AdminStatusBadge({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "success" | "warning" | "danger";
  children: React.ReactNode;
}) {
  const Icon = tone === "success" ? CheckCircle2 : undefined;
  return (
    <span className={`admin-badge admin-badge-${tone}`}>
      {Icon ? <Icon size={13} /> : null}
      {children}
    </span>
  );
}

export function AnimatedNumber({ value }: { value: number }) {
  const [displayValue, setDisplayValue] = useState(0);

  useEffect(() => {
    const start = performance.now();
    const duration = 520;
    const frame = (now: number) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setDisplayValue(Math.round(value * eased));
      if (progress < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }, [value]);

  return <>{displayValue.toLocaleString()}</>;
}