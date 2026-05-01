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
import { isApiError } from "@/lib/api/client";
import { cn, reviewReasonLabel } from "@/lib/utils";
import type { ViewerPageRef } from "@/types/api";

interface PtiffQaViewerProps {
  jobId: string;
}

export default function PtiffQaViewer({ jobId }: PtiffQaViewerProps) {
  const queryClient = useQueryClient();
  const [cursor, setCursor] = useState<{ pageNumber: number; subPageIndex: number | null } | null>(
    null
  );

  const queryKey = ["ptiff-qa-viewer", jobId, cursor?.pageNumber ?? null, cursor?.subPageIndex ?? null];

  const { data, isLoading, isError, isFetching } = useQuery({
    queryKey,
    queryFn: () => getViewerPage(jobId, cursor?.pageNumber, cursor?.subPageIndex),
    staleTime: 60_000,
    refetchInterval: false,
  });

  const flagMut = useMutation({
    mutationFn: () =>
      flagPageForCorrection(
        jobId,
        data!.current_page.page_number,
        data!.current_page.sub_page_index
      ),
    onSuccess: (res) => {
      toast.success(`Page ${res.page_number} sent for review.`);
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa-viewer", jobId] });
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("This page cannot be sent for review right now.");
      else if (status === 404) toast.error("Page not found.");
      else toast.error("We could not send this page for review.");
    },
  });

  const approveMut = useMutation({
    mutationFn: () =>
      approvePtiffQaPage(
        jobId,
        data!.current_page.page_number,
        data!.current_page.sub_page_index ?? undefined
      ),
    onSuccess: (res) => {
      toast.success(
        `Page ${res.page_number} approved.${res.gate_released ? " Processing can continue." : ""}`
      );
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa-viewer", jobId] });
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("This page is no longer waiting for review.");
      else toast.error("We could not approve this page.");
    },
  });

  function navigateTo(ref: ViewerPageRef) {
    setCursor({ pageNumber: ref.page_number, subPageIndex: ref.sub_page_index });
  }

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
          title="Could not load page viewer"
          message="There was a problem loading these pages. Please try again."
        />
      </div>
    );
  }

  const { job_summary, current_page, navigation } = data;
  const isBusy = flagMut.isPending || approveMut.isPending || isFetching;

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm shadow-slate-200/70">
        <div className="mb-2 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Eye className="h-4 w-4 text-slate-400" />
            <span className="text-sm font-medium text-slate-700">Review progress</span>
            <Badge variant={job_summary.is_gate_ready ? "success" : "muted"} className="text-2xs">
              {job_summary.is_gate_ready ? "Ready" : "Reviewing"}
            </Badge>
          </div>
          <span className="text-xs tabular-nums text-slate-400">
            {navigation.current_index + 1} / {navigation.total_pages}
          </span>
        </div>

        <div className="flex h-2 overflow-hidden rounded-full bg-slate-200">
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

        <div className="mt-2 flex flex-wrap items-center gap-4">
          <LegendDot color="bg-emerald-500" label={`${job_summary.pages_accepted} ready`} />
          <LegendDot color="bg-indigo-500" label={`${job_summary.pages_approved} approved`} />
          {job_summary.pages_pending_qa > 0 && (
            <LegendDot color="bg-slate-300" label={`${job_summary.pages_pending_qa} to review`} />
          )}
          {job_summary.pages_in_correction > 0 && (
            <LegendDot color="bg-orange-400" label={`${job_summary.pages_in_correction} need review`} />
          )}
          {job_summary.pages_failed > 0 && (
            <LegendDot color="bg-red-400" label={`${job_summary.pages_failed} issue${job_summary.pages_failed !== 1 ? "s" : ""}`} />
          )}
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm shadow-slate-200/70">
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold tabular-nums text-slate-800">
              Page {current_page.page_number}
              {current_page.sub_page_index != null && (
                <span className="font-normal text-slate-400">
                  {" "}
                  {current_page.sub_page_index === 0 ? "Left" : "Right"}
                </span>
              )}
            </span>
            <StatusBadge status={current_page.status as never} type="page" />
            {current_page.ptiff_qa_approved && (
              <Badge variant="success" className="gap-1 text-2xs">
                <CheckCircle className="h-3 w-3" />
                Approved
              </Badge>
            )}
          </div>

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
                Needs review
              </Button>
            )}
          </div>
        </div>

        <div className="relative">
          <button
            className={cn(
              "absolute left-3 top-1/2 z-10 h-10 w-10 -translate-y-1/2 rounded-full border border-slate-200 bg-white/80 shadow backdrop-blur",
              "flex items-center justify-center transition-colors hover:bg-white",
              !navigation.prev && "cursor-not-allowed opacity-30"
            )}
            onClick={() => navigation.prev && navigateTo(navigation.prev)}
            disabled={!navigation.prev || isBusy}
            aria-label="Previous page"
          >
            <ChevronLeft className="h-5 w-5 text-slate-700" />
          </button>

          <div className="relative flex min-h-[480px] items-center justify-center bg-slate-100 px-16 py-6">
            <ArtifactImage
              uri={current_page.output_image_uri}
              fallbackUri={current_page.input_image_uri}
              alt={`Page ${current_page.page_number} preview`}
              containerClassName="h-[520px] w-full max-w-xl rounded shadow-sm border border-slate-200 bg-white"
              className="object-contain"
              fallbackText="No image available yet."
              maxWidth={1600}
            />

            {isFetching && (
              <div className="absolute inset-0 flex items-center justify-center bg-slate-100/55 backdrop-blur-[1px]">
                <div className="flex flex-col items-center gap-3 text-slate-500">
                  <Loader2 className="h-8 w-8 animate-spin" />
                  <span className="text-sm">Updating page...</span>
                </div>
              </div>
            )}
          </div>

          <button
            className={cn(
              "absolute right-3 top-1/2 z-10 h-10 w-10 -translate-y-1/2 rounded-full border border-slate-200 bg-white/80 shadow backdrop-blur",
              "flex items-center justify-center transition-colors hover:bg-white",
              !navigation.next && "cursor-not-allowed opacity-30"
            )}
            onClick={() => navigation.next && navigateTo(navigation.next)}
            disabled={!navigation.next || isBusy}
            aria-label="Next page"
          >
            <ChevronRight className="h-5 w-5 text-slate-700" />
          </button>
        </div>

        {current_page.quality_summary?.overall_passed != null && (
          <div className="flex items-center gap-2 border-t border-slate-100 px-5 py-3">
            {current_page.quality_summary.overall_passed ? (
              <CheckCircle className="h-3.5 w-3.5 text-emerald-500" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 text-yellow-500" />
            )}
            <span className="text-xs text-slate-600">
              {current_page.quality_summary.overall_passed
                ? "This page looks ready."
                : "This page may need review before it is ready."}
            </span>
          </div>
        )}

        {current_page.review_reasons && current_page.review_reasons.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 px-5 py-2">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-yellow-500" />
            {current_page.review_reasons.map((reason) => (
              <span
                key={reason}
                className="rounded-full border border-yellow-200 bg-yellow-50 px-2 py-0.5 text-2xs text-yellow-700"
              >
                {reviewReasonLabel(reason)}
              </span>
            ))}
          </div>
        )}

        <div className="flex items-center justify-between border-t border-slate-100 px-5 py-2.5">
          <button
            className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 disabled:text-slate-300"
            onClick={() => navigation.prev && navigateTo(navigation.prev)}
            disabled={!navigation.prev || isBusy}
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            {navigation.prev ? pageRefLabel(navigation.prev) : "No previous"}
          </button>

          <span className="text-2xs tabular-nums text-slate-400">
            {navigation.current_index + 1} of {navigation.total_pages}
          </span>

          <button
            className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 disabled:text-slate-300"
            onClick={() => navigation.next && navigateTo(navigation.next)}
            disabled={!navigation.next || isBusy}
          >
            {navigation.next ? pageRefLabel(navigation.next) : "No next"}
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}

function pageRefLabel(ref: ViewerPageRef): string {
  return `Page ${ref.page_number}${
    ref.sub_page_index == null ? "" : ref.sub_page_index === 0 ? " Left" : " Right"
  }`;
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={cn("h-2 w-2 rounded-full", color)} />
      <span className="text-2xs text-slate-500">{label}</span>
    </div>
  );
}
