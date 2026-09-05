"use client";

import { useEffect, useState } from "react";
import { AlertCircle, BookOpen, CheckCircle2, Loader2, Users } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { getAdminDashboard, type AdminDashboardResponse } from "@/lib/api/admin";

const METRIC_COPY = {
  total_users: {
    label: "Total users",
    note: "Registered accounts in PostgreSQL.",
    icon: Users,
  },
  total_courses: {
    label: "Total courses",
    note: "Courses currently stored in PostgreSQL.",
    icon: BookOpen,
  },
  system_status: {
    label: "System status",
    note: "Admin API and authorization are responding.",
    icon: CheckCircle2,
  },
};

export default function AdminDashboardPage() {
  const [dashboard, setDashboard] = useState<AdminDashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setIsLoading(true);
    getAdminDashboard()
      .then((data) => {
        if (!active) return;
        setDashboard(data);
        setError(null);
      })
      .catch((caught) => {
        if (!active) return;
        setError(caught instanceof ApiError ? caught.message : "Dashboard could not be loaded.");
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const metrics = dashboard
    ? [
        { ...METRIC_COPY.total_users, value: dashboard.total_users.toLocaleString() },
        { ...METRIC_COPY.total_courses, value: dashboard.total_courses.toLocaleString() },
        { ...METRIC_COPY.system_status, value: dashboard.system_status.toUpperCase() },
      ]
    : [];

  return (
    <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-6">
      <section className="rounded-[8px] border border-[#302D29] bg-[#201E1B] p-5 sm:p-6">
        <p className="text-[12px] font-semibold uppercase tracking-[0.14em] text-[#D88445]">
          Overview
        </p>
        <h2 className="mt-2 text-[24px] font-semibold text-[#F7EEE2] sm:text-[30px]">
          Admin Dashboard
        </h2>
        <p className="mt-2 max-w-2xl text-[14px] leading-6 text-[#BEB6AD]">
          Live platform totals from the database-backed admin API.
        </p>
      </section>

      {isLoading ? (
        <section className="flex min-h-[160px] items-center justify-center rounded-[8px] border border-[#302D29] bg-[#201E1B] text-sm text-[#BEB6AD]">
          <Loader2 size={18} className="mr-2 animate-spin text-[#D88445]" />
          Loading dashboard
        </section>
      ) : null}

      {error ? (
        <section className="flex items-center gap-2 rounded-[8px] border border-[#5A3529] bg-[#2A1D19] p-4 text-sm text-[#F0C4A6]">
          <AlertCircle size={17} className="shrink-0" />
          {error}
        </section>
      ) : null}

      <section className="grid gap-4 md:grid-cols-3">
        {metrics.map((item) => {
          const Icon = item.icon;
          return (
          <article
            key={item.label}
            className="rounded-[8px] border border-[#302D29] bg-[#201E1B] p-5"
          >
            <div className="flex items-center justify-between gap-3">
              <p className="text-[13px] font-medium text-[#BEB6AD]">{item.label}</p>
              <Icon size={18} className="text-[#D88445]" />
            </div>
            <p className="mt-3 text-[26px] font-semibold text-[#F7EEE2]">{item.value}</p>
            <p className="mt-2 text-[12px] leading-5 text-[#8E8780]">{item.note}</p>
          </article>
          );
        })}
      </section>
    </div>
  );
}
