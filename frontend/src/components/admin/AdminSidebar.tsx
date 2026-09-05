"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils/cn";
import { ADMIN_NAVIGATION } from "./navigation";

function isActiveRoute(pathname: string, href: string): boolean {
  return href === "/admin" ? pathname === href : pathname.startsWith(href);
}

export function AdminSidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden min-h-screen w-[248px] shrink-0 border-r border-[#2A2825] bg-[#151413] px-4 py-5 md:block">
      <Link href="/admin" className="flex items-center gap-2.5 px-2 text-[#F4EFE7]">
        <span className="flex h-8 w-8 items-center justify-center rounded-[8px] border border-[#3A342D] bg-[#211E1A] text-[#D88445]">
          <ShieldCheck size={17} />
        </span>
        <span className="text-[14px] font-semibold">Course Admin</span>
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
                "flex h-10 items-center gap-2.5 rounded-[8px] px-3 text-[13px] font-medium transition-colors",
                active
                  ? "border border-[#4C392A] bg-[#2B221B] text-[#F7EEE2]"
                  : "border border-transparent text-[#A9A29A] hover:bg-[#201E1B] hover:text-[#F4EFE7]",
              )}
            >
              <Icon size={16} className={active ? "text-[#D88445]" : "text-[#77716A]"} />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
