"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Eye,
  Loader2,
  RefreshCcw,
  Search,
  Users as UsersIcon,
  UserCheck,
  UserX,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import {
  getAdminUser,
  listAdminUsers,
  updateAdminUserRole,
  updateAdminUserStatus,
  type AdminUser,
  type AdminUserListOptions,
  type AdminUserListResponse,
} from "@/lib/api/admin";
import { useAuth } from "@/lib/auth/auth-provider";
import type { Role } from "@/lib/types/auth";
import {
  AdminAlert,
  AdminEmpty,
  AdminLoading,
  AdminPageHeader,
  AdminPanel,
  AdminStatusBadge,
} from "@/components/admin/AdminUI";

const PAGE_SIZE = 25;
const ROLE_LABELS: Record<Role, string> = {
  author: "Author",
  editor_reviewer: "Editor/Reviewer",
  manager: "Manager",
  admin: "Admin",
};
const ROLE_OPTIONS: Role[] = ["author", "editor_reviewer", "manager", "admin"];
const selectClass =
  "h-10 rounded-[8px] border border-line bg-white px-3 text-[13px] text-ink outline-none transition-colors focus:border-brand-500";
const inputClass =
  "h-10 w-full rounded-[8px] border border-line bg-white px-3 pl-9 text-[13px] text-ink outline-none placeholder:text-ink-400 transition-colors focus:border-brand-500";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function RoleBadge({ role }: { role: Role }) {
  return <AdminStatusBadge tone={role === "admin" ? "warning" : "neutral"}>{ROLE_LABELS[role] ?? role}</AdminStatusBadge>;
}

function StateBadge({ active, activeLabel, inactiveLabel }: {
  active: boolean;
  activeLabel: string;
  inactiveLabel: string;
}) {
  return <AdminStatusBadge tone={active ? "success" : "danger"}>{active ? activeLabel : inactiveLabel}</AdminStatusBadge>;
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid gap-1 border-b border-line py-3 last:border-0 sm:grid-cols-[150px_1fr] sm:gap-4">
      <dt className="text-[12px] uppercase tracking-[0.08em] text-ink-400">{label}</dt>
      <dd className="break-words text-[13px] text-ink">{value}</dd>
    </div>
  );
}

