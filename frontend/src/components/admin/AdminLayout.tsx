"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, ShieldAlert } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { getAdminHealth } from "@/lib/api/admin";
import { useAuth } from "@/lib/auth/auth-provider";
import { AdminHeader } from "./AdminHeader";
import { AdminSidebar } from "./AdminSidebar";

type AdminAccessState = "checking" | "allowed" | "denied";

function AdminAccessMessage({ denied }: { denied?: boolean }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[#1B1A18] px-4 text-[#F4EFE7]">
      <div className="flex w-full max-w-[420px] items-center gap-3 rounded-[8px] border border-[#302D29] bg-[#201E1B] p-4 text-sm text-[#BEB6AD]">
        {denied ? (
          <ShieldAlert size={18} className="shrink-0 text-[#D88445]" />
        ) : (
          <Loader2 size={18} className="shrink-0 animate-spin text-[#D88445]" />
        )}
        {denied ? "Admin access is not available for this account." : "Checking admin access"}
      </div>
    </main>
  );
}

export function AdminLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { user, isLoading } = useAuth();
  const [access, setAccess] = useState<AdminAccessState>("checking");

  useEffect(() => {
    if (isLoading) return;
    if (!user) {
      router.replace("/login");
      return;
    }

    let active = true;
    setAccess("checking");

    getAdminHealth()
      .then(() => {
        if (active) setAccess("allowed");
      })
      .catch((error) => {
        if (!active) return;
        setAccess("denied");
        if (error instanceof ApiError && error.status === 401) {
          router.replace("/login");
          return;
        }
        router.replace("/");
      });

    return () => {
      active = false;
    };
  }, [isLoading, router, user]);

  if (isLoading || access === "checking") return <AdminAccessMessage />;
  if (access === "denied") return <AdminAccessMessage denied />;

  return (
    <div className="min-h-screen bg-[#1B1A18] text-[#F4EFE7]">
      <div className="flex min-h-screen">
        <AdminSidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <AdminHeader />
          <main className="flex-1 px-4 py-6 sm:px-6 lg:px-8">{children}</main>
        </div>
      </div>
    </div>
  );
}
