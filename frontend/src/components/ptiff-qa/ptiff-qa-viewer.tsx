"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import {
  ChevronLeft,
  ChevronRight,
  Flag,
  CheckCircle,
  Loader2,
  AlertTriangle,
  Eye,
} from "lucide-react";
import { getViewerPage, flagPageForCorrection, approvePtiffQaPage } from "@/lib/api/ptiff-qa";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { ErrorBanner } from "@/components/shared/error-banner";
import { StatusBadge } from "@/components/shared/status-badge";
import { ArtifactImage } from "@/components/shared/artifact-image";
import { getApiErrorMessage, isApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { ViewerPageRef } from "@/types/api";

interface PtiffQaViewerProps {
  jobId: string;
}

export default function PtiffQaViewer({ jobId }: PtiffQaViewerProps) {
  const queryClient = useQueryClient();

  // Navigation cursor: null = first page (server decides)
  const [cursor, setCursor] = useState<{ pageNumber: number; subPageIndex: number | null } | null>(
    null
  );

  const queryKey = ["ptiff-qa-viewer", jobId, cursor?.pageNumber ?? null, cursor?.subPageIndex ?? null];

  const { data, isLoading, isError, isFetching } = useQuery({
    queryKey,
    queryFn: () => getViewerPage(jobId, cursor?.pageNumber, cursor?.subPageIndex),
    staleTime: 60_000, // presigned URLs are valid for 300s; refresh well before expiry
    refetchInterval: false,
  });

  // Flag: send to pending_human_correction
  const flagMut = useMutation({
    mutationFn: () =>
      flagPageForCorrection(
        jobId,
        data!.current_page.page_number,
        data!.current_page.sub_page_index
      ),
    onSuccess: (res) => {
      toast.success(
        `Page ${res.page_number} flagged for human correction.`
      );
      // Refresh viewer (page status changed) and job detail
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa-viewer", jobId] });
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("Page cannot be flagged in its current state.");
      else if (status === 404) toast.error("Page not found.");
      else toast.error(getApiErrorMessage(err, "Failed to flag page."));
    },
  });

  // Approve: mark ptiff_qa_pending page as approved
  const approveMut = useMutation({
    mutationFn: () =>
      approvePtiffQaPage(
        jobId,
        data!.current_page.page_number,
        data!.current_page.sub_page_index ?? undefined
      ),
    onSuccess: (res) => {
      const msg = res.gate_released
        ? `Page ${res.page_number} approved. Gate released!`
        : `Page ${res.page_number} approved.`;
      toast.success(msg);
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa-viewer", jobId] });
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("Page is not in ptiff_qa_pending state.");
      else toast.error(getApiErrorMessage(err, "Failed to approve page."));
    },
  });

  function navigateTo(ref: ViewerPageRef) {
    setCursor({ pageNumber: ref.page_number, subPageIndex: ref.sub_page_index });
  }

  // ── Loading state ────────────────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner size="lg" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="p-4">
        <ErrorBanner
          variant="inline"
          title="Could not load viewer"
          message="The PTIFF QA viewer failed to load. Check that the job has processed pages."
        />
      </div>
    );
  }

  const { job_summary, current_page, navigation } = data;
  const isBusy = flagMut.isPending || approveMut.isPending || isFetching;

  // ── Render ───────────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-4">
      {/* ── Job progress bar ──────────────────────────────────────────────── */}
      <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <Eye className="h-4 w-4 text-slate-400" />
            <span className="text-sm font-medium text-slate-700">PTIFF QA Progress</span>
            <Badge variant={job_summary.is_gate_ready ? "success" : "muted"} className="text-2xs">
              {job_summary.is_gate_ready ? "Gate ready" : "Pending"}
            </Badge>
          </div>
          <span className="text-xs text-slate-400 tabular-nums">
            {navigation.current_index + 1} / {navigation.total_pages}
          </span>
        </div>

        <div className="h-2 bg-slate-200 rounded-full overflow-hidden flex">
          <div
            className="bg-emerald-500 transition-all"
            style={{
              width: `${(job_summary.pages_accepted / Math.max(job_summary.total_pages, 1)) * 100}%`,
            }}
          />
          <div
            className="bg-indigo-500 transition-all"
            style={{
              width: `${(job_summary.pages_approved / Math.max(job_summary.total_pages, 1)) * 100}%`,
            }}
          />
          <div
            className="bg-orange-400 transition-all"
            style={{
              width: `${(job_summary.pages_in_correction / Math.max(job_summary.total_pages, 1)) * 100}%`,
            }}
          />
          <div
            className="bg-red-400 transition-all"
            style={{
              width: `${(job_summary.pages_failed / Math.max(job_summary.total_pages, 1)) * 100}%`,
            }}
          />
        </div>

        <div className="flex items-center gap-4 mt-2 flex-wrap">
          <LegendDot color="bg-emerald-500" label={`${job_summary.pages_accepted} accepted`} />
          <LegendDot color="bg-indigo-500" label={`${job_summary.pages_approved} QA approved`} />
          {job_summary.pages_pending_qa > 0 && (
            <LegendDot color="bg-slate-300" label={`${job_summary.pages_pending_qa} pending QA`} />
          )}
          {job_summary.pages_in_correction > 0 && (
            <LegendDot color="bg-orange-400" label={`${job_summary.pages_in_correction} correction`} />
          )}
          {job_summary.pages_failed > 0 && (
            <LegendDot color="bg-red-400" label={`${job_summary.pages_failed} failed`} />
          )}
        </div>
      </div>

      {/* ── Carousel card ─────────────────────────────────────────────────── */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        {/* Page header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-slate-800 tabular-nums">
              Page {current_page.page_number}
              {current_page.sub_page_index != null && (
                <span className="text-slate-400 font-normal">
                  /{current_page.sub_page_index}
                </span>
              )}
            </span>
            <StatusBadge status={current_page.status as never} type="page" />
            {current_page.ptiff_qa_approved && (
              <Badge variant="success" className="text-2xs gap-1">
                <CheckCircle className="h-3 w-3" />
                QA Approved
              </Badge>
            )}
          </div>

          {/* Action buttons */}
          <div className="flex items-center gap-2">
            {current_page.can_approve && (
              <Button
                size="sm"
                variant="outline"
                className="gap-1.5 border-emerald-300 text-emerald-700 hover:bg-emerald-50"
                onClick={() => approveMut.mutate()}
                disabled={isBusy}
              >
                {approveMut.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <CheckCircle className="h-3.5 w-3.5" />
                )}
                Approve
              </Button>
            )}
            {current_page.can_send_to_correction && (
              <Button
                size="sm"
                variant="outline"
                className="gap-1.5 border-orange-300 text-orange-700 hover:bg-orange-50"
                onClick={() => flagMut.mutate()}
                disabled={isBusy}
              >
                {flagMut.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Flag className="h-3.5 w-3.5" />
                )}
                Send to Correction
              </Button>
            )}
          </div>
        </div>

        {/* Image + navigation */}
        <div className="relative">
          {/* Prev arrow */}
          <button
            className={cn(
              "absolute left-3 top-1/2 -translate-y-1/2 z-10",
              "h-10 w-10 rounded-full bg-white/80 backdrop-blur border border-slate-200 shadow",
              "flex items-center justify-center",
              "hover:bg-white transition-colors",
              !navigation.prev && "opacity-30 cursor-not-allowed"
            )}
            onClick={() => navigation.prev && navigateTo(navigation.prev)}
            disabled={!navigation.prev || isBusy}
            aria-label="Previous page"
          >
            <ChevronLeft className="h-5 w-5 text-slate-700" />
          </button>

          {/* Image area */}
          <div className="flex items-center justify-center min-h-[480px] bg-slate-100 px-16 py-6">
            {isFetching ? (
              <div className="flex flex-col items-center gap-3 text-slate-400">
                <Loader2 className="h-8 w-8 animate-spin" />
                <span className="text-sm">Loading image…</span>
              </div>
            ) : (
              <ArtifactImage
                uri={current_page.output_image_uri ?? current_page.input_image_uri}
                alt={`Page ${current_page.page_number} PTIFF preview`}
                containerClassName="h-[520px] w-full max-w-xl rounded shadow-sm border border-slate-200 bg-white"
                className="object-contain"
                fallbackText={current_page.preview_unavailable_reason ?? "No image available yet."}
              />
            )}
          </div>

          {/* Next arrow */}
          <button
            className={cn(
              "absolute right-3 top-1/2 -translate-y-1/2 z-10",
              "h-10 w-10 rounded-full bg-white/80 backdrop-blur border border-slate-200 shadow",
              "flex items-center justify-center",
              "hover:bg-white transition-colors",
              !navigation.next && "opacity-30 cursor-not-allowed"
            )}
            onClick={() => navigation.next && navigateTo(navigation.next)}
            disabled={!navigation.next || isBusy}
            aria-label="Next page"
          >
            <ChevronRight className="h-5 w-5 text-slate-700" />
          </button>
        </div>

        {/* Quality metrics footer */}
        {current_page.quality_summary && (
          <div className="border-t border-slate-100 px-5 py-3 flex items-center gap-6 flex-wrap">
            <span className="text-2xs text-slate-400 uppercase tracking-wider">Quality</span>
            <QualityMetric
              label="Blur"
              value={current_page.quality_summary.blur_score}
            />
            <QualityMetric
              label="Skew"
              value={current_page.quality_summary.skew_angle_deg}
              unit="°"
            />
            <QualityMetric
              label="Border"
              value={current_page.quality_summary.border_fraction}
              asPercent
            />
            <QualityMetric
              label="Coverage"
              value={current_page.quality_summary.coverage_fraction}
              asPercent
            />
            {current_page.quality_summary.overall_passed != null && (
              <div className="flex items-center gap-1">
                {current_page.quality_summary.overall_passed ? (
                  <CheckCircle className="h-3.5 w-3.5 text-emerald-500" />
                ) : (
                  <AlertTriangle className="h-3.5 w-3.5 text-yellow-500" />
                )}
                <span className="text-xs text-slate-600">
                  {current_page.quality_summary.overall_passed ? "Passed" : "Failed"}
                </span>
              </div>
            )}
            {current_page.processing_time_ms != null && (
              <span className="text-2xs text-slate-400 ml-auto">
                {(current_page.processing_time_ms / 1000).toFixed(1)}s
              </span>
            )}
          </div>
        )}

        {/* Review reasons */}
        {current_page.review_reasons && current_page.review_reasons.length > 0 && (
          <div className="border-t border-slate-100 px-5 py-2 flex items-center gap-2 flex-wrap">
            <AlertTriangle className="h-3.5 w-3.5 text-yellow-500 shrink-0" />
            {current_page.review_reasons.map((r) => (
              <span
                key={r}
                className="text-2xs bg-yellow-50 text-yellow-700 border border-yellow-200 rounded px-1.5 py-0.5"
              >
                {r}
              </span>
            ))}
          </div>
        )}

        {/* Navigation footer */}
        <div className="border-t border-slate-100 px-5 py-2.5 flex items-center justify-between">
          <button
            className="text-xs text-indigo-600 hover:text-indigo-800 disabled:text-slate-300 flex items-center gap-1"
            onClick={() => navigation.prev && navigateTo(navigation.prev)}
            disabled={!navigation.prev || isBusy}
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            {navigation.prev
              ? `Page ${navigation.prev.page_number}${navigation.prev.sub_page_index != null ? `/${navigation.prev.sub_page_index}` : ""}`
              : "No previous"}
          </button>

          <span className="text-2xs text-slate-400 tabular-nums">
            {navigation.current_index + 1} of {navigation.total_pages}
          </span>

          <button
            className="text-xs text-indigo-600 hover:text-indigo-800 disabled:text-slate-300 flex items-center gap-1"
            onClick={() => navigation.next && navigateTo(navigation.next)}
            disabled={!navigation.next || isBusy}
          >
            {navigation.next
              ? `Page ${navigation.next.page_number}${navigation.next.sub_page_index != null ? `/${navigation.next.sub_page_index}` : ""}`
              : "No next"}
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={cn("h-2 w-2 rounded-full", color)} />
      <span className="text-2xs text-slate-500">{label}</span>
    </div>
  );
}

function QualityMetric({
  label,
  value,
  unit,
  asPercent,
}: {
  label: string;
  value: number | null | undefined;
  unit?: string;
  asPercent?: boolean;
}) {
  if (value == null) return null;
  const display = asPercent
    ? `${(value * 100).toFixed(0)}%`
    : unit
    ? `${value.toFixed(2)}${unit}`
    : value.toFixed(3);

  return (
    <div className="flex items-center gap-1">
      <span className="text-2xs text-slate-400">{label}:</span>
      <span className="text-xs text-slate-700 font-medium tabular-nums">{display}</span>
    </div>
  );
}