export default function AdminUsersPage() {
  const { user: currentUser } = useAuth();
  const [page, setPage] = useState(0);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [role, setRole] = useState<"all" | Role>("all");
  const [status, setStatus] = useState<"all" | "active" | "inactive">("all");
  const [verification, setVerification] = useState<"all" | "verified" | "unverified">("all");
  const [data, setData] = useState<AdminUserListResponse | null>(null);
  const [selectedUser, setSelectedUser] = useState<AdminUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detailsError, setDetailsError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadingDetailsId, setLoadingDetailsId] = useState<string | null>(null);
  const [updatingUserId, setUpdatingUserId] = useState<string | null>(null);

  const queryOptions = useCallback((): AdminUserListOptions => {
    const trimmedSearch = search.trim();
    return {
      search: trimmedSearch || undefined,
      role: role === "all" ? undefined : role,
      is_active: status === "all" ? undefined : status === "active",
      is_verified: verification === "all" ? undefined : verification === "verified",
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    };
  }, [page, role, search, status, verification]);

  const loadUsers = useCallback(() => {
    const controller = new AbortController();
    setIsLoading(true);
    listAdminUsers(queryOptions())
      .then((response) => {
        setData(response);
        setError(null);
      })
      .catch((caught) => {
        if (caught instanceof DOMException && caught.name === "AbortError") return;
        setError(caught instanceof ApiError ? caught.message : "Users could not be loaded.");
      })
      .finally(() => setIsLoading(false));
    return () => controller.abort();
  }, [queryOptions]);

  useEffect(() => loadUsers(), [loadUsers]);

  const refreshSelectedUser = async (userId: string) => {
    const detail = await getAdminUser(userId);
    setSelectedUser(detail);
  };

  const viewDetails = async (target: AdminUser) => {
    setSelectedUser(target);
    setDetailsError(null);
    setLoadingDetailsId(target.id);
    try {
      await refreshSelectedUser(target.id);
    } catch (caught) {
      setDetailsError(caught instanceof ApiError ? caught.message : "User details could not be loaded.");
    } finally {
      setLoadingDetailsId(null);
    }
  };

  const updateRow = (updated: AdminUser) => {
    setData((current) =>
      current
        ? {
            ...current,
            users: current.users.map((item) => (item.id === updated.id ? updated : item)),
          }
        : current,
    );
    setSelectedUser((current) => (current?.id === updated.id ? updated : current));
  };

  const changeRole = async (target: AdminUser, roleValue: Role) => {
    if (target.role === roleValue) return;
    setUpdatingUserId(target.id);
    try {
      const updated = await updateAdminUserRole(target.id, roleValue);
      updateRow(updated);
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Role could not be updated.");
    } finally {
      setUpdatingUserId(null);
    }
  };

  const changeStatus = async (target: AdminUser) => {
    const nextActive = !target.is_active;
    if (!nextActive && !window.confirm(`Deactivate ${target.email}?`)) return;
    setUpdatingUserId(target.id);
    try {
      const updated = await updateAdminUserStatus(target.id, nextActive);
      updateRow(updated);
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Status could not be updated.");
    } finally {
      setUpdatingUserId(null);
    }
  };

  const applySearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPage(0);
    setSearch(searchInput);
  };

  const clearFilters = () => {
    setSearchInput("");
    setSearch("");
    setRole("all");
    setStatus("all");
    setVerification("all");
    setPage(0);
  };

  const users = data?.users ?? [];
  const total = data?.total ?? 0;
  const canGoBack = page > 0;
  const canGoForward = (page + 1) * PAGE_SIZE < total;

  return (
    <div className="admin-page mx-auto flex w-full max-w-[1240px] flex-col gap-5">
      <AdminPageHeader eyebrow="User management" title="Users" description="Inspect registered accounts, filter database records, and manage access controls." />

      {error ? <AdminAlert>{error}</AdminAlert> : null}

      <AdminPanel className="p-4">
        <div className="grid gap-3 lg:grid-cols-[minmax(240px,1fr)_160px_160px_180px_auto]">
          <form onSubmit={applySearch} className="relative flex gap-2">
            <Search
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#8E8780]"
            />
            <input
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              className={inputClass}
              placeholder="Search email or user ID"
            />
            <Button
              type="submit"
              variant="ghost"
              size="sm"
              className="h-9 border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
              title="Search"
            >
              <Search size={14} />
            </Button>
          </form>
          <select
            value={role}
            onChange={(event) => {
              setRole(event.target.value as "all" | Role);
              setPage(0);
            }}
            className={selectClass}
            aria-label="Role filter"
          >
            <option value="all">All roles</option>
            {ROLE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {ROLE_LABELS[option]}
              </option>
            ))}
          </select>
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as "all" | "active" | "inactive");
              setPage(0);
            }}
            className={selectClass}
            aria-label="Status filter"
          >
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
          </select>
          <select
            value={verification}
            onChange={(event) => {
              setVerification(event.target.value as "all" | "verified" | "unverified");
              setPage(0);
            }}
            className={selectClass}
            aria-label="Verification filter"
          >
            <option value="all">All verification</option>
            <option value="verified">Verified</option>
            <option value="unverified">Unverified</option>
          </select>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={clearFilters}
            className="h-9 border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
            title="Reset filters"
          >
            <RefreshCcw size={14} />
            Reset
          </Button>
        </div>
      </AdminPanel>

      <AdminPanel>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#302D29] px-4 py-3">
          <div className="text-[13px] text-[#BEB6AD]">
            {total.toLocaleString()} user{total === 1 ? "" : "s"}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canGoBack || isLoading}
              onClick={() => setPage((value) => Math.max(0, value - 1))}
              className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
              title="Previous page"
            >
              <ChevronLeft size={14} />
            </Button>
            <span className="text-[12px] text-[#8E8780]">Page {page + 1}</span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!canGoForward || isLoading}
              onClick={() => setPage((value) => value + 1)}
              className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
              title="Next page"
            >
              <ChevronRight size={14} />
            </Button>
          </div>
        </div>

        {isLoading ? (
          <AdminLoading label="Loading users" />
        ) : users.length === 0 ? (
          <AdminEmpty><UsersIcon /><span>No users match the current filters.</span></AdminEmpty>
        ) : (
          <div className="overflow-x-auto">
            <table className="admin-table w-full min-w-[1120px] text-left text-[13px]">
              <thead className="bg-[#181614] text-[12px] uppercase tracking-[0.08em] text-[#8E8780]">
                <tr>
                  <th className="px-4 py-3 font-medium">User ID</th>
                  <th className="px-4 py-3 font-medium">Email</th>
                  <th className="px-4 py-3 font-medium">Role</th>
                  <th className="px-4 py-3 font-medium">Active</th>
                  <th className="px-4 py-3 font-medium">Verified</th>
                  <th className="px-4 py-3 font-medium">Created</th>
                  <th className="px-4 py-3 font-medium">Updated</th>
                  <th className="px-4 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#302D29]">
                {users.map((item) => {
                  const isSelf = item.id === currentUser?.id;
                  const busy = updatingUserId === item.id;
                  return (
                    <tr key={item.id} className="text-[#D8D0C7]">
                      <td className="max-w-[230px] truncate px-4 py-3 font-mono text-[12px] text-[#A9A29A]">
                        {item.id}
                      </td>
                      <td className="px-4 py-3 font-medium text-[#F4EFE7]">{item.email}</td>
                      <td className="px-4 py-3">
                        <RoleBadge role={item.role} />
                      </td>
                      <td className="px-4 py-3">
                        <StateBadge active={item.is_active} activeLabel="Active" inactiveLabel="Inactive" />
                      </td>
                      <td className="px-4 py-3">
                        <StateBadge
                          active={item.is_verified}
                          activeLabel="Verified"
                          inactiveLabel="Unverified"
                        />
                      </td>
                      <td className="px-4 py-3 text-[#A9A29A]">{formatDate(item.created_at)}</td>
                      <td className="px-4 py-3 text-[#A9A29A]">{formatDate(item.updated_at)}</td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => viewDetails(item)}
                            className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                            title="View details"
                          >
                            {loadingDetailsId === item.id ? (
                              <Loader2 size={14} className="animate-spin" />
                            ) : (
                              <Eye size={14} />
                            )}
                          </Button>
                          <select
                            value={item.role}
                            disabled={isSelf || busy}
                            onChange={(event) => changeRole(item, event.target.value as Role)}
                            className="h-8 rounded-[8px] border border-[#34312D] bg-[#181614] px-2 text-[12px] text-[#E8DED3] outline-none disabled:cursor-not-allowed disabled:opacity-50"
                            aria-label={`Role for ${item.email}`}
                            title={isSelf ? "Your own role cannot be changed here" : "Change role"}
                          >
                            {ROLE_OPTIONS.map((option) => (
                              <option key={option} value={option}>
                                {ROLE_LABELS[option]}
                              </option>
                            ))}
                          </select>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            disabled={isSelf || busy}
                            onClick={() => changeStatus(item)}
                            className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
                            title={isSelf ? "Current admin cannot be deactivated here" : "Change status"}
                          >
                            {busy ? (
                              <Loader2 size={14} className="animate-spin" />
                            ) : item.is_active ? (
                              <UserX size={14} />
                            ) : (
                              <UserCheck size={14} />
                            )}
                            {item.is_active ? "Deactivate" : "Activate"}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </AdminPanel>

      {selectedUser ? (
        <AdminPanel className="p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-[12px] font-semibold uppercase tracking-[0.14em] text-[#D88445]">
                User Details
              </p>
              <h3 className="mt-2 text-[18px] font-semibold text-[#F7EEE2]">{selectedUser.email}</h3>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setSelectedUser(null)}
              className="border-[#34312D] text-[#D8D0C7] hover:bg-[#2A2520] hover:text-[#F4EFE7]"
            >
              Close
            </Button>
          </div>
          {detailsError ? (
            <div className="mt-4 flex items-center gap-2 rounded-[8px] border border-[#5A3529] bg-[#2A1D19] p-3 text-sm text-[#F0C4A6]">
              <AlertCircle size={16} />
              {detailsError}
            </div>
          ) : null}
          <dl className="mt-4">
            <DetailRow label="User ID" value={<span className="font-mono text-[12px]">{selectedUser.id}</span>} />
            <DetailRow label="Email" value={selectedUser.email} />
            <DetailRow label="Role" value={<RoleBadge role={selectedUser.role} />} />
            <DetailRow
              label="Active status"
              value={
                <StateBadge
                  active={selectedUser.is_active}
                  activeLabel="Active"
                  inactiveLabel="Inactive"
                />
              }
            />
            <DetailRow
              label="Verification"
              value={
                <StateBadge
                  active={selectedUser.is_verified}
                  activeLabel="Verified"
                  inactiveLabel="Unverified"
                />
              }
            />
            <DetailRow label="Created date" value={formatDate(selectedUser.created_at)} />
            <DetailRow label="Updated date" value={formatDate(selectedUser.updated_at)} />
          </dl>
        </AdminPanel>
      ) : null}
    </div>
  );
}
