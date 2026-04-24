"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Cpu,
  Gauge,
  RefreshCw,
  ShieldCheck,
  Zap,
} from "lucide-react";
import { getDashboardSummary, getServiceHealth } from "@/lib/api/admin";
import { AdminShell } from "@/components/layout/admin-shell";
import { KPICard } from "@/components/shared/kpi-card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatPercent, formatScore } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { ServiceHealthRate } from "@/types/api";

// ── Health rate row definitions ────────────────────────────────────────────────

const healthRateMeta: Array<{
  key: ServiceHealthRate["key"];
  label: string;
  kind: "percent" | "rate";
  subtitle?: string;
  informational?: boolean;
}> = [
  {
    key: "preprocessing_success_rate",
    label: "Preprocessing Success",
    kind: "percent",
    subtitle: "IEP1A / IEP1B / IEP1C invocations that completed without error.",
  },
  {
    key: "rectification_success_rate",
    label: "Rectification Success",
    kind: "percent",
    subtitle: "IEP1D rescue invocations that completed without error.",
  },
  {
    key: "rescue_rate",
    label: "Rescue Rate",
    kind: "percent",
    informational: true,
    subtitle:
      "Of pages that failed first-pass quality, how many were sent to IEP1D rescue. " +
      'Drops to 0 % when the rectification policy is set to "Disable and send directly to review".',
  },
  {
    key: "layout_success_rate",
    label: "Layout Success",
    kind: "percent",
    subtitle: "IEP2A / IEP2B layout detection invocations that completed without error.",
  },
  {
    key: "structural_agreement_rate",
    label: "Structural Agreement",
    kind: "percent",
    subtitle: "Pages where IEP1A and IEP1B agreed on page geometry within the window.",
  },
  {
    key: "human_review_throughput_rate",
    label: "Human Review Throughput",
    kind: "rate",
    subtitle: "Human-corrected pages per hour over the selected window.",
  },
];

// ── Page ──────────────────────────────────────────────────────────────────────

