"use client";

import { useMemo, useState, type SyntheticEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Copy,
  LayoutGrid,
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

  const upstreamGeometryInvocations = useMemo(() => {
    if (!data || subPageIndex == null) return [];
    return data.lineage
      .filter((record) => record.sub_page_index == null)
      .flatMap((record) => record.service_invocations)
      .filter(
        (invocation) =>
          invocation.service_name === "iep1a" || invocation.service_name === "iep1b"
      );
  }, [data, subPageIndex]);

  const directInvocationCount = useMemo(
    () =>
      lineageRecords.reduce(
        (count, record) => count + record.service_invocations.length,
        0
      ),
    [lineageRecords]
  );

  const upstreamInvocationCount = upstreamGeometryInvocations.length;
  const showThisRowInvocationStat =
    upstreamInvocationCount === 0 || directInvocationCount > 0;

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

            <div
              className={cn(
                "grid min-w-[280px] gap-3",
                upstreamInvocationCount > 0 && directInvocationCount > 0
                  ? "grid-cols-2 sm:grid-cols-4"
                  : "grid-cols-3"
              )}
            >
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
              {showThisRowInvocationStat ? (
                <SummaryStat
                  label={
                    upstreamInvocationCount > 0
                      ? "Invocations (this row)"
                      : "Service Invocations"
                  }
                  value={directInvocationCount}
                  tone="amber"
                />
              ) : null}
              {upstreamInvocationCount > 0 ? (
                <SummaryStat
                  label="Parent Geometry"
                  value={upstreamInvocationCount}
                  tone="amber"
                  subtitle="iep1a / iep1b on parent lineage"
                />
              ) : null}
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

      {upstreamGeometryInvocations.length > 0 && (
        <Section
          title="Upstream Geometry Invocations"
          icon={<Layers className="h-4 w-4 text-indigo-600" />}
          count={upstreamGeometryInvocations.length}
          defaultOpen
        >
          <p className="mb-3 text-xs text-slate-500">
            This sub-page comes from a split parent. Geometry calls are logged on the parent
            lineage row.
          </p>
          <div className="space-y-3">
            {upstreamGeometryInvocations.map((invocation) => (
              <InvocationCard
                key={`upstream-${invocation.lineage_id}-${invocation.id}`}
                invocation={invocation}
              />
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

function getLayoutAdjudication(
  gateResults: unknown
): Record<string, unknown> | null {
  if (!gateResults || typeof gateResults !== "object") return null;
  const la = (gateResults as Record<string, unknown>).layout_adjudication;
  if (!la || typeof la !== "object") return null;
  return la as Record<string, unknown>;
}

function layoutDecisionSourceLabel(source: unknown): string | null {
  if (typeof source !== "string" || !source) return null;
  switch (source) {
    case "google_document_ai":
      return "Google Document AI";
    case "local_agreement":
      return "IEP2A & IEP2B (local agreement; IEP2A regions used)";
    case "local_fallback_unverified":
      return "Unverified local fallback (after Google hard-failure or skip)";
    case "none":
      return null;
    default:
      return snakeToTitle(source);
  }
}

function layoutDetectorRoleCaption(
  decision: unknown,
  detector: "iep2a" | "iep2b"
): string | undefined {
  if (typeof decision !== "string") return undefined;
  if (decision === "google_document_ai") {
    return detector === "iep2a" || detector === "iep2b"
      ? "Local detector run before Google adjudication (not the selected final layout)"
      : undefined;
  }
  if (decision === "local_agreement") {
    return detector === "iep2a"
      ? "Selected as canonical layout (local agreement path)"
      : "Consensus partner (matched IEP2A)";
  }
  if (decision === "local_fallback_unverified") {
    return "Candidate local result when Google could not be used reliably";
  }
  return undefined;
}

function formatHistogramSummary(hist: unknown): string | null {
  if (!hist || typeof hist !== "object") return null;
  const entries = Object.entries(hist as Record<string, unknown>).filter(
    ([, v]) => typeof v === "number" && !Number.isNaN(v as number)
  ) as [string, number][];
  if (entries.length === 0) return null;
  return entries.map(([k, v]) => `${k}: ${v}`).join(", ");
}

function summarizeLayoutDetectResponse(
  payload: Record<string, unknown> | null | undefined
): {
  detector?: string;
  modelVersion?: string;
  processingMs?: number;
  regionCount?: number;
  meanConf?: number;
  lowConfFrac?: number;
  histogram?: string | null;
  warnings?: string[];
} | null {
  if (!payload || typeof payload !== "object") return null;
  const regions = payload.regions;
  const regionCount = Array.isArray(regions) ? regions.length : undefined;
  const summary = payload.layout_conf_summary;
  let meanConf: number | undefined;
  let lowConfFrac: number | undefined;
  if (summary && typeof summary === "object") {
    const s = summary as Record<string, unknown>;
    if (typeof s.mean_conf === "number") meanConf = s.mean_conf;
    if (typeof s.low_conf_frac === "number") lowConfFrac = s.low_conf_frac;
  }
  const warnings = Array.isArray(payload.warnings)
    ? (payload.warnings as unknown[]).filter((w): w is string => typeof w === "string")
    : undefined;
  return {
    detector: typeof payload.detector_type === "string" ? payload.detector_type : undefined,
    modelVersion: typeof payload.model_version === "string" ? payload.model_version : undefined,
    processingMs:
      typeof payload.processing_time_ms === "number" ? payload.processing_time_ms : undefined,
    regionCount,
    meanConf,
    lowConfFrac,
    histogram: formatHistogramSummary(payload.region_type_histogram),
    warnings: warnings && warnings.length > 0 ? warnings : undefined,
  };
}

function layoutDetectorSummaryHasDisplayableFields(
  summary: NonNullable<ReturnType<typeof summarizeLayoutDetectResponse>>
): boolean {
  return (
    summary.detector != null ||
    (summary.modelVersion != null && summary.modelVersion !== "") ||
    summary.regionCount != null ||
    summary.meanConf != null ||
    summary.lowConfFrac != null ||
    (summary.histogram != null && summary.histogram !== "") ||
    (summary.warnings != null && summary.warnings.length > 0) ||
    summary.processingMs != null
  );
}

function LayoutDetectorEvidenceCard({
  title,
  decisionSource,
  detectorKey,
  raw,
}: {
  title: string;
  decisionSource: unknown;
  detectorKey: "iep2a" | "iep2b";
  raw: unknown;
}) {
  if (!raw || typeof raw !== "object") return null;
  const summary = summarizeLayoutDetectResponse(raw as Record<string, unknown>);
  if (!summary || !layoutDetectorSummaryHasDisplayableFields(summary)) return null;

  const caption = layoutDetectorRoleCaption(decisionSource, detectorKey);

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/80 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="text-sm font-semibold text-slate-900">{title}</p>
        {summary.processingMs != null ? (
          <span className="text-2xs text-slate-500">
            {formatDuration(summary.processingMs)}
          </span>
        ) : null}
      </div>
      {caption ? <p className="mt-1 text-2xs text-slate-500">{caption}</p> : null}
      <div className="mt-3 grid gap-2 md:grid-cols-2">
        {summary.detector ? (
          <MetricCard label="Detector" value={summary.detector} />
        ) : null}
        {summary.modelVersion ? (
          <MetricCard label="Model Version" value={summary.modelVersion} />
        ) : null}
        {summary.regionCount != null ? (
          <MetricCard label="Regions" value={String(summary.regionCount)} />
        ) : null}
        {summary.meanConf != null ? (
          <MetricCard label="Mean Confidence" value={formatScore(summary.meanConf)} />
        ) : null}
        {summary.lowConfFrac != null ? (
          <MetricCard label="Low-Conf Fraction" value={formatScore(summary.lowConfFrac)} />
        ) : null}
        {summary.histogram ? (
          <MetricCard label="Region Types" value={summary.histogram} />
        ) : null}
      </div>
      {summary.warnings?.length ? (
        <ul className="mt-2 list-disc pl-4 text-2xs text-amber-800 space-y-0.5">
          {summary.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function GoogleLayoutAuditCard({
  google,
  googleLatencyMs,
}: {
  google: Record<string, unknown> | null | undefined;
  googleLatencyMs: unknown;
}) {
  if (!google || typeof google !== "object") return null;

  const rows: { label: string; value: string }[] = [];
  const pick = (label: string, key: string) => {
    const v = google[key];
    if (v === undefined || v === null || v === "") return;
    if (typeof v === "boolean") {
      rows.push({ label, value: v ? "Yes" : "No" });
      return;
    }
    if (typeof v === "number" || typeof v === "string") {
      rows.push({ label, value: String(v) });
    }
  };

  pick("Attempted", "attempted");
  pick("Success", "success");
  pick("Region Count", "region_count");
  pick("Document Layout Blocks", "document_layout_block_count");
  pick("Blocks Have Geometry", "document_layout_blocks_have_geometry");
  pick("Page Width", "page_width");
  pick("Page Height", "page_height");
  pick("Text Length", "text_length");
  pick("Hard Failure", "hard_failure");
  pick("Empty Result", "empty_result");
  if (typeof google.empty_reason === "string" && google.empty_reason) {
    rows.push({ label: "Empty Reason", value: google.empty_reason });
  }
  if (typeof google.error === "string" && google.error) {
    rows.push({ label: "Error", value: google.error });
  }

  if (rows.length === 0 && googleLatencyMs == null) return null;

  return (
    <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="text-sm font-semibold text-slate-900">Google Document AI</p>
        {typeof googleLatencyMs === "number" ? (
          <span className="text-2xs text-slate-500">
            API {formatDuration(googleLatencyMs)}
          </span>
        ) : null}
      </div>
      <p className="mt-1 text-2xs text-slate-600">
        Metadata from the Google layout adjudication path (when invoked).
      </p>
      {rows.length > 0 ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {rows.map((r) => (
            <MetricCard key={r.label} label={r.label} value={r.value} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function LayoutEvidenceSection({
  adjudication,
}: {
  adjudication: Record<string, unknown> | null;
}) {
  if (!adjudication) return null;

  const decision = adjudication.layout_decision_source;
  const selectedLabel = layoutDecisionSourceLabel(decision);
  const iep2a = adjudication.iep2a_result;
  const iep2b = adjudication.iep2b_result;
  const google = adjudication.google_document_ai_result;
  const hasAny =
    (iep2a && typeof iep2a === "object") ||
    (iep2b && typeof iep2b === "object") ||
    (google && typeof google === "object") ||
    typeof adjudication.processing_time_ms === "number";

  if (!hasAny) return null;

  return (
    <div className="border-t border-slate-200 px-5 py-5">
      <div className="mb-3 flex items-center gap-2">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-50 border border-slate-200">
          <LayoutGrid className="h-4 w-4 text-teal-600" />
        </span>
        <h3 className="text-sm font-semibold text-slate-900">Layout Evidence</h3>
      </div>

      {selectedLabel ? (
        <p className="mb-3 rounded-lg border border-teal-200 bg-teal-50/70 px-3 py-2 text-xs text-teal-900">
          <span className="font-semibold">Selected layout source: </span>
          {selectedLabel}
        </p>
      ) : null}

      {typeof adjudication.processing_time_ms === "number" ? (
        <p className="mb-3 text-2xs text-slate-500">
          Adjudication wall time: {formatDuration(adjudication.processing_time_ms)}
        </p>
      ) : null}

      <div className="space-y-3">
        <LayoutDetectorEvidenceCard
          title="IEP2A"
          decisionSource={decision}
          detectorKey="iep2a"
          raw={iep2a}
        />
        <LayoutDetectorEvidenceCard
          title="IEP2B"
          decisionSource={decision}
          detectorKey="iep2b"
          raw={iep2b}
        />
        <GoogleLayoutAuditCard
          google={
            google && typeof google === "object" ? (google as Record<string, unknown>) : null
          }
          googleLatencyMs={adjudication.google_response_time_ms}
        />
      </div>
    </div>
  );
}

function HumanReviewPanel({
  record,
  correctionFields,
}: {
  record: LineageRecord;
  correctionFields: ReturnType<typeof parseHumanCorrectionFields>;
}) {
  const ts = record.human_correction_timestamp
    ? formatDate(record.human_correction_timestamp)
    : null;
  const crop =
    correctionFields?.crop_box && correctionFields.crop_box.length > 0
      ? `[${correctionFields.crop_box.join(", ")}]`
      : null;
  const quadStr = formatQuadPointsForDisplay(correctionFields?.quad_points);
  const selectionMode =
    typeof correctionFields?.selection_mode === "string"
      ? correctionFields.selection_mode
      : null;
  const deskew =
    correctionFields?.deskew_angle != null &&
    typeof correctionFields.deskew_angle === "number" &&
    Number.isFinite(correctionFields.deskew_angle)
      ? String(correctionFields.deskew_angle)
      : null;
  const splitX =
    correctionFields?.split_x != null &&
    typeof correctionFields.split_x === "number" &&
    Number.isFinite(correctionFields.split_x)
      ? String(correctionFields.split_x)
      : null;
  const notes =
    record.reviewer_notes != null && String(record.reviewer_notes).trim() !== ""
      ? String(record.reviewer_notes)
      : null;

  if (!record.human_corrected) {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
        <div className="flex items-center gap-2">
          <User className="h-4 w-4 text-slate-400" />
          <span className="text-xs text-slate-600">No human correction was applied.</span>
        </div>
      </div>
    );
  }

  const showReviewer = record.reviewed_by != null && record.reviewed_by !== "";
  const reviewedAt =
    record.reviewed_at != null ? formatDate(record.reviewed_at) : null;

  return (
    <div className="rounded-xl border border-amber-200/80 bg-amber-50/40 p-4">
      <div className="flex items-center gap-2 mb-3">
        <User className="h-4 w-4 text-amber-700" />
        <span className="text-xs font-medium text-slate-800">Human correction applied</span>
      </div>
      <div className="space-y-2">
        {ts ? <DetailRow label="Timestamp" value={ts} /> : null}
        {showReviewer ? <DetailRow label="Reviewer" value={record.reviewed_by!} /> : null}
        {reviewedAt ? <DetailRow label="Reviewed At" value={reviewedAt} /> : null}
        {selectionMode ? (
          <DetailRow label="Selection Mode" value={selectionMode} />
        ) : null}
        {crop ? <DetailRow label="Crop Box" value={crop} /> : null}
        {quadStr ? <DetailRow label="Quad Points" value={quadStr} /> : null}
        {deskew != null ? <DetailRow label="Deskew" value={deskew} /> : null}
        {splitX != null ? <DetailRow label="Split X" value={splitX} /> : null}
        {notes ? <DetailRow label="Notes" value={notes} /> : null}
      </div>
    </div>
  );
}

const GATE_DEBUG_MAX_ARRAY = 48;
const GATE_DEBUG_MAX_DEPTH = 18;

/**
 * Build a JSON-serializable copy of gate_results for the admin debug panel.
 * Omits large arrays (regions, final_layout_result, and any oversized arrays)
 * by reference count only — does not deep-clone huge lists.
 */
function deepSanitizeGateResultsForDebug(value: unknown, depth = 0): unknown {
  if (value === null || typeof value !== "object") return value;
  if (depth > GATE_DEBUG_MAX_DEPTH) return "[max depth]";
  if (Array.isArray(value)) {
    if (value.length > GATE_DEBUG_MAX_ARRAY) {
      return { _omitted_array: true, count: value.length };
    }
    return value.map((item) => deepSanitizeGateResultsForDebug(item, depth + 1));
  }

  const o = value as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) {
    if (Array.isArray(v)) {
      if (k === "regions" || k === "final_layout_result") {
        out[k] = { _omitted: true, count: v.length };
        continue;
      }
      if (v.length > GATE_DEBUG_MAX_ARRAY) {
        out[k] = { _omitted_array: true, count: v.length };
        continue;
      }
      out[k] = v.map((item) => deepSanitizeGateResultsForDebug(item, depth + 1));
      continue;
    }
    if (v !== null && typeof v === "object") {
      out[k] = deepSanitizeGateResultsForDebug(v, depth + 1);
      continue;
    }
    out[k] = v as unknown;
  }
  return out;
}

function GateResultsAdvancedDetails({ gateResults }: { gateResults: unknown }) {
  const [jsonText, setJsonText] = useState<string | null>(null);

  const onToggle = (event: SyntheticEvent<HTMLDetailsElement>) => {
    const el = event.currentTarget;
    if (!el.open || jsonText !== null) return;
    try {
      const sanitized = deepSanitizeGateResultsForDebug(gateResults);
      setJsonText(JSON.stringify(sanitized, null, 2));
    } catch {
      setJsonText("// gate_results could not be serialized (cyclic or non-JSON value)\n");
    }
  };

  return (
    <div className="border-t border-slate-200 px-5 py-4 bg-slate-50/80">
      <details className="group" onToggle={onToggle}>
        <summary className="cursor-pointer list-none text-xs font-medium text-slate-600 flex items-center gap-2">
          <ChevronRight className="h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-90" />
          Advanced metadata (gate_results JSON)
        </summary>
        {jsonText !== null ? (
          <pre className="mt-3 max-h-96 overflow-auto rounded-lg border border-slate-200 bg-white p-3 text-[10px] leading-relaxed text-slate-700">
            {jsonText}
          </pre>
        ) : null}
      </details>
    </div>
  );
}

function formatQuadPointsForDisplay(
  quad: number[][] | null | undefined
): string | null {
  if (!quad || !Array.isArray(quad) || quad.length === 0) return null;
  const parts = quad
    .map((pt) =>
      Array.isArray(pt) && pt.length >= 2
        ? `[${Number(pt[0]).toFixed(1)}, ${Number(pt[1]).toFixed(1)}]`
        : null
    )
    .filter(Boolean);
  if (parts.length === 0) return null;
  return parts.join(" · ");
}

function LineageRecordCard({ record }: { record: LineageRecord }) {
  const correctionFields = parseHumanCorrectionFields(record.human_correction_fields);
  const adjudication = getLayoutAdjudication(record.gate_results);
  const layoutSourceLabel = layoutDecisionSourceLabel(adjudication?.layout_decision_source);
  const hasGateResultsForDebug =
    record.gate_results != null &&
    typeof record.gate_results === "object" &&
    Object.keys(record.gate_results as object).length > 0;

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
            {record.routing_path ? (
              <MetricCard label="Routing" value={record.routing_path} />
            ) : null}
            <MetricCard label="Policy" value={record.policy_version} />
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

      <LayoutEvidenceSection adjudication={adjudication} />

      <div className="border-t border-slate-200 px-5 py-5">
        <h3 className="text-sm font-semibold text-slate-900 mb-3">Human Review</h3>
        <HumanReviewPanel record={record} correctionFields={correctionFields} />
      </div>

      <div className="grid gap-4 border-t border-slate-200 px-5 py-5 lg:grid-cols-2">
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

      <div className="border-t border-slate-200 px-5 py-5">
        <h3 className="text-sm font-semibold text-slate-900 mb-3">Record Details</h3>
        <div className="space-y-4">
          <div className="space-y-2.5">
            <p className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
              Timestamps
            </p>
            <DetailRow label="Created" value={formatDate(record.created_at)} />
            {record.completed_at ? (
              <DetailRow label="Completed" value={formatDate(record.completed_at)} />
            ) : null}
          </div>

          <div className="space-y-2.5">
            <p className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
              Acceptance
            </p>
            {record.acceptance_decision ? (
              <DetailRow label="Accepted As" value={record.acceptance_decision} />
            ) : null}
            {record.acceptance_reason ? (
              <DetailRow label="Reason" value={record.acceptance_reason} />
            ) : null}
            {layoutSourceLabel ? (
              <DetailRow label="Selected Layout Source" value={layoutSourceLabel} />
            ) : null}
          </div>

          {(record.selected_geometry_model != null && record.selected_geometry_model !== "") ||
          record.structural_agreement != null ? (
            <div className="space-y-2.5">
              <p className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
                Geometry
              </p>
              {record.selected_geometry_model ? (
                <DetailRow label="Selected Model" value={record.selected_geometry_model} />
              ) : null}
              {record.structural_agreement != null ? (
                <DetailRow
                  label="Structural Agreement"
                  value={record.structural_agreement ? "Yes" : "No"}
                />
              ) : null}
            </div>
          ) : null}

          {(record.reviewed_by != null && record.reviewed_by !== "") ||
          record.reviewed_at != null ? (
            <div className="space-y-2.5">
              <p className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
                Review Workflow
              </p>
              {record.reviewed_by ? (
                <DetailRow label="Reviewer" value={record.reviewed_by} />
              ) : null}
              {record.reviewed_at ? (
                <DetailRow label="Reviewed At" value={formatDate(record.reviewed_at)} />
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      {hasGateResultsForDebug ? (
        <GateResultsAdvancedDetails gateResults={record.gate_results} />
      ) : null}
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
  subtitle,
}: {
  label: string;
  value: number;
  tone: "blue" | "violet" | "amber";
  subtitle?: string;
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
      {subtitle ? (
        <p className="mt-1 text-[10px] leading-tight text-slate-400">{subtitle}</p>
      ) : null}
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

  const metrics: { label: string; value: string }[] = [];
  if (gate.structural_agreement != null) {
    metrics.push({
      label: "Structural Agreement",
      value: gate.structural_agreement ? "Yes" : "No",
    });
  }
  if (gate.selected_model != null && gate.selected_model !== "") {
    metrics.push({ label: "Selected Model", value: gate.selected_model });
  }
  if (gate.artifact_validation_score != null) {
    metrics.push({
      label: "Validation Score",
      value: formatScore(gate.artifact_validation_score),
    });
  }
  if (gate.processing_time_ms != null) {
    metrics.push({
      label: "Processing Time",
      value: formatDuration(gate.processing_time_ms),
    });
  }

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

      {metrics.length > 0 ? (
        <div
          className={cn(
            "grid gap-3 mt-3",
            metrics.length === 1
              ? "md:grid-cols-1"
              : metrics.length === 2
              ? "md:grid-cols-2"
              : metrics.length === 3
              ? "md:grid-cols-3"
              : "md:grid-cols-2 xl:grid-cols-4"
          )}
        >
          {metrics.map((m) => (
            <MetricCard key={m.label} label={m.label} value={m.value} />
          ))}
        </div>
      ) : null}

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
        {invocation.service_version ? (
          <MetricCard label="Service Version" value={invocation.service_version} />
        ) : null}
        {invocation.model_version ? (
          <MetricCard label="Model Version" value={invocation.model_version} />
        ) : null}
        {invocation.model_source ? (
          <MetricCard label="Model Source" value={invocation.model_source} />
        ) : null}
        {invocation.completed_at ? (
          <MetricCard label="Completed" value={formatDate(invocation.completed_at)} />
        ) : null}
      </div>

      {!invocation.service_version &&
      !invocation.model_version &&
      !invocation.model_source &&
      !invocation.completed_at ? (
        <p className="mt-2 text-2xs text-slate-500">
          No version or model metadata was recorded for this invocation.
        </p>
      ) : null}

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

function finiteNumberFromJson(value: unknown): number | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const t = value.trim();
    if (t === "") return undefined;
    const n = Number(t);
    if (Number.isFinite(n)) return n;
  }
  return undefined;
}

function parseHumanCorrectionFields(value: unknown): {
  crop_box?: number[];
  deskew_angle?: number;
  split_x?: number;
  quad_points?: number[][];
  selection_mode?: string;
} | null {
  if (!value || typeof value !== "object") return null;

  const record = value as Record<string, unknown>;

  let quad_points: number[][] | undefined;
  const rawQuad = record.quad_points;
  if (Array.isArray(rawQuad)) {
    const parsed: number[][] = [];
    for (const entry of rawQuad) {
      if (!Array.isArray(entry) || entry.length < 2) continue;
      const x = Number(entry[0]);
      const y = Number(entry[1]);
      if (Number.isNaN(x) || Number.isNaN(y)) continue;
      parsed.push([x, y]);
    }
    if (parsed.length > 0) quad_points = parsed;
  }

  const selection_mode =
    typeof record.selection_mode === "string" && record.selection_mode
      ? record.selection_mode
      : undefined;

  const deskew_angle = finiteNumberFromJson(record.deskew_angle);
  const split_x = finiteNumberFromJson(record.split_x);

  return {
    crop_box: Array.isArray(record.crop_box)
      ? record.crop_box
          .map((entry) => Number(entry))
          .filter((entry) => !Number.isNaN(entry))
      : undefined,
    ...(deskew_angle !== undefined ? { deskew_angle } : {}),
    ...(split_x !== undefined ? { split_x } : {}),
    quad_points,
    selection_mode,
  };
}
