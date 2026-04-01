"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
  badge?: number;
  attention?: boolean;
}

interface SidebarProps {
  items: NavItem[];
  groups?: { label: string; items: NavItem[] }[];
  logo?: React.ReactNode;
  footer?: React.ReactNode;
  collapsed?: boolean;
}

function NavLink({
  item,
  active,
  collapsed,
}: {
  item: NavItem;
  active: boolean;
  collapsed: boolean;
}) {
  const Icon = item.icon;

  const inner = (
    <Link
      href={item.href}
      className={cn(
        "group relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-all duration-100",
        active
          ? "bg-indigo-50 text-indigo-700 font-medium"
          : "text-slate-600 hover:bg-slate-100 hover:text-slate-900",
        collapsed && "justify-center px-2"
      )}
    >
      <span className="relative">
        <Icon
          className={cn(
            "h-4 w-4 shrink-0 transition-colors",
            active ? "text-indigo-600" : "text-slate-400 group-hover:text-slate-600"
          )}
        />
        {item.attention && (
          <span className="absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-orange-400" />
        )}
      </span>

      {!collapsed && (
        <>
          <span className="flex-1 truncate">{item.label}</span>
          {item.badge != null && item.badge > 0 && (
            <span className="flex h-5 min-w-[20px] items-center justify-center rounded-full bg-orange-100 px-1.5 text-2xs font-semibold text-orange-600">
              {item.badge > 99 ? "99+" : item.badge}
            </span>
          )}
        </>
      )}

      {active && (
        <span className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 bg-indigo-500 rounded-r-full" />
      )}
    </Link>
  );

  if (collapsed) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>{inner}</TooltipTrigger>
        <TooltipContent side="right">
          {item.label}
          {item.badge != null && item.badge > 0 && ` (${item.badge})`}
        </TooltipContent>
      </Tooltip>
    );
  }

  return inner;
}

export function Sidebar({ items, groups, logo, footer, collapsed = false }: SidebarProps) {
  const pathname = usePathname();

  const isActive = (href: string) => {
    if (href === "/admin/dashboard" || href === "/jobs") {
      return pathname === href;
    }
    return pathname.startsWith(href);
  };

  return (
    <TooltipProvider delayDuration={0}>
      <aside
        className={cn(
          "flex flex-col bg-white border-r border-slate-200",
          "transition-all duration-200",
          collapsed ? "w-14" : "w-56"
        )}
      >
        {/* Logo */}
        <div
          className={cn(
            "flex items-center h-14 border-b border-slate-200 shrink-0",
            collapsed ? "justify-center px-3" : "px-4 gap-3"
          )}
        >
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 shrink-0">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              className="h-4 w-4 text-white"
              xmlns="http://www.w3.org/2000/svg"
            >
              <path
                d="M4 4h6v6H4V4zm10 0h6v6h-6V4zM4 14h6v6H4v-6zm10 3a3 3 0 100-6 3 3 0 000 6z"
                fill="currentColor"
              />
            </svg>
          </div>
          {!collapsed && (
            <span className="text-sm font-semibold text-slate-900 tracking-tight">
              LibraryAI
            </span>
          )}
        </div>

        {/* Nav items */}
        <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
          {logo}

          {groups
            ? groups.map((group) => (
                <div key={group.label} className="mb-4">
                  {!collapsed && (
                    <p className="px-3 mb-1.5 text-2xs font-semibold uppercase tracking-wider text-slate-400">
                      {group.label}
                    </p>
                  )}
                  {group.items.map((item) => (
                    <NavLink
                      key={item.href}
                      item={item}
                      active={isActive(item.href)}
                      collapsed={collapsed}
                    />
                  ))}
                </div>
              ))
            : items.map((item) => (
                <NavLink
                  key={item.href}
                  item={item}
                  active={isActive(item.href)}
                  collapsed={collapsed}
                />
              ))}
        </nav>

        {/* Footer */}
        {footer && (
          <div className="border-t border-slate-200 p-2">{footer}</div>
        )}
      </aside>
    </TooltipProvider>
  );
}
