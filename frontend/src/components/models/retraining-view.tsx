"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import {
  AlertTriangle,
  CheckCircle,
  Clock,
  Play,
  RefreshCw,
  XCircle,
  Zap,
} from "lucide-react";
import { getRetrainingStatus, triggerManualRetraining } from "@/lib/api/retraining";
import { getApiErrorMessage } from "@/lib/api/client";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorBanner } from "@/components/shared/error-banner";
import { ConfirmModal } from "@/components/shared/confirm-modal";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type { RetrainingJobSummary, TriggerCooldown } from "@/types/api";
import { cn, formatDate, formatRelative, snakeToTitle } from "@/lib/utils";

export function RetrainingView() {
  const queryClient = useQueryClient();
  const [showTriggerModal, setShowTriggerModal] = useState(false);
  const [triggerBlockedReason, setTriggerBlockedReason] = useState<string | null>(null);
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["retraining-status"],
    queryFn: getRetrainingStatus,
    staleTime: 20_000,
    refetchInterval: 30_000,
  });

  const triggerMutation = useMutation({
    mutationFn: triggerManualRetraining,
    onSuccess: (result) => {
      if (result.worker_start_status === "failed") {
        toast.error(result.worker_start_message || "Retraining queued, but worker start failed.");
      } else {
        toast.success(result.message || "Manual retraining queued.");
      }
      setShowTriggerModal(false);
      queryClient.invalidateQueries({ queryKey: ["retraining-status"] });
    },
    onError: (error) => {
      const msg = getApiErrorMessage(error, "Could not queue manual retraining.");
      // 422 = insufficient data — show as a persistent banner, not just a toast
      // so the admin understands what action they need to take.
      if ((error as { response?: { status?: number } })?.response?.status === 422) {
        setTriggerBlockedReason(msg);
        setShowTriggerModal(false);
      } else {
        toast.error(msg);
      }
    },
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Spinner size="lg" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <ErrorBanner
        variant="fullscreen"
        title="Failed to Load"
        message="Could not load retraining status."
      />
    );
  }

  const { summary, active_jobs, queued_jobs, recently_completed, trigger_cooldowns, as_of } = data;
  const hasRetrainingInFlight = summary.active_count > 0 || summary.queued_count > 0;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-3 gap-4 sm:grid-cols-6">
        <SummaryCard label="Active" value={summary.active_count} tone="blue" />
        <SummaryCard label="Queued" value={summary.queued_count} tone="amber" />
        <SummaryCard label="Completed" value={summary.completed_count} tone="emerald" />
        <SummaryCard label="Failed" value={summary.failed_count} tone="red" />
        <SummaryCard label="Total Triggers" value={summary.total_triggers} tone="slate" />
        <SummaryCard
          label="Pending"
          value={summary.pending_triggers}
          tone="orange"
          attention={summary.pending_triggers > 0}
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-slate-500">As of {formatDate(as_of)}</p>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            className="gap-1.5 text-slate-500"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => { setTriggerBlockedReason(null); setShowTriggerModal(true); }}
            disabled={hasRetrainingInFlight || triggerMutation.isPending}
            className="gap-1.5"
          >
            <Play className="h-3.5 w-3.5" />
            Retrain
          </Button>
        </div>
      </div>

      {triggerBlockedReason && (
        <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div>
            <p className="font-semibold mb-0.5">Retraining cannot start — not enough training data</p>
            <p className="text-amber-700">{triggerBlockedReason}</p>
            <p className="mt-1 text-amber-600">
              Accept more human-corrected pages (at least 10 per model/material combination) to unlock retraining.
            </p>
          </div>
          <button
            className="ml-auto shrink-0 text-amber-400 hover:text-amber-600"
            onClick={() => setTriggerBlockedReason(null)}
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}

      <JobSection
        title="Active Jobs"
        jobs={active_jobs}
        emptyText="No jobs currently running."
        variant="active"
      />

      <JobSection
        title="Queued Jobs"
        jobs={queued_jobs}
        emptyText="No jobs in queue."
        variant="queued"
      />

      <div>
        <h3 className="mb-3 text-sm font-semibold text-slate-900">Trigger Cooldowns</h3>
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm shadow-slate-200/60">
          {trigger_cooldowns.length === 0 ? (
            <EmptyState title="No trigger cooldowns" />
          ) : (
            <table className="w-full data-table">
              <thead>
                <tr>
                  <th>Trigger Type</th>
                  <th>Status</th>
                  <th>Cooldown Until</th>
                  <th>Last Fired</th>
                  <th>Last Status</th>
                </tr>
              </thead>
              <tbody>
                {trigger_cooldowns.map((cooldown) => (
                  <CooldownRow key={cooldown.trigger_type} cooldown={cooldown} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <JobSection
        title="Recently Completed"
        jobs={recently_completed}
        emptyText="No jobs completed in the last 72h."
        variant="completed"
      />

      <ConfirmModal
        open={showTriggerModal}
        onOpenChange={setShowTriggerModal}
        title="Start retraining?"
        description="This queues a manual preprocessing retraining run for the IEP1A and IEP1B model pair."
        confirmLabel="Start Retraining"
        loading={triggerMutation.isPending}
        onConfirm={() => triggerMutation.mutate()}
      />
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone,
  attention,
}: {
  label: string;
  value: number;
  tone: "blue" | "amber" | "emerald" | "red" | "slate" | "orange";
  attention?: boolean;
}) {
  const toneMap: Record<"blue" | "amber" | "emerald" | "red" | "slate" | "orange", string> = {
    blue: "bg-blue-50 border-blue-200 text-blue-600",
    amber: "bg-amber-50 border-amber-200 text-amber-600",
    emerald: "bg-emerald-50 border-emerald-200 text-emerald-600",
    red: "bg-red-50 border-red-200 text-red-600",
    slate: "bg-white border-slate-200 text-slate-900",
    orange: "bg-orange-50 border-orange-200 text-orange-600",
  };

  return (
    <div
      className={cn(
        "rounded-xl border p-4 text-center shadow-sm",
        toneMap[tone],
        attention && value > 0 && "ring-2 ring-orange-100"
      )}
    >
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
      <p className="mt-1 text-2xs text-slate-500">{label}</p>
    </div>
  );
}

function JobSection({
  title,
  jobs,
  emptyText,
  variant,
}: {
  title: string;
  jobs: RetrainingJobSummary[];
  emptyText: string;
  variant: "active" | "queued" | "completed";
}) {
  return (
    <div>
      <h3 className="mb-3 text-sm font-semibold text-slate-900">{title}</h3>
      {jobs.length === 0 ? (
        <div className="rounded-xl border border-slate-200 bg-white py-8 text-center shadow-sm shadow-slate-200/60">
          <p className="text-xs text-slate-500">{emptyText}</p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {jobs.map((job) => (
            <RetrainingJobCard key={job.job_id} job={job} variant={variant} />
          ))}
        </div>
      )}
    </div>
  );
}

function RetrainingJobCard({
  job,
  variant,
}: {
  job: RetrainingJobSummary;
  variant: "active" | "queued" | "completed";
}) {
  const statusIcon = {
    running: <Play className="h-3.5 w-3.5 text-blue-600 animate-pulse" />,
    pending: <Clock className="h-3.5 w-3.5 text-amber-600" />,
    completed: <CheckCircle className="h-3.5 w-3.5 text-emerald-600" />,
    failed: <XCircle className="h-3.5 w-3.5 text-red-600" />,
  }[job.status] ?? <Clock className="h-3.5 w-3.5 text-slate-500" />;

  const surfaceClass = {
    active: "bg-blue-50/50 border-blue-100",
    queued: "bg-amber-50/50 border-amber-100",
    completed: "bg-emerald-50/40 border-emerald-100",
  }[variant];

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm shadow-slate-200/60">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <div
            className={cn(
              "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border",
              surfaceClass
            )}
          >
            {statusIcon}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <code className="font-mono text-xs text-indigo-600">
                {job.job_id.substring(0, 8)}...
              </code>
              <span className="text-xs capitalize text-slate-500">{job.pipeline_type}</span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-3 text-2xs text-slate-500">
              {job.dataset_version && <span>Dataset: {job.dataset_version}</span>}
              {job.mlflow_run_id && <span>MLflow: {job.mlflow_run_id.substring(0, 8)}...</span>}
              {job.trigger_id && <span>Trigger: {job.trigger_id.substring(0, 8)}...</span>}
            </div>
          </div>
        </div>

        <div className="flex flex-col items-end gap-1.5">
          <Badge
            variant={
              job.status === "completed"
                ? "success"
                : job.status === "failed"
                ? "danger"
                : job.status === "running"
                ? "info"
                : "warning"
            }
          >
            {job.status}
          </Badge>

          {job.result_mAP != null && (
            <span className="text-xs text-slate-500">
              mAP:{" "}
              <span className="font-semibold tabular-nums text-slate-900">
                {job.result_mAP.toFixed(3)}
              </span>
            </span>
          )}

          {job.promotion_decision && (
            <span
              className={cn(
                "text-xs font-medium",
                job.promotion_decision === "promoted"
                  ? "text-emerald-600"
                  : "text-slate-500"
              )}
            >
              {snakeToTitle(job.promotion_decision)}
            </span>
          )}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-4 text-2xs text-slate-500">
        <span>Created {formatRelative(job.created_at)}</span>
        {job.started_at && <span>Started {formatRelative(job.started_at)}</span>}
        {job.completed_at && <span>Completed {formatRelative(job.completed_at)}</span>}
      </div>

      {job.error_message && (
        <div className="mt-3 rounded-lg border border-red-200 bg-red-50 p-2.5">
          <p className="text-xs text-red-700">{job.error_message}</p>
        </div>
      )}
    </div>
  );
}

function CooldownRow({ cooldown }: { cooldown: TriggerCooldown }) {
  return (
    <tr>
      <td>
        <div className="flex items-center gap-2">
          <Zap className="h-3.5 w-3.5 text-slate-400" />
          <span className="text-xs text-slate-700">{snakeToTitle(cooldown.trigger_type)}</span>
        </div>
      </td>
      <td>
        {cooldown.in_cooldown ? (
          <Badge variant="warning" dot>
            In Cooldown
          </Badge>
        ) : (
          <Badge variant="muted" dot>
            Ready
          </Badge>
        )}
      </td>
      <td>
        <span className="text-xs text-slate-500">
          {cooldown.cooldown_until ? formatRelative(cooldown.cooldown_until) : "-"}
        </span>
      </td>
      <td>
        <span className="text-xs text-slate-500">
          {cooldown.last_fired_at ? formatRelative(cooldown.last_fired_at) : "-"}
        </span>
      </td>
      <td>
        <span className="text-xs capitalize text-slate-500">
          {cooldown.last_status ?? "-"}
        </span>
      </td>
    </tr>
  );
}
