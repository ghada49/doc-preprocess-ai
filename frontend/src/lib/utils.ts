import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import { formatDistanceToNow, format, parseISO } from "date-fns";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return format(parseISO(iso), "MMM d, yyyy HH:mm");
  } catch {
    return iso;
  }
}

export function formatDateShort(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return format(parseISO(iso), "MMM d, HH:mm");
  } catch {
    return iso;
  }
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return formatDistanceToNow(parseISO(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

export function formatPercent(value: number | null | undefined, decimals = 1): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(decimals)}%`;
}

export function formatScore(value: number | null | undefined, decimals = 3): string {
  if (value == null) return "—";
  return value.toFixed(decimals);
}

export function truncateId(id: string, chars = 8): string {
  return id.length > chars ? `${id.substring(0, chars)}…` : id;
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

/** Convert page state enum to display label */
export function pageStateLabel(state: string): string {
  const labels: Record<string, string> = {
    queued: "Queued",
    preprocessing: "Preprocessing",
    rectification: "Rectification",
    ptiff_qa_pending: "PTIFF QA",
    layout_detection: "Layout",
    semantic_norm: "Orientation",
    pending_human_correction: "Needs Review",
    accepted: "Accepted",
    review: "Review",
    failed: "Failed",
    split: "Split",
  };
  return labels[state] ?? snakeToTitle(state);
}

/** Convert job status to display label */
export function jobStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "Queued",
    running: "Running",
    done: "Done",
    failed: "Failed",
  };
  return labels[status] ?? capitalize(status);
}

/** Get CSS class for page state badge */
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

/** Get CSS class for job status badge */
export function jobStatusClass(status: string): string {
  const classes: Record<string, string> = {
    queued: "job-queued",
    running: "job-running",
    done: "job-done",
    failed: "job-failed",
  };
  return classes[status] ?? "job-queued";
}

/** Is a job "active" (should poll)? */
export function isJobActive(status: string): boolean {
  return status === "queued" || status === "running";
}

/** Are there active (non-terminal) pages? */
export function hasActivePages(pages: { status: string }[]): boolean {
  const terminalStates = new Set(["accepted", "review", "failed"]);
  return pages.some((p) => !terminalStates.has(p.status));
}

/** Review reason display label */
export function reviewReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    low_confidence: "Low Confidence",
    geometry_mismatch: "Geometry Mismatch",
    split_detected: "Split Detected",
    skew_detected: "Skew Detected",
    crop_failed: "Crop Failed",
    quality_gate_failed: "Quality Gate Failed",
    manual_flag: "Manual Flag",
  };
  return labels[reason] ?? snakeToTitle(reason);
}

/** Model stage badge color */
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

/** Service health badge color */
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
