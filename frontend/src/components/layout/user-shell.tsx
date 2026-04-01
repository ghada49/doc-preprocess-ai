"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  Briefcase,
  ClipboardList,
  PlusCircle,
  LogOut,
} from "lucide-react";
import { useAuth } from "@/lib/auth/auth-context";
import { Sidebar, type NavItem } from "./sidebar";
import { TopBar } from "./top-bar";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

const userNavItems: NavItem[] = [
  { label: "My Jobs", href: "/jobs", icon: Briefcase },
  { label: "Submit Job", href: "/submit", icon: PlusCircle },
  { label: "Correction Queue", href: "/queue", icon: ClipboardList },
];

interface UserShellProps {
  children: React.ReactNode;
  breadcrumbs?: { label: string; href?: string }[];
  headerRight?: React.ReactNode;
  className?: string;
}

export function UserShell({
  children,
  breadcrumbs,
  headerRight,
  className,
}: UserShellProps) {
  const { isAuthenticated, isLoading, logout } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (isLoading) return;
    if (!isAuthenticated) {
      router.replace("/login");
    }
  }, [isAuthenticated, isLoading, router]);

  if (isLoading || !isAuthenticated) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-50">
        <Spinner size="lg" />
      </div>
    );
  }

  return (
    <div className="flex h-screen bg-slate-50 overflow-hidden">
      <Sidebar
        items={userNavItems}
        footer={
          <button
            onClick={() => { logout(); router.push("/login"); }}
            className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-slate-500 hover:bg-slate-100 hover:text-slate-700 transition-colors"
          >
            <LogOut className="h-4 w-4 shrink-0" />
            <span>Sign out</span>
          </button>
        }
      />

      <div className="flex flex-1 flex-col overflow-hidden">
        <TopBar
          breadcrumbs={breadcrumbs}
          right={headerRight}
        />
        <main className={cn("flex-1 overflow-y-auto", className)}>
          {children}
        </main>
      </div>
    </div>
  );
}
