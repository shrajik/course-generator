import { request } from "./client";

export type NotificationKind =
  | "generation_done"
  | "review_requested"
  | "course_approved"
  | "changes_requested";

/** Mirrors backend app/services/notification_service.py. */
export interface AppNotification {
  notification_id: string;
  kind: NotificationKind;
  title: string;
  message: string;
  course_id: string;
  link: string;
  created_at: string;
  read: boolean;
}

export interface NotificationList {
  notifications: AppNotification[];
  unread: number;
}

export function listNotifications(limit = 30): Promise<NotificationList> {
  return request<NotificationList>("/api/notifications", { query: { limit } });
}

export function markNotificationRead(id: string): Promise<void> {
  return request<void>(`/api/notifications/${id}/read`, { method: "POST" });
}

export function markAllNotificationsRead(): Promise<{ marked: number }> {
  return request<{ marked: number }>("/api/notifications/read-all", { method: "POST" });
}
