import { type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface PageHeaderProps {
  title: string;
  description?: string;
  icon?: LucideIcon;
  iconColor?: string;
  actions?: React.ReactNode;
  className?: string;
  badge?: React.ReactNode;
}

export function PageHeader({
  title,
  description,
  icon: Icon,
  iconColor = "text-indigo-600",
  actions,
  className,
  badge,
}: PageHeaderProps) {
  return (
    <div className={cn("flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between", className)}>
      <div className="flex items-start gap-3">
        {Icon && (
          <div className="mt-0.5 flex h-10 w-10 items-center justify-center rounded-xl border border-white bg-white shadow-sm shadow-slate-200/80 ring-1 ring-slate-200/70">
            <Icon className={cn("h-4.5 w-4.5", iconColor)} />
          </div>
        )}
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold tracking-tight text-slate-950">{title}</h1>
            {badge}
          </div>
          {description && (
            <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-500">
              {description}
            </p>
          )}
        </div>
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  );
}
