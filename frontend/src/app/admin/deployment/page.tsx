"use client";

import { useQuery } from "@tanstack/react-query";
import { Rocket, RefreshCw, AlertTriangle, CheckCircle2, XCircle, CircleDashed } from "lucide-react";
import { getDeploymentStatus, getQueueStatus } from "@/lib/api/admin";
import { AdminShell } from "@/components/layout/admin-shell";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { formatDate } from "@/lib/utils";
import { cn } from "@/lib/utils";

function InfoRow({
  label,
  value,
  mono = false,
  missing = false,
}: {
  label: string;
  value: string | null | undefined;
  mono?: boolean;
  missing?: boolean;
}) {
  return (
    <div className="flex items-start justify-between py-2.5 border-b border-slate-100 last:border-0">
      <span className="text-xs text-slate-500">{label}</span>
      <span className={cn("text-xs font-semibold text-right", mono && "font-mono", missing && "text-slate-400 italic")}>
        {value ?? "—"}
      </span>
    </div>
  );
}

function FlagRow({
  label,
  value,
  description,
}: {
  label: string;
  value: string;
  description: string;
}) {
  const isLive = value === "live";
  const isDisabled = value === "disabled";

  return (
    <div className="flex items-start gap-3 py-2.5 border-b border-slate-100 last:border-0">
      <div className="shrink-0 mt-0.5">
        {isLive ? (
          <CheckCircle2 className="h-4 w-4 text-emerald-500" />
        ) : isDisabled ? (
          <XCircle className="h-4 w-4 text-red-500" />
        ) : (
          <CircleDashed className="h-4 w-4 text-amber-500" />
        )}
      </div>
      <div className="flex-1">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-slate-700">{label}</span>
          <Badge
            variant={isLive ? "success" : isDisabled ? "danger" : "warning"}
            className="text-2xs font-mono uppercase"
          >
            {value}
          </Badge>
        </div>
        <p className="text-2xs text-slate-400 mt-0.5 leading-snug">{description}</p>
      </div>
    </div>
  );
}

