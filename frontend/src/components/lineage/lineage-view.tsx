"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Copy,
  Layers,
  Shield,
  User,
  XCircle,
} from "lucide-react";
import { getLineage } from "@/lib/api/lineage";
import { ArtifactImage } from "@/components/shared/artifact-image";
import { ArtifactLinkButton } from "@/components/shared/artifact-link-button";
import { ErrorBanner } from "@/components/shared/error-banner";
import { StatusBadge } from "@/components/shared/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import {
  cn,
  formatDate,
  formatDuration,
  formatScore,
  snakeToTitle,
} from "@/lib/utils";
import type {
  LineageQualityGate,
  LineageRecord,
  LineageServiceInvocation,
} from "@/types/api";

interface LineageViewProps {
  jobId: string;
  pageNumber: number;
  subPageIndex?: number;
}

export function LineageView({
  jobId,
  pageNumber,
  subPageIndex,
}: LineageViewProps) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["lineage", jobId, pageNumber, subPageIndex],
    queryFn: () => getLineage(jobId, pageNumber, subPageIndex),
    staleTime: 30_000,
  });

  const lineageRecords = useMemo(() => {
    if (!data) return [];
    if (subPageIndex == null) return data.lineage;

    return data.lineage.filter((record) => record.sub_page_index === subPageIndex);
  }, [data, subPageIndex]);

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
        title="Lineage Unavailable"
        message="Could not load lineage for this page."
      />
    );
  }

  if (lineageRecords.length === 0) {
    return (
      <ErrorBanner
        variant="fullscreen"
        title="No Matching Lineage"
        message="No lineage row matched the selected sub-page index."
      />
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-br from-white via-white to-slate-50 shadow-sm shadow-slate-200/70">
        <div className="border-b border-slate-200 px-5 py-4 sm:px-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="muted">Audit Trail</Badge>
                {subPageIndex != null && (
                  <Badge variant="info">Sub-page {subPageIndex}</Badge>
                )}
              </div>
              <div>
                <h2 className="text-lg font-semibold tracking-tight text-slate-900">
                  Page {data.page_number}
                </h2>
                <p className="mt-1 text-sm text-slate-500">
                  Job{" "}
                  <code className="rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-xs text-indigo-700">
                    {data.job_id}
                  </code>
                </p>
              </div>
            </div>

            <div className="grid min-w-[280px] grid-cols-3 gap-3">
              <SummaryStat
                label="Lineage Rows"
                value={lineageRecords.length}
                tone="blue"
              />
              <SummaryStat
                label="Quality Gates"
                value={data.quality_gates.length}
                tone="violet"
              />
              <SummaryStat
                label="Invocations"
                value={lineageRecords.reduce(
                  (count, record) => count + record.service_invocations.length,
                  0
                )}
                tone="amber"
              />
            </div>
          </div>
        </div>

        <div className="flex flex-wrap gap-x-5 gap-y-2 px-5 py-3 text-xs text-slate-500 sm:px-6">
          <span>Job ID {data.job_id}</span>
          <span>Page {data.page_number}</span>
          <span>
            Showing {lineageRecords.length} lineage row
            {lineageRecords.length !== 1 ? "s" : ""}
          </span>
        </div>
      </div>

      {data.quality_gates.length > 0 && (
        <Section
          title="Quality Gate Decisions"
          icon={<Shield className="h-4 w-4 text-violet-600" />}
          count={data.quality_gates.length}
          defaultOpen
        >
          <div className="space-y-3">
            {data.quality_gates.map((gate) => (
              <QualityGateCard key={gate.gate_id} gate={gate} />
            ))}
          </div>
        </Section>
      )}

      <div className="space-y-4">
        {lineageRecords.map((record) => (
          <LineageRecordCard key={record.lineage_id} record={record} />
        ))}
      </div>
    </div>
  );
}

