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
import { formatPercent, formatScore, snakeToTitle } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { ServiceHealthRate } from "@/types/api";

const healthRateMeta: Array<{
  key: ServiceHealthRate["key"];
  label: string;
  kind: "percent" | "rate";
}> = [
  {
    key: "preprocessing_success_rate",
    label: "Preprocessing Success",
    kind: "percent",
  },
  {
    key: "rectification_success_rate",
    label: "Rectification Success",
    kind: "percent",
  },
  {
    key: "layout_success_rate",
    label: "Layout Success",
    kind: "percent",
  },
  {
    key: "structural_agreement_rate",
    label: "Structural Agreement",
    kind: "percent",
  },
  {
    key: "human_review_throughput_rate",
    label: "Human Review Throughput",
    kind: "rate",
  },
];

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
    key: item.key,
    label: item.label,
    kind: item.kind,
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
            Live KPI snapshot from the EEP admin summary and service health
            endpoints.
          </p>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <KPICard
            label="Throughput (p/h)"
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
            label="Pending Correction"
            value={summary?.pending_corrections_count ?? null}
            icon={AlertTriangle}
            iconColor="text-orange-500"
            loading={summaryLoading}
            attention={(summary?.pending_corrections_count ?? 0) > 0}
          />
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
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
          <KPICard
            label="Health Window"
            value={health?.window_hours ?? null}
            icon={Gauge}
            iconColor="text-slate-500"
            loading={healthLoading}
            sublabel={health?.window_hours ? "hours" : undefined}
          />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold text-slate-800">
                Service Health Rates
              </h2>
              <span className="text-2xs text-slate-400">
                Rolling window: {health?.window_hours ?? "—"}h
              </span>
            </div>

            {healthLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 5 }).map((_, index) => (
                  <Skeleton key={index} className="h-10 w-full" />
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                {rateRows.map((row) => (
                  <HealthRateRow
                    key={row.key}
                    label={row.label}
                    value={row.value}
                    kind={row.kind}
                  />
                ))}
              </div>
            )}
          </div>

          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-slate-800 mb-4">
              Queue Pressure
            </h2>

            {summaryLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 4 }).map((_, index) => (
                  <Skeleton key={index} className="h-14 w-full" />
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                <InsightRow
                  label="Pending human correction"
                  value={String(summary?.pending_corrections_count ?? 0)}
                  emphasis={
                    (summary?.pending_corrections_count ?? 0) > 0
                      ? "warning"
                      : "neutral"
                  }
                />
                <InsightRow
                  label="Active jobs"
                  value={String(summary?.active_jobs_count ?? 0)}
                />
                <InsightRow
                  label="Active workers"
                  value={String(summary?.active_workers_count ?? 0)}
                />
                <InsightRow
                  label="Structural agreement"
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
              </div>
            )}
          </div>
        </div>
      </div>
    </AdminShell>
  );
}

function HealthRateRow({
  label,
  value,
  kind,
}: {
  label: string;
  value: number | null;
  kind: "percent" | "rate";
}) {
  const normalized =
    value == null
      ? null
      : kind === "percent"
      ? Math.max(0, Math.min(1, value))
      : Math.max(0, Math.min(1, value / 100));

  const tone =
    value == null
      ? "bg-slate-300"
      : kind === "percent" && value >= 0.95
      ? "bg-emerald-500"
      : kind === "percent" && value >= 0.8
      ? "bg-amber-500"
      : kind === "percent"
      ? "bg-red-500"
      : "bg-indigo-500";

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs text-slate-600">{label}</span>
        <span className="text-xs font-medium text-slate-700 tabular-nums">
          {value == null
            ? "—"
            : kind === "percent"
            ? formatPercent(value)
            : formatScore(value, 1)}
        </span>
      </div>
      <div className="h-2 bg-slate-200 rounded-full overflow-hidden">
        <div
          className={cn("h-full rounded-full transition-all", tone)}
          style={{ width: `${((normalized ?? 0) * 100).toFixed(1)}%` }}
        />
      </div>
    </div>
  );
}

function InsightRow({
  label,
  value,
  emphasis = "neutral",
}: {
  label: string;
  value: string;
  emphasis?: "neutral" | "warning";
}) {
  return (
    <div
      className={cn(
        "flex items-center justify-between rounded-lg border px-3 py-3",
        emphasis === "warning"
          ? "border-orange-200 bg-orange-50"
          : "border-slate-200 bg-slate-50"
      )}
    >
      <span className="text-xs text-slate-600">{label}</span>
      <span className="text-sm font-semibold text-slate-900 tabular-nums">
        {value}
      </span>
    </div>
  );
}
