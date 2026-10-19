import { BarChart3, BookOpen, LayoutDashboard, Layers3, Library, Settings, Users } from "lucide-react";
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
  { label: "Templates", href: "/admin/templates", icon: Layers3 },
  { label: "Knowledge library", href: "/admin/knowledge", icon: Library },
];

export const ADMIN_SECONDARY_NAVIGATION: AdminNavigationItem[] = [
  { label: "Analytics", href: "#", icon: BarChart3 },
  { label: "Settings", href: "#", icon: Settings },
];

export function adminSectionTitle(pathname: string): string {
  const match = ADMIN_NAVIGATION.find((item) =>
    item.href === "/admin" ? pathname === item.href : pathname.startsWith(item.href),
  );
  return match?.label ?? "Admin";
}