function LineageRecordCard({ record }: { record: LineageRecord }) {
  const correctionFields = parseHumanCorrectionFields(record.human_correction_fields);

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm shadow-slate-200/60">
      <div className="border-b border-slate-200 px-5 py-4">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge
                status={(record.acceptance_decision as string) ?? "queued"}
                type="page"
              />
              <Badge variant="muted">
                {record.sub_page_index != null
                  ? `Sub-page ${record.sub_page_index}`
                  : "Whole page"}
              </Badge>
              <Badge variant="info">
                {record.material_type.replace(/_/g, " ")}
              </Badge>
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <code className="rounded bg-slate-100 px-2 py-1 font-mono">
                {record.lineage_id}
              </code>
              <CopyButton value={record.lineage_id} />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 lg:min-w-[280px]">
            <MetricCard
              label="Routing"
              value={record.routing_path ?? "—"}
            />
            <MetricCard
              label="Policy"
              value={record.policy_version}
            />
            <MetricCard
              label="Artifacts"
              value={`${record.preprocessed_artifact_state} / ${record.layout_artifact_state}`}
            />
            <MetricCard
              label="Total Time"
              value={formatDuration(record.total_processing_ms)}
            />
          </div>
        </div>
      </div>

      <div className="grid gap-4 px-5 py-5 lg:grid-cols-2">
        <ArtifactPreviewCard
          title="Original OTIFF"
          subtitle="Input artifact"
          uri={record.otiff_uri}
          fallbackText="No original artifact"
        />
        <ArtifactPreviewCard
          title="Output Artifact"
          subtitle="Best available output"
          uri={record.output_image_uri}
          fallbackText="No output artifact"
        />
      </div>

      <div className="grid gap-6 border-t border-slate-200 px-5 py-5 lg:grid-cols-[1.2fr_1fr]">
        <div>
          <h3 className="text-sm font-semibold text-slate-900 mb-3">
            Record Details
          </h3>
          <div className="space-y-2.5">
            <DetailRow label="Created" value={formatDate(record.created_at)} />
            <DetailRow
              label="Completed"
              value={formatDate(record.completed_at)}
            />
            <DetailRow
              label="Accepted As"
              value={record.acceptance_decision ?? "—"}
            />
            <DetailRow
              label="Reason"
              value={record.acceptance_reason ?? "—"}
            />
            <DetailRow
              label="Geometry Model"
              value={record.selected_geometry_model ?? "—"}
            />
            <DetailRow
              label="Agreement"
              value={
                record.structural_agreement == null
                  ? "—"
                  : record.structural_agreement
                  ? "Yes"
                  : "No"
              }
            />
            <DetailRow
              label="Reviewer"
              value={record.reviewed_by ?? "—"}
            />
            <DetailRow
              label="Reviewed At"
              value={formatDate(record.reviewed_at)}
            />
          </div>
        </div>

        <div>
          <h3 className="text-sm font-semibold text-slate-900 mb-3">
            Human Review
          </h3>
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
            <div className="flex items-center gap-2 mb-3">
              <User className="h-4 w-4 text-amber-600" />
              <span className="text-xs font-medium text-slate-700">
                {record.human_corrected ? "Human correction applied" : "No human correction"}
              </span>
            </div>

            <div className="space-y-2">
              <DetailRow
                label="Timestamp"
                value={formatDate(record.human_correction_timestamp)}
              />
              <DetailRow
                label="Crop Box"
                value={
                  correctionFields?.crop_box
                    ? `[${correctionFields.crop_box.join(", ")}]`
                    : "—"
                }
              />
              <DetailRow
                label="Deskew"
                value={
                  correctionFields?.deskew_angle != null
                    ? String(correctionFields.deskew_angle)
                    : "—"
                }
              />
              <DetailRow
                label="Split X"
                value={
                  correctionFields?.split_x != null
                    ? String(correctionFields.split_x)
                    : "—"
                }
              />
              <DetailRow
                label="Notes"
                value={record.reviewer_notes ?? "—"}
              />
            </div>
          </div>
        </div>
      </div>

      {record.service_invocations.length > 0 && (
        <div className="border-t border-slate-200 px-5 py-5">
          <h3 className="text-sm font-semibold text-slate-900 mb-3">
            Service Invocations
          </h3>
          <div className="space-y-3">
            {record.service_invocations.map((invocation) => (
              <InvocationCard
                key={`${record.lineage_id}-${invocation.id}`}
                invocation={invocation}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  icon,
  count,
  children,
  defaultOpen = false,
}: {
  title: string;
  icon: React.ReactNode;
  count: number;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm shadow-slate-200/60">
      <button
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between px-5 py-4 text-left transition-colors hover:bg-slate-50"
      >
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-50">
            {icon}
          </span>
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-slate-900">
              {title}
            </span>
            <span className="rounded-md bg-slate-100 px-2 py-0.5 text-2xs font-semibold text-slate-500">
              {count}
            </span>
          </div>
        </div>
        {open ? (
          <ChevronDown className="h-4 w-4 text-slate-400" />
        ) : (
          <ChevronRight className="h-4 w-4 text-slate-400" />
        )}
      </button>
      {open && <div className="border-t border-slate-200 px-5 py-4">{children}</div>}
    </div>
  );
}

function SummaryStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "blue" | "violet" | "amber";
}) {
  const toneClass =
    tone === "blue"
      ? "bg-blue-50 border-blue-200 text-blue-600"
      : tone === "violet"
      ? "bg-violet-50 border-violet-200 text-violet-600"
      : "bg-amber-50 border-amber-200 text-amber-600";

  return (
    <div
      className={cn(
        "rounded-xl border p-4 text-center shadow-sm",
        toneClass
      )}
    >
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
      <p className="mt-1 text-2xs text-slate-500">{label}</p>
    </div>
  );
}

function ArtifactPreviewCard({
  title,
  subtitle,
  uri,
  fallbackText,
}: {
  title: string;
  subtitle: string;
  uri: string | null;
  fallbackText: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div>
          <p className="text-sm font-semibold text-slate-900">{title}</p>
          <p className="text-xs text-slate-500">{subtitle}</p>
        </div>
        <div className="flex items-center gap-2">
          <ArtifactLinkButton uri={uri} label="Open" size="xs" variant="outline" />
          <ArtifactLinkButton
            uri={uri}
            label="Download"
            mode="download"
            size="xs"
            variant="ghost"
          />
        </div>
      </div>

      <ArtifactImage
        uri={uri}
        containerClassName="h-52 rounded-xl border border-slate-200 bg-white"
        className="object-contain"
        fallbackText={fallbackText}
      />
    </div>
  );
}

function QualityGateCard({ gate }: { gate: LineageQualityGate }) {
  const passed = gate.review_reason == null;

  return (
    <div
      className={cn(
        "rounded-xl border p-4",
        passed ? "border-emerald-200 bg-emerald-50/60" : "border-orange-200 bg-orange-50/60"
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {passed ? (
            <CheckCircle className="h-4 w-4 text-emerald-600" />
          ) : (
            <XCircle className="h-4 w-4 text-orange-600" />
          )}
          <span className="text-sm font-semibold text-slate-900">
            {snakeToTitle(gate.gate_type)}
          </span>
          <Badge variant={passed ? "success" : "warning"}>
            {snakeToTitle(gate.route_decision)}
          </Badge>
        </div>
        <span className="text-xs text-slate-500">
          {formatDate(gate.created_at)}
        </span>
      </div>

      <div className="grid gap-3 mt-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Structural Agreement"
          value={
            gate.structural_agreement == null
              ? "—"
              : gate.structural_agreement
              ? "Yes"
              : "No"
          }
        />
        <MetricCard
          label="Selected Model"
          value={gate.selected_model ?? "—"}
        />
        <MetricCard
          label="Validation Score"
          value={
            gate.artifact_validation_score != null
              ? formatScore(gate.artifact_validation_score)
              : "—"
          }
        />
        <MetricCard
          label="Processing Time"
          value={formatDuration(gate.processing_time_ms)}
        />
      </div>

      {gate.review_reason && (
        <p className="mt-3 text-xs text-orange-700">
          Review reason: {snakeToTitle(gate.review_reason)}
        </p>
      )}
      {gate.selection_reason && (
        <p className="mt-1 text-xs text-slate-500">
          Selection reason: {gate.selection_reason}
        </p>
      )}
    </div>
  );
}

function InvocationCard({
  invocation,
}: {
  invocation: LineageServiceInvocation;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-white border border-slate-200">
            <Layers className="h-4 w-4 text-indigo-600" />
          </span>
          <div>
            <p className="text-sm font-semibold text-slate-900">
              {invocation.service_name}
            </p>
            <p className="text-xs text-slate-500">
              Invoked {formatDate(invocation.invoked_at)}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Badge
            variant={
              invocation.status === "completed" || invocation.status === "success"
                ? "success"
                : invocation.status === "failed"
                ? "danger"
                : "muted"
            }
          >
            {snakeToTitle(invocation.status)}
          </Badge>
          <span className="text-xs text-slate-500">
            {formatDuration(invocation.processing_time_ms)}
          </span>
        </div>
      </div>

      <div className="grid gap-3 mt-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Service Version"
          value={invocation.service_version ?? "—"}
        />
        <MetricCard
          label="Model Version"
          value={invocation.model_version ?? "—"}
        />
        <MetricCard
          label="Model Source"
          value={invocation.model_source ?? "—"}
        />
        <MetricCard
          label="Completed"
          value={formatDate(invocation.completed_at)}
        />
      </div>

      {invocation.error_message && (
        <p className="mt-3 text-xs text-red-600">
          Error: {invocation.error_message}
        </p>
      )}
    </div>
  );
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5">
      <p className="text-2xs text-slate-400 uppercase tracking-wider">{label}</p>
      <p className="mt-1 text-xs text-slate-700 break-words">{value}</p>
    </div>
  );
}

function DetailRow({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="w-28 shrink-0 pt-0.5 text-2xs font-semibold uppercase tracking-wider text-slate-400">
        {label}
      </span>
      <span className="min-w-0 flex-1 break-all text-xs leading-5 text-slate-600">
        {value}
      </span>
    </div>
  );
}

function CopyButton({ value }: { value: string }) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="h-7 w-7 shrink-0"
      onClick={() => void navigator.clipboard.writeText(value)}
      aria-label="Copy"
    >
      <Copy className="h-3.5 w-3.5" />
    </Button>
  );
}

function parseHumanCorrectionFields(value: unknown): {
  crop_box?: number[];
  deskew_angle?: number | null;
  split_x?: number | null;
} | null {
  if (!value || typeof value !== "object") return null;

  const record = value as Record<string, unknown>;
  return {
    crop_box: Array.isArray(record.crop_box)
      ? record.crop_box
          .map((entry) => Number(entry))
          .filter((entry) => !Number.isNaN(entry))
      : undefined,
    deskew_angle:
      typeof record.deskew_angle === "number"
        ? record.deskew_angle
        : record.deskew_angle == null
        ? null
        : undefined,
    split_x:
      typeof record.split_x === "number"
        ? record.split_x
        : record.split_x == null
        ? null
        : undefined,
  };
}
