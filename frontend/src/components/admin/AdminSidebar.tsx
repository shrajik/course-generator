"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import { ADMIN_NAVIGATION, ADMIN_SECONDARY_NAVIGATION } from "./navigation";

function isActiveRoute(pathname: string, href: string): boolean {
  return href === "/admin" ? pathname === href : pathname.startsWith(href);
}

export function AdminSidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden min-h-screen w-[248px] shrink-0 border-r border-line bg-white px-4 py-5 md:block">
      <Link href="/admin" className="flex items-center gap-2.5 px-2 text-ink">
        <span className="flex h-9 w-9 items-center justify-center rounded-[10px] border border-brand-200 bg-brand-50 text-brand-500">
          <ShieldCheck size={17} />
        </span>
        <span>
          <span className="block text-[14px] font-semibold">Course Admin</span>
          <span className="mt-0.5 block text-[10px] font-medium uppercase tracking-[0.12em] text-ink-400">Workspace</span>
        </span>
      </Link>

      <nav className="mt-8 space-y-1">
        {ADMIN_NAVIGATION.map((item) => {
          const Icon = item.icon;
          const active = isActiveRoute(pathname, item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex h-10 items-center gap-2.5 rounded-[9px] border px-3 text-[13px] font-medium transition-colors duration-200",
                active
                  ? "border-brand-200 bg-brand-50 text-ink"
                  : "border-transparent text-ink-500 hover:border-line hover:bg-canvas hover:text-ink",
              )}
            >
              <Icon size={16} className={active ? "text-brand-500" : "text-ink-400"} />
              {item.label}
            </Link>
          );
        })}
        <div className="my-4 border-t border-line" />
        {ADMIN_SECONDARY_NAVIGATION.map((item) => {
          const Icon = item.icon;
          return (
            <span
              key={item.label}
              className="flex h-10 cursor-not-allowed items-center gap-2.5 rounded-[9px] border border-transparent px-3 text-[13px] font-medium text-ink-400"
              title={`${item.label} is not available yet`}
            >
              <Icon size={16} className="text-ink-300" />
              {item.label}
            </span>
          );
        })}
      </nav>
    </aside>
  );
}
