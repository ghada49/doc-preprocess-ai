"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import {
  ArrowUpCircle,
  CheckCircle,
  Clock,
  FlaskConical,
  RefreshCw,
  RotateCcw,
  XCircle,
  Zap,
} from "lucide-react";
import {
  getModelEvaluations,
  promoteModel,
  rollbackModel,
  triggerEvaluation,
} from "@/lib/api/models";
import { getApiErrorMessage, isApiError } from "@/lib/api/client";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorBanner } from "@/components/shared/error-banner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { ModelStage, ModelVersionRecord } from "@/types/api";
import { cn, formatDate, formatRelative, modelStageClass } from "@/lib/utils";

export function ModelEvalDashboard() {
  const queryClient = useQueryClient();
  const [serviceFilter, setServiceFilter] = useState<string>("all");
  const [stageFilter, setStageFilter] = useState<string>("all");
  const [showEvalModal, setShowEvalModal] = useState(false);
  const [evalTag, setEvalTag] = useState("");
  const [evalService, setEvalService] = useState("iep1a");

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["model-evaluations", { serviceFilter, stageFilter }],
    queryFn: () =>
      getModelEvaluations({
        service: serviceFilter !== "all" ? serviceFilter : undefined,
        stage: stageFilter !== "all" ? (stageFilter as ModelStage) : undefined,
        limit: 50,
      }),
    staleTime: 15_000,
  });

  const records = data?.records ?? [];

  const triggerMut = useMutation({
    mutationFn: () => triggerEvaluation({ candidate_tag: evalTag, service: evalService }),
    onSuccess: (res) => {
      toast.success(`Evaluation queued: ${res.message.substring(0, 60)}...`);
      setShowEvalModal(false);
      setEvalTag("");
      queryClient.invalidateQueries({ queryKey: ["model-evaluations"] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 404) toast.error("No model found for that service and tag.");
      else if (status === 409) toast.error("Evaluation already pending for this candidate.");
      else toast.error(getApiErrorMessage(err, "Failed to trigger evaluation."));
    },
  });

  const promoteMut = useMutation({
    mutationFn: (service: "iep1a" | "iep1b") => promoteModel({ service, force: false }),
    onSuccess: () => {
      toast.success("Model promoted to production.");
      queryClient.invalidateQueries({ queryKey: ["model-evaluations"] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 404) toast.error("No staging candidate found.");
      else if (status === 409) toast.error("Gate check failed. Promotion blocked.");
      else toast.error(getApiErrorMessage(err, "Promotion failed."));
    },
  });

  const rollbackMut = useMutation({
    mutationFn: (service: "iep1a" | "iep1b") => rollbackModel({ service, reason: "manual" }),
    onSuccess: () => {
      toast.success("Model rolled back to the previous version.");
      queryClient.invalidateQueries({ queryKey: ["model-evaluations"] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 404) toast.error("No archived version to roll back to.");
      else if (status === 409) toast.error("Rollback window has expired (2h).");
      else toast.error(getApiErrorMessage(err, "Rollback failed."));
    },
  });

  if (isError) {
    return (
      <ErrorBanner
        variant="fullscreen"
        title="Failed to Load"
        message="Could not load model evaluation records."
      />
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Select value={serviceFilter} onValueChange={setServiceFilter}>
          <SelectTrigger className="w-36">
            <SelectValue placeholder="Service" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All services</SelectItem>
            <SelectItem value="iep1a">IEP1A</SelectItem>
            <SelectItem value="iep1b">IEP1B</SelectItem>
          </SelectContent>
        </Select>

        <Select value={stageFilter} onValueChange={setStageFilter}>
          <SelectTrigger className="w-36">
            <SelectValue placeholder="Stage" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All stages</SelectItem>
            <SelectItem value="experimental">Experimental</SelectItem>
            <SelectItem value="staging">Staging</SelectItem>
            <SelectItem value="shadow">Shadow</SelectItem>
            <SelectItem value="production">Production</SelectItem>
            <SelectItem value="archived">Archived</SelectItem>
          </SelectContent>
        </Select>

        <Button
          variant="ghost"
          size="icon"
          onClick={() => refetch()}
          className="h-9 w-9 text-slate-500"
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
        </Button>

        <Button
          size="sm"
          onClick={() => setShowEvalModal(true)}
          className="ml-auto gap-2"
        >
          <Zap className="h-3.5 w-3.5" />
          Trigger Evaluation
        </Button>
      </div>

      <div className="space-y-3">
        {isLoading ? (
          Array.from({ length: 4 }).map((_, index) => (
            <div
              key={index}
              className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm shadow-slate-200/60"
            >
              <div className="flex justify-between">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-5 w-20" />
              </div>
              <Skeleton className="mt-3 h-3 w-48" />
              <div className="mt-4 grid grid-cols-4 gap-3">
                {[1, 2, 3, 4].map((item) => (
                  <Skeleton key={item} className="h-12 w-full" />
                ))}
              </div>
            </div>
          ))
        ) : records.length === 0 ? (
          <EmptyState
            icon={FlaskConical}
            title="No evaluation records"
            description="Trigger an evaluation to see model records here."
          />
        ) : (
          records.map((record) => (
            <ModelRecordCard
              key={record.model_id}
              record={record}
              onPromote={() => promoteMut.mutate(record.service_name as "iep1a" | "iep1b")}
              onRollback={() => rollbackMut.mutate(record.service_name as "iep1a" | "iep1b")}
              isPromoting={promoteMut.isPending}
              isRollingBack={rollbackMut.isPending}
            />
          ))
        )}
      </div>

      <Dialog open={showEvalModal} onOpenChange={setShowEvalModal}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Trigger Model Evaluation</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 px-6">
            <div className="space-y-1.5">
              <Label>Service</Label>
              <Select value={evalService} onValueChange={setEvalService}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="iep1a">IEP1A</SelectItem>
                  <SelectItem value="iep1b">IEP1B</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Candidate Tag</Label>
              <Input
                value={evalTag}
                onChange={(event) => setEvalTag(event.target.value)}
                placeholder="e.g. v1.2.0"
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setShowEvalModal(false)}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={() => triggerMut.mutate()}
              loading={triggerMut.isPending}
              disabled={!evalTag.trim()}
            >
              Queue Evaluation
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ModelRecordCard({
  record,
  onPromote,
  onRollback,
  isPromoting,
  isRollingBack,
}: {
  record: ModelVersionRecord;
  onPromote: () => void;
  onRollback: () => void;
  isPromoting: boolean;
  isRollingBack: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const canPromote =
    record.stage === "staging" &&
    (record.service_name === "iep1a" || record.service_name === "iep1b");
  const canRollback =
    record.stage === "production" &&
    (record.service_name === "iep1a" || record.service_name === "iep1b");

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm shadow-slate-200/70">
      <div className="flex items-start justify-between gap-4 p-5">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-indigo-50 ring-1 ring-inset ring-indigo-100">
            <FlaskConical className="h-4 w-4 text-indigo-600" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold uppercase tracking-wide text-slate-900">
                {record.service_name}
              </span>
              <code className="font-mono text-xs text-indigo-600">{record.version_tag}</code>
              <span
                className={cn(
                  "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium capitalize",
                  modelStageClass(record.stage)
                )}
              >
                {record.stage}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-slate-500">
              {record.dataset_version && (
                <span>
                  Dataset: <span className="text-slate-700">{record.dataset_version}</span>
                </span>
              )}
              {record.mlflow_run_id && (
                <span>
                  MLflow:{" "}
                  <code className="font-mono text-2xs text-slate-700">
                    {record.mlflow_run_id.substring(0, 8)}...
                  </code>
                </span>
              )}
              <span>Created {formatRelative(record.created_at)}</span>
            </div>
          </div>
        </div>

        <div className="flex items-start gap-3">
          {record.gate_summary ? (
            <div className="text-right">
              {record.gate_summary.all_pass ? (
                <div className="flex items-center gap-1.5 text-emerald-600">
                  <CheckCircle className="h-4 w-4" />
                  <span className="text-xs font-medium">All gates passed</span>
                </div>
              ) : (
                <div className="flex items-center gap-1.5 text-red-600">
                  <XCircle className="h-4 w-4" />
                  <span className="text-xs font-medium">
                    {record.gate_summary.failed_gates} gate
                    {record.gate_summary.failed_gates !== 1 ? "s" : ""} failed
                  </span>
                </div>
              )}
              <p className="mt-0.5 text-right text-2xs text-slate-500">
                {record.gate_summary.passed_gates}/{record.gate_summary.total_gates} passed
              </p>
            </div>
          ) : (
            <div className="flex items-center gap-1.5 text-slate-500">
              <Clock className="h-3.5 w-3.5" />
              <span className="text-xs">No evaluation yet</span>
            </div>
          )}

          <div className="flex items-center gap-1.5">
            {canPromote && (
              <Button
                variant="success"
                size="xs"
                onClick={onPromote}
                loading={isPromoting}
                className="gap-1"
              >
                <ArrowUpCircle className="h-3 w-3" />
                Promote
              </Button>
            )}
            {canRollback && (
              <Button
                variant="danger"
                size="xs"
                onClick={onRollback}
                loading={isRollingBack}
                className="gap-1"
              >
                <RotateCcw className="h-3 w-3" />
                Rollback
              </Button>
            )}
            <Button
              variant="ghost"
              size="xs"
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? "Less" : "Details"}
            </Button>
          </div>
        </div>
      </div>

      {expanded && (
        <div className="border-t border-slate-200 bg-slate-50/80 p-5">
          {record.gate_results ? (
            <div>
              <p className="mb-3 text-2xs font-semibold uppercase tracking-wider text-slate-400">
                Gate Results
              </p>
              <div className="grid grid-cols-2 gap-2">
                {Object.entries(record.gate_results).map(([name, result]) => (
                  <div
                    key={name}
                    className={cn(
                      "flex items-center justify-between rounded-lg border p-3",
                      result.pass
                        ? "border-emerald-200 bg-emerald-50"
                        : "border-red-200 bg-red-50"
                    )}
                  >
                    <div className="flex items-center gap-2">
                      {result.pass ? (
                        <CheckCircle className="h-3.5 w-3.5 text-emerald-600" />
                      ) : (
                        <XCircle className="h-3.5 w-3.5 text-red-600" />
                      )}
                      <span className="text-xs text-slate-700">{name}</span>
                    </div>
                    {result.value != null && (
                      <code className="font-mono text-xs text-slate-600">
                        {result.value.toFixed(3)}
                      </code>
                    )}
                  </div>
                ))}
              </div>
              {record.gate_summary?.failed_names.length ? (
                <p className="mt-3 text-xs text-red-600">
                  Failed: {record.gate_summary.failed_names.join(", ")}
                </p>
              ) : null}
            </div>
          ) : (
            <p className="py-4 text-center text-xs text-slate-500">
              No gate results yet. Trigger an evaluation to populate this record.
            </p>
          )}

          {record.notes && (
            <p className="mt-3 border-t border-slate-200 pt-3 text-xs text-slate-500">
              Notes: {record.notes}
            </p>
          )}
          {record.promoted_at && (
            <p className="mt-2 text-xs text-slate-500">
              Promoted: {formatDate(record.promoted_at)}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
