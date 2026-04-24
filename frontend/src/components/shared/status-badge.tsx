import { cn } from "@/lib/utils";
import { pageStateLabel, pageStateClass, jobStatusLabel, jobStatusClass } from "@/lib/utils";

interface StatusBadgeProps {
  status: string;
  type: "page" | "job";
  className?: string;
}

export function StatusBadge({ status, type, className }: StatusBadgeProps) {
  const label = type === "page" ? pageStateLabel(status) : jobStatusLabel(status);
  const cls = type === "page" ? pageStateClass(status) : jobStatusClass(status);

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium border shadow-sm shadow-slate-200/40",
        cls,
        className
      )}
    >
      <span className="inline-block h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {label}
    </span>
  );
}

// Compact dot-only variant
export function StatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    queued: "bg-slate-400",
    preprocessing: "bg-blue-500",
    rectification: "bg-purple-500",
    ptiff_qa_pending: "bg-indigo-500",
    layout_detection: "bg-cyan-500",
    semantic_norm: "bg-blue-500",
    pending_human_correction: "bg-orange-500",
    accepted: "bg-emerald-500",
    review: "bg-yellow-500",
    failed: "bg-red-500",
    split: "bg-violet-500",
  };
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 rounded-full",
        colors[status] ?? "bg-slate-400"
      )}
    />
  );
}
