import { BookOpen, LayoutDashboard, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface AdminNavigationItem {
  label: string;
  href: string;
  icon: LucideIcon;
}

export const ADMIN_NAVIGATION: AdminNavigationItem[] = [
  { label: "Dashboard", href: "/admin", icon: LayoutDashboard },
  { label: "Users", href: "/admin/users", icon: Users },
  { label: "Courses", href: "/admin/courses", icon: BookOpen },
];

export function adminSectionTitle(pathname: string): string {
  const match = ADMIN_NAVIGATION.find((item) =>
    item.href === "/admin" ? pathname === item.href : pathname.startsWith(item.href),
  );
  return match?.label ?? "Admin";
}
