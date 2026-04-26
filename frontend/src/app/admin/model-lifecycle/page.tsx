"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Layers, RefreshCw, AlertTriangle, CheckCircle2, XCircle, ArrowUp, RotateCcw } from "lucide-react";
import { getPromotionAudit, getModelGateComparisons, getDeploymentStatus } from "@/lib/api/admin";
import { getModelEvaluations } from "@/lib/api/models";
import { getRetrainingStatus } from "@/lib/api/retraining";
import { AdminShell } from "@/components/layout/admin-shell";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { formatDate } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { ModelVersionRecord, PromotionAuditRecord } from "@/types/api";

// ── Stage badge ───────────────────────────────────────────────────────────────

function StageBadge({ stage }: { stage: string }) {
  const variant: "success" | "info" | "muted" | "warning" =
    stage === "production"
      ? "success"
      : stage === "staging"
      ? "info"
      : stage === "shadow"
      ? "warning"
      : "muted";
  return <Badge variant={variant} className="text-2xs">{stage}</Badge>;
}

// ── Model version card ────────────────────────────────────────────────────────

function ModelCard({ model }: { model: ModelVersionRecord }) {
  const gates = model.gate_results ?? {};
  const gateEntries = Object.entries(gates);

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div>
          <p className="text-xs font-semibold font-mono text-slate-800">{model.version_tag}</p>
          <p className="text-2xs text-slate-500">{model.service_name}</p>
        </div>
        <StageBadge stage={model.stage} />
      </div>

      {model.gate_summary && (
        <div className="mb-2">
          <div className="flex items-center gap-1.5 mb-1">
            {model.gate_summary.all_pass ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
            ) : (
              <XCircle className="h-3.5 w-3.5 text-red-500" />
            )}
            <span className="text-2xs text-slate-600">
              {model.gate_summary.passed_gates}/{model.gate_summary.total_gates} gates passed
            </span>
          </div>
          {model.gate_summary.failed_names.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {model.gate_summary.failed_names.map((g) => (
                <span key={g} className="text-2xs font-mono bg-red-50 text-red-700 border border-red-200 px-1.5 py-0.5 rounded">
                  {g}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="text-2xs text-slate-400 space-y-0.5">
        {model.dataset_version && <p>Dataset: <span className="font-mono text-slate-600">{model.dataset_version}</span></p>}
        {model.promoted_at && <p>Promoted: {formatDate(model.promoted_at)}</p>}
        <p>Created: {formatDate(model.created_at)}</p>
      </div>
    </div>
  );
}

// ── Promotion audit row ───────────────────────────────────────────────────────

function AuditRow({ record }: { record: PromotionAuditRecord }) {
  return (
    <div className="flex items-start gap-3 py-3 border-b border-slate-100 last:border-0">
      <div className="shrink-0 mt-0.5">
        {record.action === "promote" ? (
          <ArrowUp className="h-4 w-4 text-emerald-500" />
        ) : (
          <RotateCcw className="h-4 w-4 text-amber-500" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between flex-wrap gap-1 mb-0.5">
          <span className="text-xs font-semibold text-slate-800 capitalize">{record.action}</span>
          <span className="text-2xs text-slate-400">{formatDate(record.created_at)}</span>
        </div>
        <p className="text-2xs text-slate-500">
          <span className="font-mono text-indigo-600">{record.service_name}</span>
          {" · "}model <span className="font-mono">{record.candidate_model_id.slice(0, 8)}…</span>
          {record.previous_model_id && (
            <> · displaced <span className="font-mono">{record.previous_model_id.slice(0, 8)}…</span></>
          )}
        </p>
        {record.forced && (
          <Badge variant="warning" className="text-2xs mt-1">FORCED — gates bypassed</Badge>
        )}
        {record.failed_gates_bypassed?.length ? (
          <p className="text-2xs text-red-600 mt-0.5">
            Bypassed: {record.failed_gates_bypassed.join(", ")}
          </p>
        ) : null}
        {record.reason && (
          <p className="text-2xs text-slate-400 italic mt-0.5">&ldquo;{record.reason}&rdquo;</p>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function ModelLifecyclePage() {
  const [auditOffset, setAuditOffset] = useState(0);
  const auditLimit = 20;

  const { data: modelsData, isLoading: modelsLoading, refetch: refetchModels, isFetching } = useQuery({
    queryKey: ["model-evaluations-lifecycle"],
    queryFn: () => getModelEvaluations({ limit: 50 }),
    staleTime: 30_000,
  });

  const { data: auditData, isLoading: auditLoading, refetch: refetchAudit } = useQuery({
    queryKey: ["promotion-audit", { offset: auditOffset, limit: auditLimit }],
    queryFn: () => getPromotionAudit({ limit: auditLimit, offset: auditOffset }),
    staleTime: 30_000,
  });

  const { data: shadowData, isLoading: shadowLoading } = useQuery({
    queryKey: ["model-gate-comparisons-lifecycle", { limit: 20 }],
    queryFn: () => getModelGateComparisons({ limit: 20 }),
    staleTime: 30_000,
  });

  const { data: retrainingData } = useQuery({
    queryKey: ["retraining-status"],
    queryFn: getRetrainingStatus,
    staleTime: 30_000,
  });

  const { data: deployment } = useQuery({
    queryKey: ["deployment-status"],
    queryFn: getDeploymentStatus,
    staleTime: 60_000,
  });

  const productionModels = modelsData?.records.filter((m) => m.stage === "production") ?? [];
  const stagingModels = modelsData?.records.filter((m) => m.stage === "staging") ?? [];
  const shadowModels = modelsData?.records.filter((m) => m.stage === "shadow") ?? [];
  const archivedModels = modelsData?.records.filter((m) => m.stage === "archived") ?? [];

  function refetchAll() {
    refetchModels(); refetchAudit();
  }

  return (
    <AdminShell
      breadcrumbs={[{ label: "Model Lifecycle" }]}
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
            <Layers className="h-5 w-5 text-slate-500" />
            <h1 className="text-base font-semibold text-slate-900">Model Lifecycle</h1>
          </div>
          <p className="text-xs text-slate-500">
            Model version stages, gate results, promotion audit, shadow evaluations, and retraining state.
            For promotion/rollback controls see{" "}
            <a href="/admin/models" className="text-indigo-500 hover:underline">Model Evaluation</a>.
          </p>
        </div>

        {/* Retraining mode banner */}
        {deployment?.feature_flags && (
          <div className={cn(
            "flex items-start gap-3 rounded-xl border px-4 py-3",
            deployment.feature_flags.retraining_mode === "stub"
              ? "border-amber-200 bg-amber-50"
              : "border-emerald-200 bg-emerald-50"
          )}>
            <AlertTriangle className={cn("h-4 w-4 shrink-0 mt-0.5",
              deployment.feature_flags.retraining_mode === "stub" ? "text-amber-500" : "text-emerald-500"
            )} />
            <div>
              <p className={cn("text-xs font-semibold",
                deployment.feature_flags.retraining_mode === "stub" ? "text-amber-800" : "text-emerald-800"
              )}>
                Retraining Mode: <span className="uppercase font-mono">{deployment.feature_flags.retraining_mode}</span>
                {" · "}Golden Eval: <span className="uppercase font-mono">{deployment.feature_flags.golden_eval_mode}</span>
              </p>
              <p className={cn("text-xs mt-0.5",
                deployment.feature_flags.retraining_mode === "stub" ? "text-amber-700" : "text-emerald-700"
              )}>
                {deployment.feature_flags.retraining_mode === "stub"
                  ? "Live model training is intentionally disabled. The full promotion / rollback / gate pipeline is implemented and tested. Training compute is stubbed pending GPU budget and dataset validation."
                  : "Live training is active. New model versions are trained against the full dataset pipeline."}
              </p>
            </div>
          </div>
        )}

        {/* Model versions by stage */}
        <div className="space-y-4">
          <ModelStageSection
            title="Production"
            badge="production"
            models={productionModels}
            loading={modelsLoading}
            emptyMsg="No production model. Promote a staging candidate from Model Evaluation."
          />
          <ModelStageSection
            title="Staging (Promotion Candidates)"
            badge="staging"
            models={stagingModels}
            loading={modelsLoading}
            emptyMsg="No staging candidates. Trigger an evaluation run to create one."
          />
          {shadowModels.length > 0 && (
            <ModelStageSection title="Shadow" badge="shadow" models={shadowModels} loading={modelsLoading} />
          )}
          {archivedModels.length > 0 && (
            <ModelStageSection title="Archived (recent 5)" badge="archived" models={archivedModels.slice(0, 5)} loading={modelsLoading} />
          )}
        </div>

        {/* Retraining summary */}
        {retrainingData && (
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <h2 className="text-sm font-semibold text-slate-800 mb-3">Retraining Pipeline Status</h2>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
              <Stat label="Active" value={retrainingData.summary.active_count} />
              <Stat label="Queued" value={retrainingData.summary.queued_count} />
              <Stat label="Completed" value={retrainingData.summary.completed_count} />
              <Stat label="Failed" value={retrainingData.summary.failed_count} tone={retrainingData.summary.failed_count > 0 ? "bad" : "neutral"} />
            </div>
            {retrainingData.recently_completed.length > 0 && (
              <>
                <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-2">Recent completions</p>
                <div className="space-y-2">
                  {retrainingData.recently_completed.slice(0, 5).map((job) => (
                    <div key={job.job_id} className="flex items-center justify-between rounded-lg bg-slate-50 border border-slate-100 px-3 py-2">
                      <div>
                        <span className="text-xs font-mono text-slate-600">{job.pipeline_type}</span>
                        {job.dataset_version && (
                          <span className="text-2xs text-slate-400 ml-2">dataset: {job.dataset_version}</span>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        {job.result_mAP != null && (
                          <span className="text-xs font-semibold text-indigo-700 tabular-nums">
                            mAP {job.result_mAP.toFixed(4)}
                          </span>
                        )}
                        <Badge
                          variant={job.status === "completed" ? "success" : job.status === "failed" ? "danger" : "muted"}
                          className="text-2xs"
                        >
                          {job.status}
                        </Badge>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {/* Promotion audit */}
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between mb-1">
            <h2 className="text-sm font-semibold text-slate-800">Promotion &amp; Rollback Audit</h2>
            <span className="text-2xs text-slate-400">Total: {auditData?.total ?? "—"}</span>
          </div>
          <p className="text-2xs text-slate-400 mb-3">
            Every promote/rollback action is recorded with actor, gates bypassed, and reason.
            Manual admin approval is required for all promotions.
          </p>

          {auditLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-14" />)}
            </div>
          ) : !auditData?.items.length ? (
            <p className="text-xs text-slate-400 italic">No promotion or rollback events yet.</p>
          ) : (
            <>
              {auditData.items.map((record) => (
                <AuditRow key={record.audit_id} record={record} />
              ))}
              {auditData.total > auditLimit && (
                <div className="flex items-center justify-between pt-3 border-t border-slate-100 mt-3">
                  <span className="text-xs text-slate-500">
                    Showing {auditOffset + 1}–{Math.min(auditOffset + auditLimit, auditData.total)} of {auditData.total}
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setAuditOffset(Math.max(0, auditOffset - auditLimit))}
                      disabled={auditOffset === 0}
                    >
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setAuditOffset(auditOffset + auditLimit)}
                      disabled={auditOffset + auditLimit >= auditData.total}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Model gate comparison summary */}
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between mb-1">
            <h2 className="text-sm font-semibold text-slate-800">Offline Model Gate Comparisons</h2>
            <span className="text-2xs text-slate-400">Total: {shadowData?.total ?? "—"}</span>
          </div>
          <p className="text-2xs text-slate-400 mb-3">
            Compares the geometry IoU gate score of the current <code className="font-mono">shadow</code>-stage
            model against the current <code className="font-mono">production</code>-stage model. The delta is a
            model-level metric from offline evaluation — no candidate inference ran on live pages.
          </p>
          {shadowLoading ? (
            <Skeleton className="h-24" />
          ) : !shadowData?.items.length ? (
            <p className="text-xs text-slate-400 italic">No shadow evaluations recorded.</p>
          ) : (
            <div className="grid grid-cols-3 gap-3">
              <Stat label="Total evals" value={shadowData.total} />
              <Stat
                label="Completed"
                value={shadowData.items.filter((e) => e.status === "completed").length}
                tone="good"
              />
              <Stat
                label="Failed"
                value={shadowData.items.filter((e) => e.status === "failed").length}
                tone={shadowData.items.some((e) => e.status === "failed") ? "bad" : "neutral"}
              />
            </div>
          )}
        </div>
      </div>
    </AdminShell>
  );
}

// ── Model stage section ───────────────────────────────────────────────────────

function ModelStageSection({
  title,
  badge,
  models,
  loading,
  emptyMsg,
}: {
  title: string;
  badge: string;
  models: ModelVersionRecord[];
  loading: boolean;
  emptyMsg?: string;
}) {
  return (
    <div>
      <p className="text-2xs font-medium text-slate-400 uppercase tracking-wide mb-2">{title}</p>
      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 2 }).map((_, i) => <Skeleton key={i} className="h-32" />)}
        </div>
      ) : models.length === 0 ? (
        <p className="text-xs text-slate-400 italic">{emptyMsg ?? "None."}</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {models.map((m) => <ModelCard key={m.model_id} model={m} />)}
        </div>
      )}
    </div>
  );
}

// ── Stat tile ────────────────────────────────────────────────────────────────

function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: number;
  tone?: "neutral" | "good" | "bad";
}) {
  return (
    <div className="bg-slate-50 border border-slate-100 rounded-lg px-3 py-2 text-center">
      <p className={cn(
        "text-xl font-bold tabular-nums",
        tone === "good" ? "text-emerald-700" : tone === "bad" ? "text-red-700" : "text-slate-800"
      )}>
        {value}
      </p>
      <p className="text-2xs text-slate-400 mt-0.5">{label}</p>
    </div>
  );
}
