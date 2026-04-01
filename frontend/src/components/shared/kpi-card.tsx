import { type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";

interface KPICardProps {
  label: string;
  value: string | number | null;
  icon: LucideIcon;
  iconColor?: string;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
  sublabel?: string;
  loading?: boolean;
  attention?: boolean;
  className?: string;
}

export function KPICard({
  label,
  value,
  icon: Icon,
  iconColor = "text-indigo-600",
  trend,
  trendValue,
  sublabel,
  loading = false,
  attention = false,
  className,
}: KPICardProps) {
  return (
    <div
      className={cn(
        "relative bg-white border rounded-xl p-5 transition-all duration-200 hover:border-slate-300 group shadow-sm",
        attention
          ? "border-orange-200 bg-orange-50/40"
          : "border-slate-200",
        className
      )}
    >
      {attention && (
        <div className="absolute top-3 right-3 h-2 w-2 rounded-full bg-orange-400 animate-pulse-slow" />
      )}

      <div className="flex items-start justify-between mb-4">
        <p className="text-xs font-medium text-slate-500 uppercase tracking-wider">
          {label}
        </p>
        <div
          className={cn(
            "flex h-8 w-8 items-center justify-center rounded-lg bg-slate-100 transition-colors group-hover:bg-slate-200",
            attention && "bg-orange-100"
          )}
        >
          <Icon className={cn("h-4 w-4", iconColor)} />
        </div>
      </div>

      {loading ? (
        <div className="space-y-2">
          <Skeleton className="h-8 w-24" />
          <Skeleton className="h-3 w-16" />
        </div>
      ) : (
        <>
          <p className="text-2xl font-semibold text-slate-900 tabular-nums">
            {value ?? "—"}
          </p>
          <div className="mt-1.5 flex items-center gap-2">
            {sublabel && (
              <span className="text-xs text-slate-500">{sublabel}</span>
            )}
            {trend && trendValue && (
              <span
                className={cn(
                  "text-xs font-medium",
                  trend === "up" && "text-emerald-600",
                  trend === "down" && "text-red-500",
                  trend === "neutral" && "text-slate-500"
                )}
              >
                {trend === "up" ? "↑" : trend === "down" ? "↓" : "→"} {trendValue}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
