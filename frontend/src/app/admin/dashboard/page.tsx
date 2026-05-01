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
  Inbox,
  MailX,
  AlertCircle,
  CheckCircle2,
  CircleDashed,
} from "lucide-react";
import {
  getDashboardSummary,
  getDeploymentStatus,
  getQueueStatus,
  getServiceHealth,
} from "@/lib/api/admin";
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
    subtitle: "IEP1A / IEP1B invocations that completed without error.",
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
      'Fraction of first-pass failures sent to IEP1D rescue. Drops to 0% when policy is "Disable and send directly to review".',
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
    subtitle: "Pages where IEP1A and IEP1B agreed on page geometry.",
  },
  {
    key: "human_review_throughput_rate",
    label: "Human Review Throughput",
    kind: "rate",
    subtitle: "Human-corrected pages per active hour.",
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

  const { data: health, isLoading: healthLoading, refetch: refetchHealth } = useQuery({
    queryKey: ["service-health"],
    queryFn: () => getServiceHealth(24),
    staleTime: 20_000,
    refetchInterval: 30_000,
  });

  const { data: queue, isLoading: queueLoading, refetch: refetchQueue } = useQuery({
    queryKey: ["queue-status"],
    queryFn: getQueueStatus,
    staleTime: 10_000,
    refetchInterval: 15_000,
  });

  const { data: deployment, refetch: refetchDeployment } = useQuery({
    queryKey: ["deployment-status"],
    queryFn: getDeploymentStatus,
    staleTime: 60_000,
  });

  const rateRows = healthRateMeta.map((item) => ({
    ...item,
    value: health?.[item.key] ?? null,
  }));

  function refetchAll() {
    refetchSummary();
    refetchHealth();
    refetchQueue();
    refetchDeployment();
  }

  return (
    <AdminShell
      breadcrumbs={[{ label: "Overview" }]}
      headerRight={
        <Button
          variant="ghost"
          size="sm"
          onClick={refetchAll}
          className="gap-1.5 text-slate-500"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", summaryFetching && "animate-spin")} />
          <span className="text-xs text-slate-500">Refresh</span>
        </Button>
      }
    >
      <div className="p-6 space-y-6">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-base font-semibold text-slate-900">
              Operations Overview
            </h1>
            <p className="text-xs text-slate-500 mt-0.5">
              Live KPI snapshot — refreshes every 30 s. Use the sidebar for deep-dive pages.
            </p>
          </div>
          {deployment && (
            <div className="flex items-center gap-2 text-xs text-slate-500 bg-slate-100 px-3 py-1.5 rounded-lg border border-slate-200">
              {deployment.image_tag ? (
                <span className="font-mono">{deployment.image_tag}</span>
              ) : deployment.git_sha ? (
                <span className="font-mono">{deployment.git_sha.slice(0, 8)}</span>
              ) : (
                <span className="italic">commit unknown</span>
              )}
              {deployment.alembic_version && (
                <span className="text-slate-400">· db {deployment.alembic_version}</span>
              )}
            </div>
          )}
        </div>

        {/* Feature flag strip */}
        {deployment && (
          <FeatureFlagStrip flags={deployment.feature_flags} />
        )}

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
              sublabel="Active-hour average"
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
              label="Processing Queue"
              value={queue?.page_tasks_queued ?? null}
              icon={Inbox}
              iconColor="text-teal-600"
              loading={queueLoading}
            />
            <KPICard
              label="Dead-Letter Queue"
              value={queue?.page_tasks_dead_letter ?? null}
              icon={MailX}
              iconColor="text-red-600"
              loading={queueLoading}
              attention={(queue?.page_tasks_dead_letter ?? 0) > 0}
            />
          </div>
        </div>

        {/* Row 3 — shadow + worker slots */}
        <div>
          <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-2">
            Evaluation & capacity
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <KPICard
              label="Shadow-Mode Jobs"
              value={summary?.shadow_evaluations_count ?? null}
              icon={BarChart3}
              iconColor="text-slate-600"
              loading={summaryLoading}
            />
            <KPICard
              label="Shadow Queue Depth"
              value={queue?.shadow_tasks_queued ?? null}
              icon={BarChart3}
              iconColor="text-indigo-400"
              loading={queueLoading}
            />
            <KPICard
              label="Worker Slots Free"
              value={
                queue?.worker_slots_available != null
                  ? `${queue.worker_slots_available} / ${queue.worker_slots_max}`
                  : null
              }
              icon={Cpu}
              iconColor="text-green-600"
              loading={queueLoading}
            />
            <KPICard
              label="Policy Skips (24h)"
              value={health?.policy_skips_count ?? null}
              icon={AlertCircle}
              iconColor="text-amber-600"
              loading={healthLoading}
            />
          </div>
        </div>

        {/* Detail panels */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Service Health Rates */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <div className="flex items-center justify-between mb-1">
              <h2 className="text-sm font-semibold text-slate-800">Service Health Rates</h2>
              <span className="text-2xs text-slate-400 tabular-nums">
                Last {health?.window_hours ?? "—"}h
              </span>
            </div>
            <p className="text-2xs text-slate-400 mb-4">
              Success rates per pipeline stage. See{" "}
              <a href="/admin/observability" className="text-indigo-500 hover:underline">
                Observability
              </a>{" "}
              for full breakdown.
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

          {/* Queue & system state */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-slate-800 mb-1">
              Queue &amp; System State
            </h2>
            <p className="text-2xs text-slate-400 mb-4">
              Live counts from Redis and the active policy. Refreshes every 15 s.
            </p>

            {summaryLoading || healthLoading || queueLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 7 }).map((_, i) => (
                  <Skeleton key={i} className="h-14 w-full" />
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                <InsightRow
                  label="Awaiting human review"
                  sublabel="Pages blocked on correction before continuing"
                  value={String(summary?.pending_corrections_count ?? 0)}
                  emphasis={(summary?.pending_corrections_count ?? 0) > 0 ? "warning" : "neutral"}
                />
                <InsightRow
                  label="Processing queue depth"
                  sublabel="Tasks waiting to be claimed by a worker"
                  value={String(queue?.page_tasks_queued ?? "—")}
                />
                <InsightRow
                  label="In-flight (processing list)"
                  sublabel="Tasks currently claimed by worker processes"
                  value={String(queue?.page_tasks_processing ?? "—")}
                />
                <InsightRow
                  label="Dead-letter queue"
                  sublabel="Exhausted tasks (retry_count ≥ 3) — require manual inspection"
                  value={String(queue?.page_tasks_dead_letter ?? 0)}
                  emphasis={(queue?.page_tasks_dead_letter ?? 0) > 0 ? "warning" : "neutral"}
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
                  sublabel={`Pages sent directly to human review — last ${health?.window_hours ?? "—"}h`}
                  value={String(health?.policy_skips_count ?? 0)}
                  emphasis={(health?.policy_skips_count ?? 0) > 0 ? "info" : "neutral"}
                />
              </div>
            )}
          </div>
        </div>
      </div>
    </AdminShell>
  );
}

// ── Feature Flag Strip ────────────────────────────────────────────────────────

function FeatureFlagStrip({
  flags,
}: {
  flags: { retraining_mode: string; golden_eval_mode: string };
}) {
  return (
    <div className="flex flex-wrap gap-2">
      <FlagChip
        label="Retraining"
        value={flags.retraining_mode}
        liveColor="text-emerald-700 bg-emerald-50 border-emerald-200"
        stubColor="text-amber-700 bg-amber-50 border-amber-200"
      />
      <FlagChip
        label="Golden Eval"
        value={flags.golden_eval_mode}
        liveColor="text-emerald-700 bg-emerald-50 border-emerald-200"
        stubColor="text-amber-700 bg-amber-50 border-amber-200"
      />
    </div>
  );
}

function FlagChip({
  label,
  value,
  liveColor,
  stubColor,
}: {
  label: string;
  value: string;
  liveColor: string;
  stubColor: string;
}) {
  const isLive = value === "live";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium",
        isLive ? liveColor : stubColor
      )}
    >
      {isLive ? (
        <CheckCircle2 className="h-3 w-3" />
      ) : (
        <CircleDashed className="h-3 w-3" />
      )}
      {label}:{" "}
      <span className="font-semibold uppercase tracking-wide">{value}</span>
    </span>
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
        <span className="text-xs font-semibold text-slate-800 tabular-nums">{displayValue}</span>
      </div>
      <div className="h-2 bg-slate-100 rounded-full overflow-hidden mb-1">
        <div
          className={cn("h-full rounded-full transition-all duration-500", tone)}
          style={{ width: `${((normalized ?? 0) * 100).toFixed(1)}%` }}
        />
      </div>
      {subtitle && <p className="text-2xs text-slate-400 leading-relaxed">{subtitle}</p>}
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
        <p
          className={cn(
            "text-xs font-medium",
            emphasis === "warning"
              ? "text-orange-800"
              : emphasis === "info"
              ? "text-indigo-800"
              : "text-slate-700"
          )}
        >
          {label}
        </p>
        {sublabel && <p className="text-2xs text-slate-400 mt-0.5 leading-snug">{sublabel}</p>}
      </div>
      <span
        className={cn(
          "text-sm font-bold tabular-nums shrink-0",
          emphasis === "warning"
            ? "text-orange-700"
            : emphasis === "info"
            ? "text-indigo-700"
            : "text-slate-900"
        )}
      >
        {value}
      </span>
    </div>
  );
}
