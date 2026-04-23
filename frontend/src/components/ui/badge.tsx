import * as React from "react";
import { cn } from "@/lib/utils";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: "default" | "success" | "warning" | "danger" | "info" | "purple" | "muted";
  dot?: boolean;
}

const variantClasses: Record<NonNullable<BadgeProps["variant"]>, string> = {
  default: "bg-slate-100 text-slate-600 border border-slate-200",
  success: "bg-emerald-50 text-emerald-700 border border-emerald-200",
  warning: "bg-amber-50 text-amber-700 border border-amber-200",
  danger: "bg-red-50 text-red-700 border border-red-200",
  info: "bg-blue-50 text-blue-700 border border-blue-200",
  purple: "bg-violet-50 text-violet-700 border border-violet-200",
  muted: "bg-slate-50 text-slate-500 border border-slate-200",
};

const dotClasses: Record<NonNullable<BadgeProps["variant"]>, string> = {
  default: "bg-slate-400",
  success: "bg-emerald-500",
  warning: "bg-amber-500",
  danger: "bg-red-500",
  info: "bg-blue-500",
  purple: "bg-violet-500",
  muted: "bg-slate-400",
};

export const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, variant = "default", dot = false, children, ...props }, ref) => {
    return (
      <span
        ref={ref}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold shadow-sm shadow-slate-200/40",
          variantClasses[variant],
          className
        )}
        {...props}
      >
        {dot && (
          <span
            className={cn("inline-block h-1.5 w-1.5 rounded-full", dotClasses[variant])}
          />
        )}
        {children}
      </span>
    );
  }
);
Badge.displayName = "Badge";
