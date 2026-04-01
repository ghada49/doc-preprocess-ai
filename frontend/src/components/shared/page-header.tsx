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
    <div className={cn("flex items-start justify-between gap-4", className)}>
      <div className="flex items-start gap-3">
        {Icon && (
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-100 border border-slate-200 mt-0.5">
            <Icon className={cn("h-4.5 w-4.5", iconColor)} />
          </div>
        )}
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-base font-semibold text-slate-900">{title}</h1>
            {badge}
          </div>
          {description && (
            <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">
              {description}
            </p>
          )}
        </div>
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  );
}
