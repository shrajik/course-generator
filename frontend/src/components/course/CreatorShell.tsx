"use client";

import Link from "next/link";
import { useState } from "react";
import { BookOpen, Clock3, FileText, Menu, Plus, Sparkles, X } from "lucide-react";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils/cn";

const NAV_ITEMS = [
  { label: "Create Course", href: "/", icon: Plus },
  { label: "My Courses", href: "/courses", icon: BookOpen },
  { label: "Templates", href: "/templates", icon: FileText },
  { label: "History", href: "/history", icon: Clock3 },
];

function activeFor(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/" || ["/toc", "/generate", "/editor", "/preview"].some((route) => pathname.startsWith(route));
  return pathname === href || pathname.startsWith(`${href}/`);
}

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 text-ink">
      <span className="creator-brand-mark" aria-hidden="true"><Sparkles size={16} /></span>
      <span>
        <span className="block text-[14px] font-semibold tracking-[-0.01em]">AI Course Creator</span>
        <span className="mt-0.5 block text-[10px] font-medium uppercase tracking-[0.13em] text-ink-400">Workspace</span>
      </span>
    </Link>
  );
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <nav className="mt-8 space-y-1.5">
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon;
        const active = activeFor(pathname, item.href);
        return <Link key={item.href} href={item.href} onClick={onNavigate} className={cn("flex h-10 items-center gap-3 rounded-[9px] px-3 text-[13px] font-medium transition-colors duration-200", active ? "bg-brand-50 text-brand-500" : "text-ink-500 hover:bg-canvas hover:text-ink")}><Icon size={17} strokeWidth={active ? 2.2 : 1.8} />{item.label}</Link>;
      })}
    </nav>
  );
}

function Sidebar({ mobile = false, onClose }: { mobile?: boolean; onClose?: () => void }) {
  return <aside className={cn("creator-sidebar", mobile && "creator-sidebar-mobile")}>
    <div className="flex items-start justify-between"><Brand />{mobile ? <button type="button" onClick={onClose} className="creator-icon-button" aria-label="Close navigation"><X size={17} /></button> : null}</div>
    <Navigation onNavigate={onClose} />
    <div className="creator-promo-card"><span className="creator-promo-icon"><Sparkles size={17} /></span><p className="mt-3 text-[13px] font-semibold leading-5 text-ink">Turn your knowledge<br />into engaging courses</p><p className="mt-2 text-[11px] leading-4 text-ink-500">AI-powered. Effortless.<br />For everyone.</p></div>
  </aside>;
}

export function CreatorShell({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  return <div className="creator-app-shell">
    <div className="creator-mobile-header"><Brand /><button type="button" onClick={() => setMobileOpen(true)} className="creator-icon-button" aria-label="Open navigation"><Menu size={19} /></button></div>
    <Sidebar />
    {mobileOpen ? <><button type="button" className="creator-mobile-backdrop" onClick={() => setMobileOpen(false)} aria-label="Close navigation" /><Sidebar mobile onClose={() => setMobileOpen(false)} /></> : null}
    <main className="creator-main-content">{children}</main>
  </div>;
}