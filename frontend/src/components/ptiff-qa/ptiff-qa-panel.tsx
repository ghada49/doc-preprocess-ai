"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { CheckCircle, AlertTriangle, Shield } from "lucide-react";
import { getPtiffQa, approveAllPtiffQa, approvePtiffQaPage, editPtiffQaPage } from "@/lib/api/ptiff-qa";
import { PageHeader } from "@/components/shared/page-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { ErrorBanner } from "@/components/shared/error-banner";
import { cn } from "@/lib/utils";
import { isApiError } from "@/lib/api/client";

interface PtiffQaPanelProps {
  jobId: string;
  backPath?: string;
}

export default function PtiffQaPanel({ jobId }: PtiffQaPanelProps) {
  const queryClient = useQueryClient();

  const { data, isLoading, isError } = useQuery({
    queryKey: ["ptiff-qa", jobId],
    queryFn: () => getPtiffQa(jobId),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const response = query.state.data;
      if (!response) return 8_000;
      return response.pages_pending > 0 ? 8_000 : false;
    },
  });

  const approveAllMut = useMutation({
    mutationFn: () => approveAllPtiffQa(jobId),
    onSuccess: (res) => {
      toast.success(
        `${res.approved_count} page${res.approved_count !== 1 ? "s" : ""} approved.${
          res.gate_released ? " Processing can continue." : ""
        }`
      );
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
    onError: () => toast.error("We could not approve all pages. Please try again."),
  });

  const approvePageMut = useMutation({
    mutationFn: (page: { pageNumber: number; subPageIndex?: number | null }) =>
      approvePtiffQaPage(jobId, page.pageNumber, page.subPageIndex ?? undefined),
    onSuccess: (res) => {
      toast.success(
        `Page ${res.page_number} approved.${res.gate_released ? " Processing can continue." : ""}`
      );
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("This page is no longer waiting for review.");
      else toast.error("We could not approve this page.");
    },
  });

  const editPageMut = useMutation({
    mutationFn: (page: { pageNumber: number; subPageIndex?: number | null }) =>
      editPtiffQaPage(jobId, page.pageNumber, page.subPageIndex ?? undefined),
    onSuccess: (res) => {
      toast.success(`Page ${res.page_number} moved to review.`);
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["correction-queue"] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("This page is no longer waiting for review.");
      else toast.error("We could not send this page for review.");
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
      <div className="p-6">
        <ErrorBanner
          variant="fullscreen"
          title="Could not load review"
          message="There was a problem loading these pages. Please try again."
        />
      </div>
    );
  }

  const overview = summarizeOverview(data.pages);
  const reviewBadge =
    overview.needsReviewCount > 0
      ? { label: "Needs review", variant: "warning" as const }
      : overview.toReviewCount > 0
        ? { label: "To review", variant: "warning" as const }
        : overview.approvedCount > 0
          ? { label: "Approved", variant: "success" as const }
          : overview.readyCount > 0
            ? { label: "Ready", variant: "success" as const }
        : { label: "Ready", variant: "success" as const };

  return (
    <div className="max-w-4xl space-y-5 p-6">
      <PageHeader
        title="Review results"
        description="Approve pages that look correct or send pages that need attention to review."
        icon={Shield}
        badge={
          <Badge variant={reviewBadge.variant}>{reviewBadge.label}</Badge>
        }
        actions={
          <Button
            onClick={() => approveAllMut.mutate()}
            loading={approveAllMut.isPending}
            disabled={overview.toReviewCount === 0 || approveAllMut.isPending}
            className="gap-1.5"
          >
            <CheckCircle className="h-4 w-4" />
            Approve all ({overview.toReviewCount})
          </Button>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        {[
          {
            label: "Total",
            value: data.total_pages,
            color: "text-slate-900",
            surface: "bg-white border-slate-200",
          },
          {
            label: "To review",
            value: overview.toReviewCount,
            color: "text-amber-600",
            surface: "bg-amber-50 border-amber-200",
          },
          {
            label: "Approved",
            value: overview.approvedCount,
            color: "text-indigo-600",
            surface: "bg-indigo-50 border-indigo-200",
          },
          {
            label: "Ready",
            value: overview.readyCount,
            color: "text-emerald-600",
            surface: "bg-emerald-50 border-emerald-200",
          },
          {
            label: "Needs review",
            value: overview.needsReviewCount,
            color: "text-orange-600",
            surface: "bg-orange-50 border-orange-200",
          },
        ].map(({ label, value, color, surface }) => (
          <div
            key={label}
            className={cn("rounded-2xl border p-4 text-center shadow-sm", surface)}
          >
            <p className={cn("text-2xl font-semibold tabular-nums", color)}>{value}</p>
            <p className="mt-1 text-2xs text-slate-500">{label}</p>
          </div>
        ))}
      </div>

      {data.ptiff_qa_mode === "manual" &&
        (overview.toReviewCount > 0 || overview.needsReviewCount > 0) && (
        <div className="flex items-start gap-3 rounded-2xl border border-amber-200 bg-amber-50 p-4 shadow-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <div>
            <p className="text-xs font-semibold text-amber-900">
              {overview.needsReviewCount > 0 ? "Review in progress" : "Review required"}
            </p>
            <p className="mt-0.5 text-xs leading-relaxed text-amber-700">
              {overview.needsReviewCount > 0
                ? "Finish reviewing the flagged pages before processing can continue."
                : "Approve each page before processing continues."}
            </p>
          </div>
        </div>
      )}

      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm shadow-slate-200/70">
        <table className="w-full data-table">
          <thead>
            <tr>
              <th>Page</th>
              <th>Status</th>
              <th>Review</th>
              <th>Needs review</th>
              <th className="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.pages.map((page) => {
              const qaState = getQaOverviewState(page);
              return (
                <tr
                  key={`${page.page_number}-${page.sub_page_index ?? 0}`}
                  className={cn(
                    qaState.rowClass,
                    !qaState.rowClass && "hover:bg-slate-50"
                  )}
                >
                  <td>
                    <span className="font-mono text-xs tabular-nums text-slate-700">
                      {page.page_number}
                      {page.sub_page_index != null &&
                        ` ${page.sub_page_index === 0 ? "Left" : "Right"}`}
                    </span>
                  </td>
                  <td>
                    <StatusBadge status={page.current_state} type="page" />
                  </td>
                  <td>
                    <Badge variant={qaState.reviewVariant} dot>
                      {qaState.reviewLabel}
                    </Badge>
                  </td>
                  <td>
                    {qaState.needsReview ? (
                      <Badge variant="danger">Yes</Badge>
                    ) : (
                      <span className="text-xs text-slate-500">No</span>
                    )}
                  </td>
                  <td className="text-right">
                    {qaState.showActions && (
                      <div className="flex items-center justify-end gap-1.5">
                        <Button
                          size="xs"
                          variant="success"
                          onClick={() =>
                            approvePageMut.mutate({
                              pageNumber: page.page_number,
                              subPageIndex: page.sub_page_index,
                            })
                          }
                          loading={approvePageMut.isPending}
                          className="gap-1"
                        >
                          <CheckCircle className="h-3 w-3" />
                          Approve
                        </Button>
                        <Button
                          size="xs"
                          variant="outline"
                          onClick={() =>
                            editPageMut.mutate({
                              pageNumber: page.page_number,
                              subPageIndex: page.sub_page_index,
                            })
                          }
                          loading={editPageMut.isPending}
                        >
                          Needs review
                        </Button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function summarizeOverview(
  pages: Array<{
    current_state: string;
    approval_status: "approved" | "pending";
    needs_correction: boolean;
  }>
) {
  let toReviewCount = 0;
  let needsReviewCount = 0;
  let approvedCount = 0;
  let readyCount = 0;

  for (const page of pages) {
    const qaState = getQaOverviewState(page);
    if (qaState.kind === "to_review") {
      toReviewCount += 1;
    } else if (qaState.kind === "needs_review") {
      needsReviewCount += 1;
    } else if (qaState.kind === "approved") {
      approvedCount += 1;
    } else {
      readyCount += 1;
    }
  }

  return { toReviewCount, needsReviewCount, approvedCount, readyCount };
}

function getQaOverviewState(page: {
  current_state: string;
  approval_status: "approved" | "pending";
  needs_correction: boolean;
}) {
  if (page.current_state === "pending_human_correction" || page.needs_correction) {
    return {
      kind: "needs_review" as const,
      reviewLabel: "In review",
      reviewVariant: "warning" as const,
      needsReview: true,
      showActions: false,
      rowClass: "bg-orange-50/60 hover:bg-orange-50/80",
    };
  }

  if (page.current_state === "ptiff_qa_pending") {
    if (page.approval_status === "approved") {
      return {
        kind: "approved" as const,
        reviewLabel: "Approved",
        reviewVariant: "success" as const,
        needsReview: false,
        showActions: false,
        rowClass: "bg-slate-50/60",
      };
    }

    return {
      kind: "to_review" as const,
      reviewLabel: "To review",
      reviewVariant: "warning" as const,
      needsReview: false,
      showActions: true,
      rowClass: "hover:bg-amber-50/60",
    };
  }

  if (page.current_state === "accepted") {
    return {
      kind: "ready" as const,
      reviewLabel: "Ready",
      reviewVariant: "success" as const,
      needsReview: false,
      showActions: false,
      rowClass: "bg-slate-50/60",
    };
  }

  return {
    kind: "reviewed" as const,
    reviewLabel: "Reviewed",
    reviewVariant: "success" as const,
    needsReview: false,
    showActions: false,
    rowClass: "bg-slate-50/60",
  };
}
