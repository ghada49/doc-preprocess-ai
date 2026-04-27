"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  LayoutDashboard,
  Briefcase,
  ClipboardList,
  GitBranch,
  FlaskConical,
  RefreshCw,
  Settings,
  Users,
  LogOut,
  Server,
  Activity,
  Rocket,
  TestTube2,
  Layers,
} from "lucide-react";
import { useAuth } from "@/lib/auth/auth-context";
import { Sidebar, type NavItem } from "./sidebar";
import { TopBar } from "./top-bar";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

const adminNavItems: NavItem[] = [
  { label: "Overview", href: "/admin/dashboard", icon: LayoutDashboard },
  { label: "Jobs", href: "/admin/jobs", icon: Briefcase },
  { label: "Correction Queue", href: "/admin/queue", icon: ClipboardList },
  { label: "Lineage / Audit Trail", href: "/admin/lineage", icon: GitBranch },
  { label: "Services", href: "/admin/services", icon: Server },
  { label: "Observability", href: "/admin/observability", icon: Activity },
  { label: "Model Lifecycle", href: "/admin/model-lifecycle", icon: Layers },
  { label: "Model Evaluation", href: "/admin/models", icon: FlaskConical },
  { label: "Retraining", href: "/admin/retraining", icon: RefreshCw },
  { label: "Deployment", href: "/admin/deployment", icon: Rocket },
  { label: "Testing Evidence", href: "/admin/testing", icon: TestTube2 },
  { label: "Policy", href: "/admin/policy", icon: Settings },
  { label: "Users", href: "/admin/users", icon: Users },
];

interface AdminShellProps {
  children: React.ReactNode;
  breadcrumbs?: { label: string; href?: string }[];
  headerRight?: React.ReactNode;
  className?: string;
}

export function AdminShell({
  children,
  breadcrumbs,
  headerRight,
  className,
}: AdminShellProps) {
  const { isAuthenticated, isAdmin, isLoading, logout } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (isLoading) return;
    if (!isAuthenticated) {
      router.replace("/login");
    } else if (!isAdmin) {
      router.replace("/jobs");
    }
  }, [isAuthenticated, isAdmin, isLoading, router]);

  if (isLoading || !isAuthenticated || !isAdmin) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-50">
        <Spinner size="lg" />
      </div>
    );
  }

  return (
    <div className="flex h-screen bg-slate-50 overflow-hidden">
      <Sidebar
        items={adminNavItems}
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
          breadcrumbs={[{ label: "Admin", href: "/admin/dashboard" }, ...(breadcrumbs ?? [])]}
          right={headerRight}
        />
        <main
          className={cn(
            "flex-1 overflow-y-auto",
            className
          )}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
