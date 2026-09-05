"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { LogOut, ShieldCheck } from "lucide-react";
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
    <header className="sticky top-0 z-20 border-b border-[#2A2825] bg-[#1B1A18]/95 backdrop-blur">
      <div className="flex min-h-16 items-center justify-between gap-3 px-4 sm:px-6">
        <div className="min-w-0">
          <p className="text-2xs font-semibold uppercase tracking-[0.12em] text-[#D88445]">
            Admin
          </p>
          <h1 className="truncate text-[18px] font-semibold text-[#F4EFE7]">{title}</h1>
        </div>

        <div className="flex min-w-0 items-center gap-3">
          {user ? (
            <div className="hidden min-w-0 items-center gap-2 rounded-[8px] border border-[#302D29] bg-[#201E1B] px-3 py-2 sm:flex">
              <ShieldCheck size={15} className="shrink-0 text-[#D88445]" />
              <div className="min-w-0">
                <p className="truncate text-[12px] font-medium text-[#F4EFE7]">{user.email}</p>
                <p className="text-2xs uppercase tracking-[0.12em] text-[#8E8780]">
                  {user.role}
                </p>
              </div>
            </div>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={handleLogout}
            className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
          >
            <LogOut size={14} />
            Log out
          </Button>
        </div>
      </div>

      <nav className="flex gap-2 overflow-x-auto border-t border-[#26231F] px-4 py-2 sm:px-6 md:hidden">
        {ADMIN_NAVIGATION.map((item) => {
          const Icon = item.icon;
          const active = isActiveRoute(pathname, item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "inline-flex h-9 shrink-0 items-center gap-2 rounded-[8px] border px-3 text-[12px] font-medium",
                active
                  ? "border-[#4C392A] bg-[#2B221B] text-[#F7EEE2]"
                  : "border-[#302D29] text-[#A9A29A]",
              )}
            >
              <Icon size={14} className={active ? "text-[#D88445]" : "text-[#77716A]"} />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}
