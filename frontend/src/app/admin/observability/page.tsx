"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, RefreshCw, AlertTriangle } from "lucide-react";
import { getDashboardSummary, getServiceHealth, getQueueStatus, getModelGateComparisons } from "@/lib/api/admin";
import { AdminShell } from "@/components/layout/admin-shell";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { formatPercent, formatDate } from "@/lib/utils";
import { cn } from "@/lib/utils";

// ── Metric tile ───────────────────────────────────────────────────────────────

function MetricTile({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  const valueColor =
    tone === "good"
      ? "text-emerald-700"
      : tone === "warn"
      ? "text-amber-700"
      : tone === "bad"
      ? "text-red-700"
      : "text-slate-900";

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
      <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-1">{label}</p>
      <p className={cn("text-2xl font-bold tabular-nums", valueColor)}>{value}</p>
      {sub && <p className="text-2xs text-slate-400 mt-1 leading-snug">{sub}</p>}
    </div>
  );
}

// ── Service rate bar ─────────────────────────────────────────────────────────

function RateRow({
  label,
  value,
  sub,
}: {
  label: string;
  value: number | null | undefined;
  sub?: string;
}) {
  const pct = value != null ? Math.round(value * 100) : null;
  const color =
    pct == null ? "bg-slate-300" : pct >= 95 ? "bg-emerald-500" : pct >= 80 ? "bg-amber-500" : "bg-red-500";
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-medium text-slate-700">{label}</span>
        <span className="text-xs font-semibold tabular-nums text-slate-800">
          {pct != null ? `${pct}%` : "—"}
        </span>
      </div>
      <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden mb-1">
        <div
          className={cn("h-full rounded-full transition-all", color)}
          style={{ width: `${pct ?? 0}%` }}
        />
      </div>
      {sub && <p className="text-2xs text-slate-400">{sub}</p>}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function ObservabilityPage() {
  const { data: summary, isLoading: sLoading, refetch: refetchS, isFetching } = useQuery({
    queryKey: ["admin-dashboard"],
    queryFn: getDashboardSummary,
    staleTime: 20_000,
    refetchInterval: 30_000,
  });

  const { data: health, isLoading: hLoading, refetch: refetchH } = useQuery({
    queryKey: ["service-health", 24],
    queryFn: () => getServiceHealth(24),
    staleTime: 20_000,
    refetchInterval: 30_000,
  });

  const { data: queue, isLoading: qLoading, refetch: refetchQ } = useQuery({
    queryKey: ["queue-status"],
    queryFn: getQueueStatus,
    staleTime: 10_000,
    refetchInterval: 15_000,
  });

  const { data: shadowData, isLoading: shadowLoading } = useQuery({
    queryKey: ["model-gate-comparisons", { limit: 10 }],
    queryFn: () => getModelGateComparisons({ limit: 10 }),
    staleTime: 30_000,
  });

  function refetchAll() {
    refetchS(); refetchH(); refetchQ();
  }

  const autoAcceptTone =
    summary?.auto_accept_rate == null ? "neutral"
    : summary.auto_accept_rate >= 0.85 ? "good"
    : summary.auto_accept_rate >= 0.65 ? "warn"
    : "bad";

  const agreementTone =
    summary?.structural_agreement_rate == null ? "neutral"
    : summary.structural_agreement_rate >= 0.85 ? "good"
    : summary.structural_agreement_rate >= 0.70 ? "warn"
    : "bad";

  return (
    <AdminShell
      breadcrumbs={[{ label: "Observability" }]}
      headerRight={
        <Button variant="ghost" size="sm" onClick={refetchAll} className="gap-1.5 text-slate-500">
          <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
          <span className="text-xs">Refresh</span>
        </Button>
      }
    >
      <div className="p-6 space-y-6">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Activity className="h-5 w-5 text-slate-500" />
            <h1 className="text-base font-semibold text-slate-900">Observability</h1>
          </div>
          <p className="text-xs text-slate-500">
            Real-time metrics from the database and Redis. All values are computed on-request —
            no caching layer. For Prometheus / Grafana dashboards, see{" "}
            <code className="font-mono text-indigo-600">monitoring/</code>.
          </p>
        </div>

        {/* Top metric tiles */}
        <div>
          <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-2">Pipeline output</p>
          {sLoading ? (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24" />)}
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <MetricTile
                label="Pages / Hour"
                value={summary?.throughput_pages_per_hour != null ? String(Math.round(summary.throughput_pages_per_hour)) : "—"}
                sub="Terminal pages in last 60 min"
              />
              <MetricTile
                label="Auto-Accept Rate"
                value={summary?.auto_accept_rate != null ? formatPercent(summary.auto_accept_rate) : "—"}
                sub="All-time accepted / terminal"
                tone={autoAcceptTone}
              />
              <MetricTile
                label="Structural Agreement"
                value={summary?.structural_agreement_rate != null ? formatPercent(summary.structural_agreement_rate) : "—"}
                sub="IEP1A ↔ IEP1B geometry match (all-time)"
                tone={agreementTone}
              />
              <MetricTile
                label="Human Review Rate"
                value={health?.human_review_throughput_rate != null ? `${health.human_review_throughput_rate.toFixed(1)}/h` : "—"}
                sub={`Corrected pages/hour (last ${health?.window_hours ?? "—"}h)`}
              />
            </div>
          )}
        </div>

        {/* Queue depths */}
        <div>
          <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-2">Queue depths (live)</p>
          {qLoading ? (
            <Skeleton className="h-24" />
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
              <MetricTile
                label="Page Queue"
                value={String(queue?.page_tasks_queued ?? "—")}
                sub="Waiting to be claimed"
              />
              <MetricTile
                label="In-Flight"
                value={String(queue?.page_tasks_processing ?? "—")}
                sub="Claimed by workers"
              />
              <MetricTile
                label="Dead-Letter"
                value={String(queue?.page_tasks_dead_letter ?? "—")}
                sub="Exhausted retries"
                tone={(queue?.page_tasks_dead_letter ?? 0) > 0 ? "bad" : "good"}
              />
              <MetricTile
                label="Shadow Queue"
                value={String(queue?.shadow_tasks_queued ?? "—")}
                sub="Shadow tasks pending"
              />
              <MetricTile
                label="Worker Slots"
                value={
                  queue?.worker_slots_available != null
                    ? `${queue.worker_slots_available}/${queue.worker_slots_max}`
                    : "—"
                }
                sub="Available concurrency"
              />
            </div>
          )}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Per-service success rates */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-slate-800">Per-Service Success Rates</h2>
              <span className="text-2xs text-slate-400">Last {health?.window_hours ?? "—"}h</span>
            </div>
            {hLoading ? (
              <div className="space-y-4">
                {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-10" />)}
              </div>
            ) : (
              <div className="space-y-4">
                <RateRow label="IEP1A + IEP1B (Preprocessing)" value={health?.preprocessing_success_rate} sub="Geometry detection stage" />
                <RateRow label="IEP1D (Rectification)" value={health?.rectification_success_rate} sub="UVDoc rescue stage" />
                <RateRow label="IEP2A + IEP2B (Layout)" value={health?.layout_success_rate} sub="Layout detection stage" />
                <RateRow label="Structural Agreement (window)" value={health?.structural_agreement_rate} sub="IEP1A ↔ IEP1B consensus in window" />
                <RateRow label="Rescue Rate" value={health?.rescue_rate} sub="First-pass failures sent to IEP1D (vs. policy skip)" />
              </div>
            )}
          </div>

          {/* Model gate comparisons */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-slate-800">Recent Model Gate Comparisons</h2>
              <span className="text-2xs text-slate-400">
                Total: {shadowData?.total ?? "—"}
              </span>
            </div>
            <p className="text-2xs text-slate-400 mb-3">
              Offline comparison of shadow vs. production model gate scores. No candidate inference
              ran on live pages. See{" "}
              <a href="/admin/model-lifecycle" className="text-indigo-500 hover:underline">
                Model Lifecycle
              </a>{" "}
              for full history.
            </p>
            {shadowLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10" />)}
              </div>
            ) : !shadowData?.items.length ? (
              <p className="text-xs text-slate-400 italic">No model gate comparisons yet.</p>
            ) : (
              <div className="space-y-2">
                {shadowData.items.map((ev) => (
                  <div
                    key={ev.eval_id}
                    className="flex items-center justify-between rounded-lg border border-slate-100 bg-slate-50 px-3 py-2"
                  >
                    <div>
                      <p className="text-xs font-mono text-slate-600 truncate max-w-[140px]">{ev.job_id}</p>
                      <p className="text-2xs text-slate-400">{formatDate(ev.created_at)}</p>
                    </div>
                    <div className="flex items-center gap-2">
                      {ev.confidence_delta != null && (
                        <span
                          className={cn(
                            "text-xs font-semibold tabular-nums",
                            ev.confidence_delta >= 0 ? "text-emerald-700" : "text-red-700"
                          )}
                        >
                          Δ{ev.confidence_delta >= 0 ? "+" : ""}{ev.confidence_delta.toFixed(3)}
                        </span>
                      )}
                      <Badge
                        variant={
                          ev.status === "completed"
                            ? "success"
                            : ev.status === "failed"
                            ? "danger"
                            : "muted"
                        }
                        className="text-2xs"
                      >
                        {ev.status}
                      </Badge>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Prometheus / Grafana demo services notice */}
        <div className="border border-slate-200 rounded-xl overflow-hidden">
          <div className="flex items-center gap-2 px-4 py-3 bg-slate-50 border-b border-slate-200">
            <AlertTriangle className="h-4 w-4 text-amber-500 shrink-0" />
            <p className="text-xs font-semibold text-slate-700">Prometheus &amp; Grafana — Demo / Batch Observability</p>
          </div>
          <div className="px-4 py-3 space-y-2 text-xs text-slate-600">
            <p>
              Prometheus and Grafana are <strong>standalone demo and batch monitoring services</strong> —
              they are <strong>off by default</strong> (desired count 0) to avoid idle Fargate costs.
              They are not started by the normal deploy or scale-up workflows.
            </p>
            <ul className="list-disc list-inside space-y-1 text-slate-500">
              <li>
                <strong>To start dashboards:</strong> run{" "}
                <code className="font-mono text-indigo-600">observability-up.yml</code> via GitHub Actions
                → Actions → Observability Up.
              </li>
              <li>
                <strong>After demo or testing:</strong> run{" "}
                <code className="font-mono text-indigo-600">observability-down.yml</code> to stop both
                services and avoid idle costs.
              </li>
              <li>
                Dashboards take a <strong>few minutes to populate</strong> after Prometheus starts
                (15-second scrape interval).
              </li>
              <li>
                <strong>Prometheus history is ephemeral</strong> — no EFS or persistent storage is
                attached. History is lost on restart.
              </li>
              <li>
                Grafana is <strong>not always running</strong>. Access is via the task&apos;s private
                IP (see observability-up.yml output for the exact address and port-forward instructions).
              </li>
              <li>
                <strong>This admin dashboard and CloudWatch remain fully available</strong> when
                Grafana is off. The metrics shown above are always served from the DB and Redis.
              </li>
            </ul>
            <p className="text-slate-400">
              Prometheus scrapes the{" "}
              <code className="font-mono">/metrics</code> endpoint on all active services (configured
              via <code className="font-mono">shared/middleware.py</code>). Four pre-built Grafana
              dashboards are provisioned from{" "}
              <code className="font-mono">monitoring/grafana/dashboards/</code>:
              api-service, gate-decisions, model-signals, workers-queue.
            </p>
          </div>
        </div>
      </div>
    </AdminShell>
  );
}
