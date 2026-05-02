import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import { formatDistanceToNow, format, parseISO } from "date-fns";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  try {
    return format(parseISO(iso), "MMM d, yyyy HH:mm");
  } catch {
    return iso;
  }
}

export function formatDateShort(iso: string | null | undefined): string {
  if (!iso) return "-";
  try {
    return format(parseISO(iso), "MMM d, HH:mm");
  } catch {
    return iso;
  }
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "-";
  try {
    return formatDistanceToNow(parseISO(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "-";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

export function formatPercent(value: number | null | undefined, decimals = 1): string {
  if (value == null) return "-";
  return `${(value * 100).toFixed(decimals)}%`;
}

export function formatScore(value: number | null | undefined, decimals = 3): string {
  if (value == null) return "-";
  return value.toFixed(decimals);
}

export function truncateId(id: string, chars = 8): string {
  return id.length > chars ? `${id.substring(0, chars)}...` : id;
}

export function capitalize(str: string): string {
  return str.charAt(0).toUpperCase() + str.slice(1);
}

export function snakeToTitle(str: string): string {
  return str
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export function pageStateLabel(state: string): string {
  const labels: Record<string, string> = {
    queued: "Waiting",
    preprocessing: "Processing",
    rectification: "Processing",
    ptiff_qa_pending: "Needs review",
    layout_detection: "Processing",
    semantic_norm: "Processing",
    pending_human_correction: "Needs review",
    accepted: "Ready",
    review: "Reviewed",
    failed: "Issue found",
    split: "Split pages",
  };
  return labels[state] ?? "Processing";
}

export function jobStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "Waiting",
    running: "Processing",
    waiting_review: "Waiting review",
    done: "Ready",
    failed: "Issue found",
  };
  return labels[status] ?? capitalize(status);
}

export function pageStateClass(state: string): string {
  const classes: Record<string, string> = {
    queued: "state-queued",
    preprocessing: "state-preprocessing",
    rectification: "state-rectification",
    ptiff_qa_pending: "state-ptiff_qa_pending",
    layout_detection: "state-layout_detection",
    semantic_norm: "state-preprocessing",
    pending_human_correction: "state-pending_human_correction",
    accepted: "state-accepted",
    review: "state-review",
    failed: "state-failed",
    split: "state-split",
  };
  return classes[state] ?? "state-queued";
}

export function jobStatusClass(status: string): string {
  const classes: Record<string, string> = {
    queued: "job-queued",
    running: "job-running",
    waiting_review: "border border-amber-200 bg-amber-50 text-amber-800",
    done: "job-done",
    failed: "job-failed",
  };
  return classes[status] ?? "job-queued";
}

export function isJobActive(status: string): boolean {
  return status === "queued" || status === "running";
}

export function hasActivePages(pages: { status: string }[]): boolean {
  const terminalStates = new Set(["accepted", "review", "failed"]);
  return pages.some((p) => !terminalStates.has(p.status));
}

export function isAutomatedPageState(status: string): boolean {
  return [
    "queued",
    "preprocessing",
    "rectification",
    "layout_detection",
    "semantic_norm",
  ].includes(status);
}

export function isHumanWaitPageState(status: string): boolean {
  return status === "pending_human_correction" || status === "ptiff_qa_pending";
}

export function jobDisplayStatusFromPages(
  jobStatus: string,
  pages: { status: string }[]
): string {
  if (jobStatus !== "running") return jobStatus;
  if (pages.some((page) => isAutomatedPageState(page.status))) return "running";
  if (pages.some((page) => isHumanWaitPageState(page.status))) return "waiting_review";
  return jobStatus;
}

export function reviewReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    low_confidence: "Please review this page",
    geometry_mismatch: "Page edges need review",
    split_detected: "Possible two-page scan",
    skew_detected: "Page angle needs review",
    crop_failed: "Could not find the page edges",
    quality_gate_failed: "Needs a quick review",
    manual_flag: "Marked for review",
  };
  return labels[reason] ?? "Please review this page";
}

export function modelStageClass(stage: string): string {
  const classes: Record<string, string> = {
    experimental: "bg-slate-100 text-slate-600 border-slate-200",
    staging: "bg-blue-50 text-blue-700 border-blue-200",
    shadow: "bg-purple-50 text-purple-700 border-purple-200",
    production: "bg-emerald-50 text-emerald-700 border-emerald-200",
    archived: "bg-slate-50 text-slate-500 border-slate-200",
  };
  return classes[stage] ?? "bg-slate-100 text-slate-600 border-slate-200";
}

export function serviceHealthClass(status: string): string {
  const classes: Record<string, string> = {
    healthy: "bg-emerald-50 text-emerald-700 border-emerald-200",
    degraded: "bg-amber-50 text-amber-700 border-amber-200",
    down: "bg-red-50 text-red-700 border-red-200",
    unknown: "bg-slate-100 text-slate-500 border-slate-200",
  };
  return classes[status] ?? "bg-slate-100 text-slate-500 border-slate-200";
}

export function clampPercent(value: number): number {
  return Math.min(100, Math.max(0, value));
}
