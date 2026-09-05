"use client";

import { useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Loader2, LogOut } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/lib/auth/auth-provider";

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

  const handleLogout = async () => {
    await logout();
    router.replace("/login");
  };

  return (
    <header className="border-b border-line bg-white">
      <div className="mx-auto flex h-12 w-full max-w-[1100px] items-center justify-between px-4 sm:px-6">
        <Link href="/" className="text-[13px] font-semibold text-ink">
          AI Course Creator
        </Link>
        <div className="flex min-w-0 items-center gap-3">
          {user ? (
            <span className="hidden max-w-[240px] truncate text-[12px] text-ink-500 sm:block">
              {user.email}
            </span>
          ) : null}
          <Button type="button" variant="ghost" size="sm" onClick={handleLogout}>
            <LogOut size={13} />
            Log out
          </Button>
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

  return (
    <>
      <AuthBar />
      {children}
    </>
  );
}