export default function AdminDashboardPage() {
  const {
    data: summary,
    isLoading: summaryLoading,
    refetch: refetchSummary,
    isFetching: summaryFetching,
  } = useQuery({
    queryKey: ["admin-dashboard"],
    queryFn: getDashboardSummary,
    staleTime: 20_000,
    refetchInterval: 30_000,
  });

  const {
    data: health,
    isLoading: healthLoading,
    refetch: refetchHealth,
  } = useQuery({
    queryKey: ["service-health"],
    queryFn: getServiceHealth,
    staleTime: 20_000,
    refetchInterval: 30_000,
  });

  const rateRows = healthRateMeta.map((item) => ({
    ...item,
    value: health?.[item.key] ?? null,
  }));

  return (
    <AdminShell
      breadcrumbs={[{ label: "Dashboard" }]}
      headerRight={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            refetchSummary();
            refetchHealth();
          }}
          className="gap-1.5 text-slate-500"
        >
          <RefreshCw
            className={cn("h-3.5 w-3.5", summaryFetching && "animate-spin")}
          />
          <span className="text-xs text-slate-500">Refresh</span>
        </Button>
      }
    >
      <div className="p-6 space-y-6">
        <div>
          <h1 className="text-base font-semibold text-slate-900">
            Operations Dashboard
          </h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Live KPI snapshot — refreshes every 30 seconds.
          </p>
        </div>

        {/* Row 1 — pipeline output metrics */}
        <div>
          <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-2">
            Pipeline output
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <KPICard
              label="Pages / Hour"
              value={
                summary?.throughput_pages_per_hour != null
                  ? Math.round(summary.throughput_pages_per_hour)
                  : null
              }
              icon={Zap}
              iconColor="text-yellow-600"
              loading={summaryLoading}
            />
            <KPICard
              label="Auto-Accept Rate"
              value={
                summary?.auto_accept_rate != null
                  ? formatPercent(summary.auto_accept_rate)
                  : null
              }
              icon={ShieldCheck}
              iconColor="text-emerald-600"
              loading={summaryLoading}
            />
            <KPICard
              label="Structural Agreement"
              value={
                summary?.structural_agreement_rate != null
                  ? formatPercent(summary.structural_agreement_rate)
                  : null
              }
              icon={Gauge}
              iconColor="text-indigo-600"
              loading={summaryLoading}
            />
            <KPICard
              label="Awaiting Review"
              value={summary?.pending_corrections_count ?? null}
              icon={AlertTriangle}
              iconColor="text-orange-500"
              loading={summaryLoading}
              attention={(summary?.pending_corrections_count ?? 0) > 0}
            />
          </div>
        </div>

        {/* Row 2 — system activity */}
        <div>
          <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-2">
            System activity
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
            <KPICard
              label="Active Jobs"
              value={summary?.active_jobs_count ?? null}
              icon={Activity}
              iconColor="text-blue-600"
              loading={summaryLoading}
            />
            <KPICard
              label="Active Workers"
              value={summary?.active_workers_count ?? null}
              icon={Cpu}
              iconColor="text-violet-600"
              loading={summaryLoading}
            />
            <KPICard
              label="Shadow Evaluations"
              value={summary?.shadow_evaluations_count ?? null}
              icon={BarChart3}
              iconColor="text-slate-600"
              loading={summaryLoading}
            />
          </div>
        </div>

        {/* Detail panels */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Service Health Rates */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <div className="flex items-center justify-between mb-1">
              <h2 className="text-sm font-semibold text-slate-800">
                Service Health Rates
              </h2>
              <span className="text-2xs text-slate-400 tabular-nums">
                Last {health?.window_hours ?? "—"}h
              </span>
            </div>
            <p className="text-2xs text-slate-400 mb-4">
              Success rates for each pipeline stage over the rolling window.
            </p>

            {healthLoading ? (
              <div className="space-y-4">
                {Array.from({ length: 6 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 w-full" />
                ))}
              </div>
            ) : (
              <div className="space-y-4">
                {rateRows.map((row) => (
                  <HealthRateRow
                    key={row.key}
                    label={row.label}
                    value={row.value}
                    kind={row.kind}
                    subtitle={row.subtitle}
                    informational={row.informational}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Queue & Policy Pressure */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-slate-800 mb-1">
              Queue &amp; Policy State
            </h2>
            <p className="text-2xs text-slate-400 mb-4">
              Live counts from the processing queue and active policy decisions.
            </p>

            {summaryLoading || healthLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-14 w-full" />
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                <InsightRow
                  label="Awaiting human review"
                  sublabel="Pages blocked on correction before continuing"
                  value={String(summary?.pending_corrections_count ?? 0)}
                  emphasis={
                    (summary?.pending_corrections_count ?? 0) > 0
                      ? "warning"
                      : "neutral"
                  }
                />
                <InsightRow
                  label="Active jobs"
                  sublabel="Jobs currently in the running state"
                  value={String(summary?.active_jobs_count ?? 0)}
                />
                <InsightRow
                  label="Active workers"
                  sublabel="Tasks currently claimed from the processing queue"
                  value={String(summary?.active_workers_count ?? 0)}
                />
                <InsightRow
                  label="Structural agreement"
                  sublabel="IEP1A / IEP1B geometry agreement (all-time)"
                  value={
                    summary?.structural_agreement_rate != null
                      ? formatPercent(summary.structural_agreement_rate)
                      : "—"
                  }
                  emphasis={
                    summary?.structural_agreement_rate != null &&
                    summary.structural_agreement_rate < 0.8
                      ? "warning"
                      : "neutral"
                  }
                />
                <InsightRow
                  label="Skipped to review by policy"
                  sublabel={`Pages sent directly to human review because rectification was disabled — last ${health?.window_hours ?? "—"}h`}
                  value={String(health?.policy_skips_count ?? 0)}
                  emphasis={
                    (health?.policy_skips_count ?? 0) > 0 ? "info" : "neutral"
                  }
                />
              </div>
            )}
          </div>
        </div>
      </div>
    </AdminShell>
  );
}

// ── HealthRateRow ──────────────────────────────────────────────────────────────

function HealthRateRow({
  label,
  value,
  kind,
  subtitle,
  informational = false,
}: {
  label: string;
  value: number | null;
  kind: "percent" | "rate";
  subtitle?: string;
  informational?: boolean;
}) {
  const normalized =
    value == null
      ? null
      : kind === "percent"
      ? Math.max(0, Math.min(1, value))
      : Math.max(0, Math.min(1, value / 100));

  const tone = informational
    ? value == null
      ? "bg-slate-300"
      : value > 0
      ? "bg-indigo-500"
      : "bg-slate-300"
    : value == null
    ? "bg-slate-300"
    : kind === "percent" && value >= 0.95
    ? "bg-emerald-500"
    : kind === "percent" && value >= 0.8
    ? "bg-amber-500"
    : kind === "percent"
    ? "bg-red-500"
    : "bg-indigo-500";

  const displayValue =
    value == null
      ? "—"
      : kind === "percent"
      ? formatPercent(value)
      : formatScore(value, 1);

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-medium text-slate-700">{label}</span>
        <span className="text-xs font-semibold text-slate-800 tabular-nums">
          {displayValue}
        </span>
      </div>
      <div className="h-2 bg-slate-100 rounded-full overflow-hidden mb-1">
        <div
          className={cn("h-full rounded-full transition-all duration-500", tone)}
          style={{ width: `${((normalized ?? 0) * 100).toFixed(1)}%` }}
        />
      </div>
      {subtitle && (
        <p className="text-2xs text-slate-400 leading-relaxed">{subtitle}</p>
      )}
    </div>
  );
}

// ── InsightRow ────────────────────────────────────────────────────────────────

function InsightRow({
  label,
  sublabel,
  value,
  emphasis = "neutral",
}: {
  label: string;
  sublabel?: string;
  value: string;
  emphasis?: "neutral" | "warning" | "info";
}) {
  return (
    <div
      className={cn(
        "flex items-center justify-between rounded-lg border px-3 py-2.5",
        emphasis === "warning"
          ? "border-orange-200 bg-orange-50"
          : emphasis === "info"
          ? "border-indigo-200 bg-indigo-50"
          : "border-slate-200 bg-slate-50"
      )}
    >
      <div className="min-w-0 mr-3">
        <p className={cn(
          "text-xs font-medium",
          emphasis === "warning" ? "text-orange-800"
          : emphasis === "info" ? "text-indigo-800"
          : "text-slate-700"
        )}>
          {label}
        </p>
        {sublabel && (
          <p className="text-2xs text-slate-400 mt-0.5 leading-snug">{sublabel}</p>
        )}
      </div>
      <span className={cn(
        "text-sm font-bold tabular-nums shrink-0",
        emphasis === "warning" ? "text-orange-700"
        : emphasis === "info" ? "text-indigo-700"
        : "text-slate-900"
      )}>
        {value}
      </span>
    </div>
  );
}
