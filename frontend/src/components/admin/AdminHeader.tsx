"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Bell, ChevronDown, LogOut, Search, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/lib/auth/auth-provider";
import { cn } from "@/lib/utils/cn";
import { ADMIN_NAVIGATION, adminSectionTitle } from "./navigation";

function isActiveRoute(pathname: string, href: string): boolean {
  return href === "/admin" ? pathname === href : pathname.startsWith(href);
}

export function AdminHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, logout } = useAuth();
  const title = adminSectionTitle(pathname);

  const handleLogout = async () => {
    await logout();
    router.replace("/login");
  };

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-white/95 backdrop-blur">
      <div className="flex min-h-16 items-center justify-between gap-3 px-4 sm:px-6">
        <div className="hidden min-w-0 flex-1 md:block">
          <div className="relative max-w-[390px]">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400" />
            <input
              aria-label="Global search"
              className="h-9 w-full rounded-[8px] border border-line bg-canvas pl-9 pr-3 text-[12px] text-ink outline-none placeholder:text-ink-400 focus:border-brand-400"
              placeholder="Search courses, users, or documents..."
            />
          </div>
        </div>
        <div className="min-w-0 md:hidden">
          <p className="text-2xs font-semibold uppercase tracking-[0.12em] text-brand-500">
            Admin
          </p>
          <h1 className="truncate text-[18px] font-semibold text-ink">{title}</h1>
        </div>

        <div className="flex min-w-0 items-center gap-3">
          <button type="button" className="relative hidden h-9 w-9 items-center justify-center rounded-full text-ink-500 transition-colors hover:bg-brand-50 hover:text-brand-500 sm:flex" aria-label="Notifications">
            <Bell size={17} />
            <span className="absolute right-[8px] top-[7px] h-1.5 w-1.5 rounded-full bg-brand-500" />
          </button>
          {user ? (
            <div className="hidden min-w-0 items-center gap-2 sm:flex">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500 text-[13px] font-semibold text-white">
                {user.email.charAt(0).toUpperCase()}
              </span>
              <div className="min-w-0">
                <p className="truncate text-[12px] font-medium text-ink">{user.email}</p>
                <p className="text-2xs uppercase tracking-[0.12em] text-ink-400">
                  Admin
                </p>
              </div>
              <ChevronDown size={14} className="ml-2 text-ink-400" />
            </div>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={handleLogout}
            className="border-line text-ink-500 hover:bg-brand-50 hover:text-ink"
          >
            <LogOut size={14} />
            Log out
          </Button>
        </div>
      </div>

      <nav className="flex gap-2 overflow-x-auto border-t border-line px-4 py-2 sm:px-6 md:hidden">
        {ADMIN_NAVIGATION.map((item) => {
          const Icon = item.icon;
          const active = isActiveRoute(pathname, item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "inline-flex h-9 shrink-0 items-center gap-2 rounded-[8px] border px-3 text-[12px] font-medium",
                active ? "border-brand-200 bg-brand-50 text-ink" : "border-line text-ink-500",
              )}
            >
              <Icon size={14} className={active ? "text-brand-500" : "text-ink-400"} />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}
