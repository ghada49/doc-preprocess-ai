"use client";

import { useQuery } from "@tanstack/react-query";
import { Server, RefreshCw, AlertTriangle, CheckCircle2, Clock } from "lucide-react";
import { getServiceInventory } from "@/lib/api/admin";
import { AdminShell } from "@/components/layout/admin-shell";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { cn, formatDate } from "@/lib/utils";
import type { ServiceInventoryItem } from "@/types/api";

// EEP is the orchestrator — separate from IEP inference services
const EEP_SERVICES = ["eep", "eep_worker", "eep_recovery", "shadow_worker", "retraining_worker", "dataset_builder"];

function deploymentBadgeVariant(type: string): "danger" | "warning" | "muted" {
  const normalizedType = type.toLowerCase();
  if (normalizedType.includes("disabled") || normalizedType.includes("not implemented")) return "danger";
  if (normalizedType.includes("runpod") || normalizedType.includes("gpu")) return "warning";
  return "muted";
}

function HealthBar({ rate }: { rate: number | null }) {
  if (rate === null) return <span className="text-xs text-slate-400 italic">no data</span>;
  const pct = Math.round(rate * 100);
  const color =
    pct >= 95 ? "bg-emerald-500" : pct >= 80 ? "bg-amber-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 bg-slate-100 rounded-full overflow-hidden">
        <div className={cn("h-full rounded-full", color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums font-semibold text-slate-700">{pct}%</span>
    </div>
  );
}

function ServiceRow({ item }: { item: ServiceInventoryItem }) {
  const isDisabled =
    item.deployment_type.toLowerCase().includes("disabled") ||
    item.role.toLowerCase().includes("not implemented");

  return (
    <tr className={cn("border-b border-slate-100 last:border-0", isDisabled && "opacity-60")}>
      <td className="py-3 pr-4">
        <p className="text-xs font-semibold font-mono text-slate-800">{item.service_name}</p>
        {item.port && (
          <p className="text-2xs text-slate-400 mt-0.5">:{item.port}</p>
        )}
      </td>
      <td className="py-3 pr-4 max-w-xs">
        <p className="text-xs text-slate-600 leading-snug">{item.role}</p>
      </td>
      <td className="py-3 pr-4">
        <Badge
          variant={deploymentBadgeVariant(item.deployment_type)}
          className="text-2xs whitespace-nowrap"
        >
          {item.deployment_type}
        </Badge>
      </td>
      <td className="py-3 pr-4">
        {item.health_signal ? (
          <HealthBar rate={item.health_signal.success_rate} />
        ) : (
          <span className="text-2xs text-slate-400 italic">
            {isDisabled ? "disabled" : "no invocations"}
          </span>
        )}
      </td>
      <td className="py-3 pr-4 tabular-nums">
        {item.health_signal ? (
          <span className="text-xs text-slate-600">
            {item.health_signal.total_invocations.toLocaleString()}
          </span>
        ) : (
          <span className="text-xs text-slate-300">—</span>
        )}
      </td>
      <td className="py-3 pr-4 tabular-nums">
        {item.health_signal?.p95_latency_ms != null ? (
          <span className="text-xs text-slate-600">
            {Math.round(item.health_signal.p95_latency_ms)} ms
          </span>
        ) : (
          <span className="text-xs text-slate-300">—</span>
        )}
      </td>
      <td className="py-3">
        {item.health_signal?.last_invoked_at ? (
          <span className="text-xs text-slate-500">
            {formatDate(item.health_signal.last_invoked_at)}
          </span>
        ) : (
          <span className="text-xs text-slate-300">—</span>
        )}
      </td>
    </tr>
  );
}

function ServiceTable({
  title,
  description,
  items,
}: {
  title: string;
  description: string;
  items: ServiceInventoryItem[];
}) {
  return (
    <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-100">
        <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
        <p className="text-2xs text-slate-400 mt-0.5">{description}</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="border-b border-slate-100 bg-slate-50">
              <th className="py-2.5 px-4 text-2xs font-medium text-slate-400 uppercase tracking-wide">Service</th>
              <th className="py-2.5 px-4 text-2xs font-medium text-slate-400 uppercase tracking-wide">Role</th>
              <th className="py-2.5 px-4 text-2xs font-medium text-slate-400 uppercase tracking-wide">Deployment</th>
              <th className="py-2.5 px-4 text-2xs font-medium text-slate-400 uppercase tracking-wide">Success (24h)</th>
              <th className="py-2.5 px-4 text-2xs font-medium text-slate-400 uppercase tracking-wide">Invocations</th>
              <th className="py-2.5 px-4 text-2xs font-medium text-slate-400 uppercase tracking-wide">P95 Latency</th>
              <th className="py-2.5 px-4 text-2xs font-medium text-slate-400 uppercase tracking-wide">Last Invoked</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50 px-4">
            {items.map((item) => (
              <ServiceRow key={item.service_name} item={item} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function ServicesPage() {
  const {
    data,
    isLoading,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ["service-inventory"],
    queryFn: getServiceInventory,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const eepServices = data?.items.filter((i) => EEP_SERVICES.includes(i.service_name)) ?? [];
  const iepServices = data?.items.filter((i) => !EEP_SERVICES.includes(i.service_name)) ?? [];

  return (
    <AdminShell
      breadcrumbs={[{ label: "Services" }]}
      headerRight={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => refetch()}
          className="gap-1.5 text-slate-500"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
          <span className="text-xs">Refresh</span>
        </Button>
      }
    >
      <div className="p-6 space-y-6">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Server className="h-5 w-5 text-slate-500" />
            <h1 className="text-base font-semibold text-slate-900">Service Architecture</h1>
          </div>
          <p className="text-xs text-slate-500">
            Live-data health signals from the <code className="font-mono">service_invocations</code> table
            (last 24 h). Static metadata reflects ECS task definitions and docker-compose.
          </p>
        </div>

        {/* Architecture diagram description */}
        <div className="bg-indigo-50 border border-indigo-200 rounded-xl p-4">
          <p className="text-xs font-semibold text-indigo-800 mb-1">EEP + IEP Architecture</p>
          <p className="text-xs text-indigo-700 leading-relaxed">
            <strong>EEP</strong> (Execution Engine Pipeline) is the central orchestrator — it owns
            job management, routing decisions, quality gates, and lineage recording.
            {" "}<strong>IEP</strong> services (Internal Execution Pipelines) are stateless inference
            microservices called exclusively by <code className="font-mono">eep_worker</code>.
            Each IEP service exposes a single inference endpoint; EEP coordinates them via
            Redis task queues. IEP1A and IEP1B run in parallel; their structural agreement
            determines the routing decision.
          </p>
        </div>

        {isLoading ? (
          <div className="space-y-4">
            <Skeleton className="h-48 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : (
          <>
            {/* EEP services */}
            <ServiceTable
              title="EEP Services (Orchestration Layer)"
              description="Central orchestrator, worker, recovery, and auxiliary pipeline services."
              items={eepServices}
            />

            {/* IEP services */}
            <ServiceTable
              title="IEP Services (Inference Layer)"
              description="Stateless AI inference microservices. Called only by eep_worker — never directly by clients."
              items={iepServices}
            />


            <p className="text-2xs text-slate-400 text-right">
              {data?.as_of && <>Data as of {formatDate(data.as_of)} · window {data.window_hours}h</>}
            </p>
          </>
        )}
      </div>
    </AdminShell>
  );
}
