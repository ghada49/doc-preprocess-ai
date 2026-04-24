"use client";

import { useRouter } from "next/navigation";
import { LogOut, User, ChevronDown } from "lucide-react";
import { useAuth } from "@/lib/auth/auth-context";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface TopBarProps {
  breadcrumbs?: { label: string; href?: string }[];
  right?: React.ReactNode;
  className?: string;
}

export function TopBar({ breadcrumbs, right, className }: TopBarProps) {
  const { user, username, isAdmin, logout } = useAuth();
  const router = useRouter();

  const handleLogout = () => {
    logout();
    router.push("/login");
  };

  return (
    <header
      className={cn(
        "sticky top-0 z-20 flex h-16 items-center justify-between border-b border-slate-200/80 bg-white/80 px-6 backdrop-blur-xl",
        className
      )}
    >
      {/* Breadcrumbs */}
      <nav className="flex items-center gap-1.5 text-sm">
        {breadcrumbs?.map((crumb, i) => (
          <span key={i} className="flex items-center gap-1.5">
            {i > 0 && <span className="text-slate-300">/</span>}
            {crumb.href ? (
              <button
                onClick={() => crumb.href && router.push(crumb.href)}
                className="text-slate-400 transition-colors hover:text-slate-700"
              >
                {crumb.label}
              </button>
            ) : (
              <span className="font-semibold text-slate-800">{crumb.label}</span>
            )}
          </span>
        ))}
      </nav>

      {/* Right side */}
      <div className="flex items-center gap-3">
        {right}

        {/* User menu */}
        <DropdownMenu>
          <DropdownMenuTrigger className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-500 shadow-sm shadow-slate-200/70 outline-none transition-colors hover:bg-slate-50 hover:text-slate-700">
            <div className="flex h-6 w-6 items-center justify-center rounded-full border border-slate-200 bg-slate-950">
              <User className="h-3 w-3 text-white" />
            </div>
            <span className="text-slate-700 font-medium">
              {username ?? user?.sub ?? "User"}
            </span>
            {isAdmin && (
              <Badge variant="info" className="text-2xs px-1.5 py-0">
                admin
              </Badge>
            )}
            <ChevronDown className="h-3 w-3 text-slate-400" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuLabel>
              <p className="text-xs text-slate-800 font-medium">{user?.sub}</p>
              {username && username !== user?.sub && (
                <p className="text-2xs text-slate-500 mt-0.5">@{username}</p>
              )}
              <p className="text-2xs text-slate-500 mt-0.5 capitalize">{user?.role} account</p>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={handleLogout}
              destructive
              className="gap-2"
            >
              <LogOut className="h-3.5 w-3.5" />
              Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
