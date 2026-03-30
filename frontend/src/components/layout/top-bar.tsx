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
        "h-14 flex items-center justify-between px-5 border-b border-slate-200 bg-white/90 backdrop-blur-sm sticky top-0 z-20",
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
                className="text-slate-400 hover:text-slate-700 transition-colors"
              >
                {crumb.label}
              </button>
            ) : (
              <span className="text-slate-700 font-medium">{crumb.label}</span>
            )}
          </span>
        ))}
      </nav>

      {/* Right side */}
      <div className="flex items-center gap-3">
        {right}

        {/* User menu */}
        <DropdownMenu>
          <DropdownMenuTrigger className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs text-slate-500 hover:bg-slate-100 hover:text-slate-700 transition-colors outline-none">
            <div className="flex h-6 w-6 items-center justify-center rounded-full bg-indigo-100 border border-indigo-200">
              <User className="h-3 w-3 text-indigo-600" />
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
