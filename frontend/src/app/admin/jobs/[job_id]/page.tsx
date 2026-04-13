"use client";

// Admin job detail — same component structure as user but accessed via admin shell
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, FileSearch, AlertTriangle, CheckCircle, XCircle, Clock, RefreshCw, GitBranch } from "lucide-react";
import { getJob } from "@/lib/api/jobs";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { ArtifactImage } from "@/components/shared/artifact-image";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { ErrorBanner } from "@/components/shared/error-banner";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { formatDate, formatRelative, formatDuration, formatScore, truncateId, hasActivePages, isJobActive, reviewReasonLabel } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { JobPage } from "@/types/api";

export default function AdminJobDetailPage() {
  const { job_id } = useParams<{ job_id: string }>();
  const router = useRouter();

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["job", job_id],
    queryFn: () => getJob(job_id),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 8_000;
      const active = isJobActive(data.summary.status) || (data.pages && hasActivePages(data.pages));
      return active ? 6_000 : false;
    },
  });

  if (isLoading) {
    return (
      <AdminShell breadcrumbs={[{ label: "Jobs", href: "/admin/jobs" }, { label: "Loading…" }]}>
        <div className="flex items-center justify-center py-20"><Spinner size="lg" /></div>
      </AdminShell>
    );
  }

  if (isError || !data) {
    return (
      <AdminShell breadcrumbs={[{ label: "Jobs", href: "/admin/jobs" }, { label: "Error" }]}>
        <div className="p-6"><ErrorBanner variant="fullscreen" title="Job Not Found" message="This job could not be loaded." /></div>
      </AdminShell>
    );
  }

  const { summary, pages } = data;
  const isActive = isJobActive(summary.status) || (pages && hasActivePages(pages));

  return (
    <AdminShell
      breadcrumbs={[{ label: "Jobs", href: "/admin/jobs" }, { label: truncateId(summary.job_id, 8) + "…" }]}
      headerRight={
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => router.push(`/admin/lineage/${job_id}/1`)} className="gap-1.5 text-slate-500">
            <GitBranch className="h-3.5 w-3.5" />
            Lineage
          </Button>
          <Button variant="ghost" size="sm" onClick={() => refetch()} className="gap-1.5 text-slate-500">
            <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
            {isActive && <span className="text-xs text-indigo-600">Live</span>}
          </Button>
        </div>
      }
    >
      <TooltipProvider>
        <div className="p-6 space-y-6 max-w-6xl">
          <PageHeader
            title={`Job ${truncateId(summary.job_id, 12)}…`}
            icon={FileSearch}
            badge={<StatusBadge status={summary.status} type="job" />}
            actions={
              summary.pending_human_correction_count > 0 ? (
                <Button size="sm" onClick={() => router.push(`/admin/queue?job_id=${summary.job_id}`)} className="gap-1.5">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  {summary.pending_human_correction_count} pages need review
                </Button>
              ) : undefined
            }
          />

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              { label: "Total Pages", value: summary.page_count, color: "text-slate-900" },
              { label: "Accepted", value: summary.accepted_count, color: "text-emerald-600" },
              { label: "Review", value: summary.review_count, color: "text-yellow-600" },
              { label: "Failed", value: summary.failed_count, color: "text-red-500" },
            ].map(({ label, value, color }) => (
              <div key={label} className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
                <p className="text-2xs text-slate-500 uppercase tracking-wider mb-2">{label}</p>
                <p className={cn("text-xl font-semibold tabular-nums", color)}>{value}</p>
              </div>
            ))}
          </div>

          {/* Owner / admin metadata */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-xs">
              {[
                ["Collection", summary.collection_id],
                ["Owner", summary.created_by_username ?? "unknown"],
                ["Material", summary.material_type],
                ["Pipeline", summary.pipeline_mode],
                ["PTIFF QA", summary.ptiff_qa_mode],
                ["Policy", summary.policy_version],
                ["Shadow", summary.shadow_mode ? "On" : "Off"],
                ["Created", formatDate(summary.created_at)],
              ].map(([label, value]) => (
                <div key={label}>
                  <p className="text-2xs text-slate-400 mb-0.5">{label}</p>
                  <p className="text-xs text-slate-700 capitalize">{value}</p>
                </div>
              ))}
            </div>
          </div>

          <div>
            <h2 className="text-sm font-semibold text-slate-800 mb-3">Pages ({pages.length})</h2>
            <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
              <table className="w-full data-table">
                <thead>
                  <tr>
                    <th>#</th><th>State</th><th>Routing</th><th>Review Reasons</th><th>Quality</th><th>Time</th><th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {pages.map((page) => (
                    <tr key={`${page.page_number}-${page.sub_page_index ?? 0}`} className={cn(page.status === "pending_human_correction" && "bg-orange-50/70 hover:bg-orange-50", page.status !== "pending_human_correction" && "hover:bg-slate-50")}>
                      <td><span className="text-xs text-slate-400 font-mono">{page.page_number}{page.sub_page_index != null && `/${page.sub_page_index}`}</span></td>
                      <td><StatusBadge status={page.status} type="page" /></td>
                      <td><span className="text-xs text-slate-500">{page.routing_path ?? "—"}</span></td>
                      <td>
                        <div className="flex flex-wrap gap-1">
                          {page.review_reasons?.map((r) => (
                            <span key={r} className="text-2xs bg-orange-50 text-orange-700 border border-orange-200 rounded px-1.5 py-0.5">{reviewReasonLabel(r)}</span>
                          ))}
                        </div>
                      </td>
                      <td>
                        {page.quality_summary ? (
                          <div className="flex gap-2 text-2xs text-slate-500">
                            {page.quality_summary.blur_score != null && <span>blur: {formatScore(page.quality_summary.blur_score, 2)}</span>}
                          </div>
                        ) : <span className="text-slate-300 text-xs">—</span>}
                      </td>
                      <td><span className="text-xs text-slate-500 tabular-nums">{formatDuration(page.processing_time_ms)}</span></td>
                      <td>
                        <div className="flex items-center gap-1.5">
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
                              <ChevronRight className="h-3 w-3" />Review
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
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </TooltipProvider>
    </AdminShell>
  );
}