export default function DeploymentPage() {
  const { data: deployment, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["deployment-status"],
    queryFn: getDeploymentStatus,
    staleTime: 60_000,
  });

  const { data: queue, isLoading: qLoading } = useQuery({
    queryKey: ["queue-status"],
    queryFn: getQueueStatus,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });

  return (
    <AdminShell
      breadcrumbs={[{ label: "Deployment" }]}
      headerRight={
        <Button variant="ghost" size="sm" onClick={() => refetch()} className="gap-1.5 text-slate-500">
          <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
          <span className="text-xs">Refresh</span>
        </Button>
      }
    >
      <div className="p-6 space-y-6">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Rocket className="h-5 w-5 text-slate-500" />
            <h1 className="text-base font-semibold text-slate-900">Deployment &amp; Scaling</h1>
          </div>
          <p className="text-xs text-slate-500">
            Deployment metadata from environment variables and the database. No ECS API calls are made —
            values reflect what the running service knows about itself.
          </p>
        </div>

        {isLoading ? (
          <div className="space-y-4">
            <Skeleton className="h-48" />
            <Skeleton className="h-48" />
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Deployment identity */}
            <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">Deployment Identity</h2>
              <InfoRow label="Image Tag" value={deployment?.image_tag} mono missing={!deployment?.image_tag} />
              <InfoRow label="Git SHA" value={deployment?.git_sha} mono missing={!deployment?.git_sha} />
              <InfoRow label="ECS Cluster" value={deployment?.ecs_cluster} mono missing={!deployment?.ecs_cluster} />
              <InfoRow label="ECS Service" value={deployment?.ecs_service} mono missing={!deployment?.ecs_service} />
              <InfoRow label="DB Migration Version" value={deployment?.alembic_version} mono />
              <InfoRow label="S3 Bucket" value={deployment?.s3_bucket} mono missing={!deployment?.s3_bucket} />
              <InfoRow
                label="Redis Configured"
                value={deployment?.redis_url_configured ? "yes" : "no"}
              />
              <InfoRow label="As Of" value={deployment?.as_of ? formatDate(deployment.as_of) : undefined} />
              {(!deployment?.image_tag && !deployment?.git_sha) && (
                <div className="mt-3 flex items-start gap-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                  <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                  <span>
                    <code className="font-mono">LIBRARYAI_IMAGE_TAG</code> and{" "}
                    <code className="font-mono">GIT_SHA</code> environment variables are not set.
                    Set them in your ECS task definition or docker-compose for traceability.
                  </span>
                </div>
              )}
            </div>

            {/* Feature flags */}
            <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">Feature Flags</h2>
              {deployment?.feature_flags && (
                <>
                  <FlagRow
                    label="Retraining Mode"
                    value={deployment.feature_flags.retraining_mode}
                    description="Controlled by LIBRARYAI_RETRAINING_TRAIN env var. stub = pipeline executes but skips actual model training."
                  />
                  <FlagRow
                    label="Golden Eval Mode"
                    value={deployment.feature_flags.golden_eval_mode}
                    description="Controlled by LIBRARYAI_RETRAINING_GOLDEN_EVAL env var. stub = gate thresholds are evaluated against synthetic scores."
                  />
                  <FlagRow
                    label="Artifact Cleanup"
                    value={deployment.feature_flags.artifact_cleanup}
                    description="Not implemented. Safe S3 retention/deletion logic is pending. The service exists in docker-compose but does not run."
                  />
                </>
              )}
              <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-lg">
                <p className="text-xs font-semibold text-amber-800 mb-1">Live Retraining Disabled</p>
                <p className="text-xs text-amber-700">
                  Live model retraining is intentionally not enabled in the current deployment.
                  The pipeline flow, gate checks, staging, promotion, and rollback are all fully
                  implemented and tested — but the training compute step remains stubbed
                  pending GPU budget allocation and training dataset validation.
                </p>
              </div>
            </div>

            {/* Queue state */}
            <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">Queue State (Live)</h2>
              {qLoading ? (
                <Skeleton className="h-32" />
              ) : (
                <>
                  <InfoRow label="Page Tasks — Queued" value={String(queue?.page_tasks_queued ?? "—")} />
                  <InfoRow label="Page Tasks — In-Flight" value={String(queue?.page_tasks_processing ?? "—")} />
                  <InfoRow
                    label="Page Tasks — Dead-Letter"
                    value={String(queue?.page_tasks_dead_letter ?? "—")}
                  />
                  <InfoRow label="Shadow Tasks — Queued" value={String(queue?.shadow_tasks_queued ?? "—")} />
                  <InfoRow
                    label="Worker Slots Available"
                    value={
                      queue?.worker_slots_available != null
                        ? `${queue.worker_slots_available} / ${queue.worker_slots_max}`
                        : "—"
                    }
                  />
                </>
              )}
            </div>

            {/* Cost / scaling notes */}
            <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
              <h2 className="text-sm font-semibold text-slate-800 mb-3">Scaling &amp; Cost Notes</h2>
              <div className="space-y-3 text-xs text-slate-600">
                <div className="p-3 bg-slate-50 rounded-lg border border-slate-200">
                  <p className="font-semibold text-slate-700 mb-1">ECS Service Scaling</p>
                  <p>
                    EEP and EEP Worker run as Fargate services. GPU inference services (IEP1A, IEP1B,
                    IEP1D, IEP2A, IEP2B) run on EC2 GPU task definitions. Scale-up and scale-down
                    are triggered by GitHub Actions workflows (
                    <code className="font-mono">.github/workflows/scale-up.yml</code> /
                    <code className="font-mono">scale-down.yml</code>).
                  </p>
                </div>
                <div className="p-3 bg-slate-50 rounded-lg border border-slate-200">
                  <p className="font-semibold text-slate-700 mb-1">GPU Cost Estimation</p>
                  <p>
                    Actual GPU-hour cost depends on EC2 instance type and ASG desired count.
                    ECS task CPU/memory are defined in{" "}
                    <code className="font-mono">k8s/ecs/*.json</code>. Live billing data
                    is not available via this dashboard — check AWS Cost Explorer.
                  </p>
                </div>
                <div className="p-3 bg-slate-50 rounded-lg border border-slate-200">
                  <p className="font-semibold text-slate-700 mb-1">S3 Storage</p>
                  <p>
                    All input, intermediate, and output artifacts are stored in the configured
                    S3 bucket. Artifact cleanup is disabled — bucket size grows with processed
                    jobs. Retention policy has not been implemented.
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </AdminShell>
  );
}
