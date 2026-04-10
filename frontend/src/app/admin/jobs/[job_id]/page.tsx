"use client";

import { Fragment, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Clock,
  FileSearch,
  GitBranch,
  RefreshCw,
} from "lucide-react";
import { getJob } from "@/lib/api/jobs";
import { AdminShell } from "@/components/layout/admin-shell";
import { LayoutOverlay } from "@/components/jobs/layout-overlay";
import { PageHeader } from "@/components/shared/page-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { ErrorBanner } from "@/components/shared/error-banner";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  cn,
  formatDate,
  formatDuration,
  formatScore,
  hasActivePages,
  isJobActive,
  reviewReasonLabel,
  truncateId,
} from "@/lib/utils";
import type { JobPage } from "@/types/api";

export default function AdminJobDetailPage() {
  const { job_id } = useParams<{ job_id: string }>();
  const router = useRouter();
  const [expandedLayoutKey, setExpandedLayoutKey] = useState<string | null>(null);

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["job", job_id],
    queryFn: () => getJob(job_id),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const job = query.state.data;
      if (!job) return 8_000;
      const operationalPages = filterOperationalPages(job.pages);

      const active =
        isJobActive(job.summary.status) ||
        (operationalPages.length > 0 && hasActivePages(operationalPages));

      return active ? 6_000 : false;
    },
  });

  if (isLoading) {
    return (
      <AdminShell
        breadcrumbs={[{ label: "Jobs", href: "/admin/jobs" }, { label: "Loading..." }]}
      >
        <div className="flex items-center justify-center py-20">
          <Spinner size="lg" />
        </div>
      </AdminShell>
    );
  }

  if (isError || !data) {
    return (
      <AdminShell
        breadcrumbs={[{ label: "Jobs", href: "/admin/jobs" }, { label: "Error" }]}
      >
        <div className="p-6">
          <ErrorBanner
            variant="fullscreen"
            title="Job Not Found"
            message="This job could not be loaded."
          />
        </div>
      </AdminShell>
    );
  }

  const { summary, pages } = data;
  const operationalPages = filterOperationalPages(pages);
  const isActive = isJobActive(summary.status) || hasActivePages(operationalPages);

  return (
    <AdminShell
      breadcrumbs={[
        { label: "Jobs", href: "/admin/jobs" },
        { label: `${truncateId(summary.job_id, 8)}...` },
      ]}
      headerRight={
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push(`/admin/lineage/${job_id}/1`)}
            className="gap-1.5 text-slate-500"
          >
            <GitBranch className="h-3.5 w-3.5" />
            Lineage
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            className="gap-1.5 text-slate-500"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
            {isActive && <span className="text-xs text-indigo-600">Live</span>}
          </Button>
        </div>
      }
    >
      <TooltipProvider>
        <div className="max-w-6xl space-y-6 p-6">
          <PageHeader
            title={`Job ${truncateId(summary.job_id, 12)}...`}
            icon={FileSearch}
            badge={<StatusBadge status={summary.status} type="job" />}
            actions={
              summary.pending_human_correction_count > 0 ? (
                <Button
                  size="sm"
                  onClick={() => router.push(`/admin/queue?job_id=${summary.job_id}`)}
                  className="gap-1.5"
                >
                  <AlertTriangle className="h-3.5 w-3.5" />
                  {summary.pending_human_correction_count} pages need review
                </Button>
              ) : undefined
            }
          />

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              { label: "Total Pages", value: summary.page_count, color: "text-slate-900" },
              { label: "Accepted", value: summary.accepted_count, color: "text-emerald-600" },
              { label: "Review", value: summary.review_count, color: "text-yellow-600" },
              { label: "Failed", value: summary.failed_count, color: "text-red-500" },
            ].map(({ label, value, color }) => (
              <div key={label} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                <p className="mb-2 text-2xs uppercase tracking-wider text-slate-500">
                  {label}
                </p>
                <p className={cn("text-xl font-semibold tabular-nums", color)}>{value}</p>
              </div>
            ))}
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="grid grid-cols-2 gap-4 text-xs sm:grid-cols-4">
              {[
                ["Collection", summary.collection_id],
                ["Owner", summary.created_by_username ?? "unknown"],
                ["Material", summary.material_type],
                ["Pipeline", summary.pipeline_mode],
                ["Policy", summary.policy_version],
                ["Shadow", summary.shadow_mode ? "On" : "Off"],
                ["Created", formatDate(summary.created_at)],
              ].map(([label, value]) => (
                <div key={label}>
                  <p className="mb-0.5 text-2xs text-slate-400">{label}</p>
                  <p className="text-xs capitalize text-slate-700">{value}</p>
                </div>
              ))}
            </div>
          </div>

          <div>
              <h2 className="mb-3 text-sm font-semibold text-slate-800">
               Pages ({operationalPages.length})
             </h2>
            <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
              <table className="w-full data-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>State</th>
                    <th>Routing</th>
                    <th>Review Reasons</th>
                    <th>Quality</th>
                    <th>Time</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {operationalPages.map((page) => {
                    const pageKey = `${page.page_number}-${page.sub_page_index ?? 0}`;
                    const isLayoutOpen = expandedLayoutKey === pageKey;
                    const displayImageUri = page.output_image_uri ?? page.input_image_uri;
                    const canInspectLayout = Boolean(
                      displayImageUri && page.output_layout_uri
                    );

                    return (
                      <Fragment key={pageKey}>
                        <tr
                          className={cn(
                            page.status === "pending_human_correction" &&
                              "bg-orange-50/70 hover:bg-orange-50",
                            page.status !== "pending_human_correction" &&
                              "hover:bg-slate-50"
                          )}
                        >
                          <td>
                            <span className="font-mono text-xs text-slate-400">
                              {page.page_number}
                              {page.sub_page_index != null && `/${page.sub_page_index}`}
                            </span>
                          </td>
                          <td>
                            <StatusBadge status={page.status} type="page" />
                          </td>
                          <td>
                            <span className="text-xs text-slate-500">
                              {page.routing_path ?? "-"}
                            </span>
                          </td>
                          <td>
                            <div className="flex flex-wrap gap-1">
                              {page.review_reasons?.map((reason) => (
                                <span
                                  key={reason}
                                  className="rounded border border-orange-200 bg-orange-50 px-1.5 py-0.5 text-2xs text-orange-700"
                                >
                                  {reviewReasonLabel(reason)}
                                </span>
                              ))}
                            </div>
                          </td>
                          <td>
                            {page.quality_summary ? (
                              <div className="flex gap-2 text-2xs text-slate-500">
                                {page.quality_summary.blur_score != null && (
                                  <span>
                                    blur: {formatScore(page.quality_summary.blur_score, 2)}
                                  </span>
                                )}
                              </div>
                            ) : (
                              <span className="text-xs text-slate-300">-</span>
                            )}
                          </td>
                          <td>
                            <span className="tabular-nums text-xs text-slate-500">
                              {formatDuration(page.processing_time_ms)}
                            </span>
                          </td>
                          <td>
                            <div className="flex flex-wrap items-center gap-1.5">
                              {canInspectLayout && (
                                <Button
                                  size="xs"
                                  variant={isLayoutOpen ? "secondary" : "ghost"}
                                  onClick={() =>
                                    setExpandedLayoutKey((current) =>
                                      current === pageKey ? null : pageKey
                                    )
                                  }
                                  className="gap-1"
                                >
                                  {isLayoutOpen ? (
                                    <ChevronDown className="h-3 w-3" />
                                  ) : (
                                    <ChevronRight className="h-3 w-3" />
                                  )}
                                  Layout
                                </Button>
                              )}
                              {page.status === "pending_human_correction" && (
                                <Button
                                  size="xs"
                                  onClick={() =>
                                    router.push(
                                      `/admin/queue/${summary.job_id}/${page.page_number}/workspace${
                                        page.sub_page_index != null
                                          ? `?sub_page_index=${page.sub_page_index}`
                                          : ""
                                      }`
                                    )
                                  }
                                  className="gap-1"
                                >
                                  <ChevronRight className="h-3 w-3" />
                                  Review
                                </Button>
                              )}
                              <Button
                                size="xs"
                                variant="ghost"
                                onClick={() =>
                                  router.push(
                                    `/admin/lineage/${summary.job_id}/${page.page_number}${
                                      page.sub_page_index != null
                                        ? `?sub_page_index=${page.sub_page_index}`
                                        : ""
                                    }`
                                  )
                                }
                              >
                                Lineage
                              </Button>
                            </div>
                          </td>
                        </tr>

                        {isLayoutOpen && (
                          <tr className="bg-slate-50/80">
                            <td colSpan={7} className="px-4 py-4">
                              <LayoutOverlay
                                imageUri={displayImageUri}
                                layoutUri={page.output_layout_uri}
                                pageLabel={`Page ${page.page_number}${
                                  page.sub_page_index != null
                                    ? ` / ${page.sub_page_index}`
                                    : ""
                                }`}
                              />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </TooltipProvider>
    </AdminShell>
  );
}

function filterOperationalPages(pages: JobPage[]): JobPage[] {
  const pageNumbersWithChildren = new Set(
    pages.filter((page) => page.sub_page_index != null).map((page) => page.page_number)
  );
  return pages.filter((page) => {
    if (page.status === "split") return false;
    if (page.sub_page_index == null && pageNumbersWithChildren.has(page.page_number)) {
      return false;
    }
    return true;
  });
}
