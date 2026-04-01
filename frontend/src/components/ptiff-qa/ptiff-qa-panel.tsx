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
import { getApiErrorMessage, isApiError } from "@/lib/api/client";

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
        `${res.approved_count} pages approved.${res.gate_released ? " Gate released!" : ""}`
      );
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
    onError: () => toast.error("Failed to approve all pages."),
  });

  const approvePageMut = useMutation({
    mutationFn: (page: { pageNumber: number; subPageIndex?: number | null }) =>
      approvePtiffQaPage(jobId, page.pageNumber, page.subPageIndex ?? undefined),
    onSuccess: (res) => {
      toast.success(
        `Page ${res.page_number} approved.${res.gate_released ? " Gate released!" : ""}`
      );
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("Page is not in ptiff_qa_pending state.");
      else toast.error(getApiErrorMessage(err, "Failed to approve page."));
    },
  });

  const editPageMut = useMutation({
    mutationFn: (page: { pageNumber: number; subPageIndex?: number | null }) =>
      editPtiffQaPage(jobId, page.pageNumber, page.subPageIndex ?? undefined),
    onSuccess: (res) => {
      toast.success(`Page ${res.page_number} → ${res.new_state}`);
      queryClient.invalidateQueries({ queryKey: ["ptiff-qa", jobId] });
      queryClient.invalidateQueries({ queryKey: ["correction-queue"] });
    },
    onError: (err: unknown) => {
      const status = isApiError(err) ? err.status : null;
      if (status === 409) toast.error("Page is not in ptiff_qa_pending state.");
      else toast.error(getApiErrorMessage(err, "Failed to send page to correction."));
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
          title="Failed to Load"
          message="Could not load PTIFF QA data."
        />
      </div>
    );
  }

  const allApproved = data.pages.every((p) => p.approval_status === "approved");

  return (
    <div className="p-6 max-w-4xl space-y-5">
      <PageHeader
        title="PTIFF Quality Assurance"
        description="Review and approve pages before the gate releases."
        icon={Shield}
        badge={
          data.is_gate_ready ? (
            <Badge variant="success">Gate Ready</Badge>
          ) : (
            <Badge variant="warning">Gate Pending</Badge>
          )
        }
        actions={
          <Button
            onClick={() => approveAllMut.mutate()}
            loading={approveAllMut.isPending}
            disabled={allApproved || approveAllMut.isPending}
            className="gap-1.5"
          >
            <CheckCircle className="h-4 w-4" />
            Approve All ({data.pages_pending})
          </Button>
        }
      />

      {/* Summary */}
      <div className="grid grid-cols-4 gap-3">
        {[
          {
            label: "Total",
            value: data.total_pages,
            color: "text-slate-900",
            surface: "bg-white border-slate-200",
          },
          {
            label: "Pending",
            value: data.pages_pending,
            color: "text-amber-600",
            surface: "bg-amber-50 border-amber-200",
          },
          {
            label: "Approved",
            value: data.pages_approved,
            color: "text-emerald-600",
            surface: "bg-emerald-50 border-emerald-200",
          },
          {
            label: "In Correction",
            value: data.pages_in_correction,
            color: "text-orange-600",
            surface: "bg-orange-50 border-orange-200",
          },
        ].map(({ label, value, color, surface }) => (
          <div
            key={label}
            className={cn(
              "rounded-xl border p-4 text-center shadow-sm",
              surface
            )}
          >
            <p className={cn("text-2xl font-semibold tabular-nums", color)}>{value}</p>
            <p className="mt-1 text-2xs text-slate-500">{label}</p>
          </div>
        ))}
      </div>

      {data.ptiff_qa_mode === "manual" && !data.is_gate_ready && (
        <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 shadow-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <div>
            <p className="text-xs font-semibold text-amber-900">Manual gate mode</p>
            <p className="mt-0.5 text-xs leading-relaxed text-amber-700">
              All pages must be approved before the pipeline gate releases.
            </p>
          </div>
        </div>
      )}

      {/* Pages */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <table className="w-full data-table">
          <thead>
            <tr>
              <th>Page</th>
              <th>State</th>
              <th>Approval</th>
              <th>Needs Correction</th>
              <th className="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.pages.map((page) => (
              <tr
                key={`${page.page_number}-${page.sub_page_index ?? 0}`}
                className={cn(
                  page.approval_status === "pending" && "hover:bg-amber-50/60",
                  page.approval_status === "approved" && "bg-slate-50/60"
                )}
              >
                <td>
                  <span className="font-mono text-xs tabular-nums text-slate-700">
                    {page.page_number}
                    {page.sub_page_index != null && `/${page.sub_page_index}`}
                  </span>
                </td>
                <td>
                  <StatusBadge status={page.current_state} type="page" />
                </td>
                <td>
                  <Badge
                    variant={
                      page.approval_status === "approved"
                        ? "success"
                        : page.approval_status === "in_correction"
                        ? "purple"
                        : "warning"
                    }
                    dot
                  >
                    {page.approval_status}
                  </Badge>
                </td>
                <td>
                  {page.needs_correction ? (
                    <Badge variant="danger">Yes</Badge>
                  ) : (
                    <span className="text-xs text-slate-500">No</span>
                  )}
                </td>
                <td className="text-right">
                  {page.approval_status === "pending" && (
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
                        Send to Correction
                      </Button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
