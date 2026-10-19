"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Bell, CheckCheck } from "lucide-react";
import {
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  type AppNotification,
} from "@/lib/api/notifications";
import { useAuth } from "@/lib/auth/auth-provider";
import { cn } from "@/lib/utils/cn";

const POLL_MS = 30_000;

function ago(value: string): string {
  const seconds = Math.max(1, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

/** The inbox: generation finished, review requested, approved, changes requested. */
export function NotificationBell() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await listNotifications();
      setItems(response.notifications);
      setUnread(response.unread);
    } catch {
      // A missed poll is harmless; the next one tries again.
    }
  }, []);

  useEffect(() => {
    if (!isAuthenticated) return;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [isAuthenticated, refresh]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const openItem = async (item: AppNotification) => {
    setOpen(false);
    if (!item.read) {
      setItems((current) => current.map((n) => (n.notification_id === item.notification_id ? { ...n, read: true } : n)));
      setUnread((count) => Math.max(0, count - 1));
      void markNotificationRead(item.notification_id).catch(() => undefined);
    }
    if (item.link) router.push(item.link);
  };

  const readAll = async () => {
    setItems((current) => current.map((n) => ({ ...n, read: true })));
    setUnread(0);
    await markAllNotificationsRead().catch(() => undefined);
  };

  if (!isAuthenticated) return null;

  return (
    <div ref={box} className="relative">
      <button
        type="button"
        onClick={() => {
          setOpen(!open);
          if (!open) void refresh();
        }}
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
        aria-expanded={open}
        className="creator-icon-button relative"
      >
        <Bell size={17} />
        {unread > 0 ? (
          <span className="absolute -right-1 -top-1 flex h-[16px] min-w-[16px] items-center justify-center rounded-full bg-brand-600 px-1 text-[10px] font-semibold text-white">
            {unread > 9 ? "9+" : unread}
          </span>
        ) : null}
      </button>

      {open ? (
        <div className="fixed left-3 top-16 z-50 w-[330px] overflow-hidden rounded-panel border border-line bg-white shadow-pop">
          <div className="flex items-center justify-between border-b border-line px-3 py-2.5">
            <p className="text-[13px] font-semibold text-ink">Notifications</p>
            {unread > 0 ? (
              <button type="button" onClick={() => void readAll()} className="flex items-center gap-1 text-[11.5px] text-brand-700">
                <CheckCheck size={13} /> Mark all read
              </button>
            ) : null}
          </div>
          {items.length === 0 ? (
            <p className="px-3 py-6 text-center text-[12.5px] text-ink-400">Nothing yet.</p>
          ) : (
            <ul className="max-h-[360px] divide-y divide-line overflow-y-auto">
              {items.map((item) => (
                <li key={item.notification_id}>
                  <button
                    type="button"
                    onClick={() => void openItem(item)}
                    className={cn("block w-full px-3 py-2.5 text-left hover:bg-canvas", !item.read && "bg-brand-50/50")}
                  >
                    <span className="flex items-start gap-2">
                      {!item.read ? <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-brand-600" aria-hidden /> : <span className="w-2 shrink-0" />}
                      <span className="min-w-0">
                        <span className="block text-[12.5px] font-medium text-ink">{item.title}</span>
                        {item.message ? <span className="mt-0.5 line-clamp-2 block text-[12px] text-ink-500">{item.message}</span> : null}
                        <span className="mt-0.5 block text-[11px] text-ink-400">{ago(item.created_at)}</span>
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
