"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Loader2, LogOut, Sparkles, ChevronDown } from "lucide-react";
import { useAuth } from "@/lib/auth/auth-provider";
import { CreatorShell } from "@/components/course/CreatorShell";

const PUBLIC_ROUTES = new Set(["/login", "/register"]);
const APP_ENTRY_ROUTE = "/";

function isAdminRoute(pathname: string): boolean {
  return pathname === "/admin" || pathname.startsWith("/admin/");
}

function AuthLoading() {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-[480px] items-center justify-center px-4">
      <div className="panel flex w-full items-center justify-center gap-2 p-5 text-sm text-ink-500">
        <Loader2 size={16} className="animate-spin text-brand-600" />
        Checking session
      </div>
    </main>
  );
}

function AuthBar() {
  const router = useRouter();
  const { user, logout } = useAuth();
  const [profileOpen, setProfileOpen] = useState(false);

  const handleLogout = async () => {
    await logout();
    router.replace("/login");
  };

  return (
    <header className="border-b border-line bg-white">
      <div className="flex h-12 w-full items-center justify-between border-b border-line px-4 sm:px-7">
        <Link href="/" className="flex items-center gap-2 text-[13px] font-semibold text-ink">
          <span className="creator-brand-mark h-7 w-7 rounded-[8px]"><Sparkles size={14} /></span>
          AI Course Creator
        </Link>
        <div className="flex min-w-0 items-center gap-3">
          {user ? (
            <div className="relative hidden sm:block">
              <button
                type="button"
                aria-expanded={profileOpen}
                aria-haspopup="menu"
                onClick={() => setProfileOpen((open) => !open)}
                className="flex items-center gap-2 text-[12px] text-ink-500"
              >
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-brand-200 text-[11px] font-semibold text-brand-700">
                  {user.email.charAt(0).toUpperCase()}
                </span>
                <span className="max-w-[240px] truncate">{user.email}</span>
                <ChevronDown size={13} className="text-ink-400" />
              </button>
              {profileOpen ? (
                <div className="absolute right-0 top-10 z-30 min-w-[180px] rounded-[9px] border border-line bg-white p-1.5 shadow-pop" role="menu">
                  {user.role === "admin" ? (
                    <Link
                      href="/admin"
                      role="menuitem"
                      onClick={() => setProfileOpen(false)}
                      className="block rounded-[7px] px-3 py-2 text-[12px] font-medium text-ink transition-colors hover:bg-brand-50 hover:text-brand-700"
                    >
                      Admin Panel
                    </Link>
                  ) : null}
                  <button
                    type="button"
                    role="menuitem"
                    onClick={handleLogout}
                    className="block w-full rounded-[7px] px-3 py-2 text-left text-[12px] font-medium text-ink transition-colors hover:bg-brand-50 hover:text-brand-700"
                  >
                    <span className="inline-flex items-center gap-2"><LogOut size={13} /> Log out</span>
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { isAuthenticated, isLoading } = useAuth();
  const isPublicRoute = PUBLIC_ROUTES.has(pathname);

  useEffect(() => {
    if (isLoading) return;
    if (!isAuthenticated && !isPublicRoute) router.replace("/login");
    if (isAuthenticated && isPublicRoute) router.replace(APP_ENTRY_ROUTE);
  }, [isAuthenticated, isLoading, isPublicRoute, router]);

  if (isLoading) return <AuthLoading />;
  if (!isAuthenticated && !isPublicRoute) return <AuthLoading />;
  if (isAuthenticated && isPublicRoute) return <AuthLoading />;

  if (isPublicRoute) return <>{children}</>;
  if (isAdminRoute(pathname)) return <>{children}</>;

  return <><AuthBar /><CreatorShell>{children}</CreatorShell></>;
}
