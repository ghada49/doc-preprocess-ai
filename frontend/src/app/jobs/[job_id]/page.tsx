"use client";

import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  RefreshCw,
  FileSearch,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Clock,
} from "lucide-react";
import { getJob } from "@/lib/api/jobs";
import { UserShell } from "@/components/layout/user-shell";
import { PageHeader } from "@/components/shared/page-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { ArtifactImage } from "@/components/shared/artifact-image";
import { ArtifactLinkButton } from "@/components/shared/artifact-link-button";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBanner } from "@/components/shared/error-banner";
import { Spinner } from "@/components/ui/spinner";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  formatDate,
  formatRelative,
  formatDuration,
  formatScore,
  truncateId,
  hasActivePages,
  isJobActive,
  pageStateLabel,
  reviewReasonLabel,
} from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { JobPage } from "@/types/api";

export default function JobDetailPage() {
  const { job_id } = useParams<{ job_id: string }>();
  const router = useRouter();

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["job", job_id],
    queryFn: () => getJob(job_id),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 8_000;
      const active =
        isJobActive(data.summary.status) ||
        (data.pages && hasActivePages(data.pages));
      return active ? 6_000 : false;
    },
  });

  if (isLoading) {
    return (
      <UserShell breadcrumbs={[{ label: "My Jobs", href: "/jobs" }, { label: "Loading…" }]}>
        <div className="flex items-center justify-center py-20">
          <Spinner size="lg" />
        </div>
      </UserShell>
    );
  }

  if (isError || !data) {
    return (
      <UserShell breadcrumbs={[{ label: "My Jobs", href: "/jobs" }, { label: "Error" }]}>
        <div className="p-6">
          <ErrorBanner variant="fullscreen" title="Job Not Found" message="This job could not be loaded." />
        </div>
      </UserShell>
    );
  }

  const { summary, pages } = data;
  const isActive = isJobActive(summary.status) || (pages && hasActivePages(pages));

  return (
    <UserShell
      breadcrumbs={[
        { label: "My Jobs", href: "/jobs" },
        { label: truncateId(summary.job_id, 8) + "…" },
      ]}
      headerRight={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => refetch()}
          className="gap-1.5 text-slate-500"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
          {isActive && <span className="text-xs text-indigo-600">Live</span>}
        </Button>
      }
    >
      <TooltipProvider>
        <div className="p-6 space-y-6 max-w-6xl">
          {/* Header */}
          <PageHeader
            title={`Job ${truncateId(summary.job_id, 12)}…`}
            icon={FileSearch}
            badge={<StatusBadge status={summary.status} type="job" />}
            actions={
              summary.pending_human_correction_count > 0 ? (
                <Button
                  size="sm"
                  onClick={() => router.push(`/queue?job_id=${summary.job_id}`)}
                  className="gap-1.5"
                >
                  <AlertTriangle className="h-3.5 w-3.5" />
                  {summary.pending_human_correction_count} pages need review
                </Button>
              ) : undefined
            }
          />

          {/* Summary cards */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <SummaryCard
              label="Total Pages"
              value={summary.page_count}
              color="text-slate-900"
            />
            <SummaryCard
              label="Accepted"
              value={summary.accepted_count}
              color="text-emerald-600"
              icon={<CheckCircle className="h-3.5 w-3.5" />}
            />
            <SummaryCard
              label="Review"
              value={summary.review_count}
              color="text-yellow-600"
              icon={<Clock className="h-3.5 w-3.5" />}
            />
            <SummaryCard
              label="Failed"
              value={summary.failed_count}
              color="text-red-500"
              icon={<XCircle className="h-3.5 w-3.5" />}
            />
          </div>

          {/* Job metadata */}
          <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-xs">
              <MetaField label="Collection" value={summary.collection_id} />
              <MetaField label="Material" value={summary.material_type} />
              <MetaField label="Pipeline" value={summary.pipeline_mode} />
              <MetaField label="PTIFF QA" value={summary.ptiff_qa_mode} />
              <MetaField label="Policy" value={summary.policy_version} />
              <MetaField
                label="Shadow"
                value={
                  <Badge variant={summary.shadow_mode ? "warning" : "muted"}>
                    {summary.shadow_mode ? "On" : "Off"}
                  </Badge>
                }
              />
              <MetaField label="Created" value={formatDate(summary.created_at)} />
              <MetaField
                label="Completed"
                value={summary.completed_at ? formatDate(summary.completed_at) : "—"}
              />
            </div>

            {/* Progress bar */}
            {summary.page_count > 0 && (
              <div className="mt-5 pt-4 border-t border-slate-200">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-2xs text-slate-500">Pipeline progress</span>
                  <span className="text-2xs text-slate-500">
                    {summary.accepted_count + summary.review_count + summary.failed_count} / {summary.page_count} terminal
                  </span>
                </div>
                <div className="h-2 bg-slate-200 rounded-full overflow-hidden flex">
                  <div
                    className="bg-emerald-500 transition-all"
                    style={{ width: `${(summary.accepted_count / summary.page_count) * 100}%` }}
                  />
                  <div
                    className="bg-yellow-500 transition-all"
                    style={{ width: `${(summary.review_count / summary.page_count) * 100}%` }}
                  />
                  <div
                    className="bg-red-500 transition-all"
                    style={{ width: `${(summary.failed_count / summary.page_count) * 100}%` }}
                  />
                  <div
                    className="bg-orange-400 transition-all"
                    style={{
                      width: `${(summary.pending_human_correction_count / summary.page_count) * 100}%`,
                    }}
                  />
                </div>
                <div className="flex items-center gap-3 mt-2">
                  <LegendDot color="bg-emerald-500" label={`${summary.accepted_count} accepted`} />
                  <LegendDot color="bg-yellow-500" label={`${summary.review_count} review`} />
                  <LegendDot color="bg-red-500" label={`${summary.failed_count} failed`} />
                  {summary.pending_human_correction_count > 0 && (
                    <LegendDot color="bg-orange-400" label={`${summary.pending_human_correction_count} correction`} />
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Pages table */}
          <div>
            <h2 className="text-sm font-semibold text-slate-800 mb-3">
              Pages
              <span className="ml-2 text-xs text-slate-400 font-normal">({pages.length})</span>
            </h2>
            <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
              <table className="w-full data-table">
                <thead>
                  <tr>
                    <th className="w-12">#</th>
                    <th>State</th>
                    <th>Routing</th>
                    <th>Review Reasons</th>
                    <th>Quality</th>
                    <th>Time</th>
                    <th>Output</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {pages.map((page) => (
                    <PageRow
                      key={`${page.page_number}-${page.sub_page_index ?? 0}`}
                      page={page}
                      jobId={summary.job_id}
                      onOpenWorkspace={() =>
                        router.push(
                          `/queue/${summary.job_id}/${page.page_number}/workspace${
                            page.sub_page_index != null
                              ? `?sub_page_index=${page.sub_page_index}`
                              : ""
                          }`
                        )
                      }
                      onOpenPtiffQa={() =>
                        router.push(`/jobs/${summary.job_id}/ptiff-qa`)
                      }
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </TooltipProvider>
    </UserShell>
  );
}

function PageRow({
  page,
  jobId,
  onOpenWorkspace,
  onOpenPtiffQa,
}: {
  page: JobPage;
  jobId: string;
  onOpenWorkspace: () => void;
  onOpenPtiffQa: () => void;
}) {
  const isAttention = page.status === "pending_human_correction";
  const isPtiffQa = page.status === "ptiff_qa_pending";

  return (
    <tr
      className={cn(
        isAttention && "bg-orange-50/70 hover:bg-orange-50",
        !isAttention && "hover:bg-slate-50"
      )}
    >
      <td>
        <span className="text-xs text-slate-400 tabular-nums font-mono">
          {page.page_number}
          {page.sub_page_index != null && (
            <span className="text-slate-300">/{page.sub_page_index}</span>
          )}
        </span>
      </td>
      <td>
        <StatusBadge status={page.status} type="page" />
      </td>
      <td>
        <span className="text-xs text-slate-500">{page.routing_path ?? "—"}</span>
      </td>
      <td>
        <div className="flex flex-wrap gap-1">
          {page.review_reasons?.map((r) => (
            <span
              key={r}
              className="text-2xs bg-orange-50 text-orange-700 border border-orange-200 rounded px-1.5 py-0.5"
            >
              {reviewReasonLabel(r)}
            </span>
          ))}
        </div>
      </td>
      <td>
        {page.quality_summary ? (
          <div className="flex gap-2 text-2xs text-slate-500">
            {page.quality_summary.blur_score != null && (
              <span>blur: {formatScore(page.quality_summary.blur_score, 2)}</span>
            )}
            {page.quality_summary.skew_residual != null && (
              <span>skew: {formatScore(page.quality_summary.skew_residual, 2)}</span>
            )}
          </div>
        ) : (
          <span className="text-slate-300 text-xs">—</span>
        )}
      </td>
      <td>
        <span className="text-xs text-slate-500 tabular-nums">
          {formatDuration(page.processing_time_ms)}
        </span>
      </td>
      <td>
        <div className="flex items-center gap-2">
          <ArtifactImage
            uri={page.output_image_uri}
            containerClassName="h-9 w-8 rounded border border-slate-200"
            className="rounded object-cover"
            fallbackText=""
          />
          <ArtifactLinkButton uri={page.output_image_uri} label="Open" size="xs" />
        </div>
      </td>
      <td>
        {isAttention && (
          <Button size="xs" onClick={onOpenWorkspace} className="gap-1">
            <ChevronRight className="h-3 w-3" />
            Review
          </Button>
        )}
        {isPtiffQa && (
          <Button size="xs" variant="secondary" onClick={onOpenPtiffQa} className="gap-1">
            PTIFF QA
          </Button>
        )}
      </td>
    </tr>
  );
}

function SummaryCard({
  label,
  value,
  color,
  icon,
}: {
  label: string;
  value: number;
  color: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
      <p className="text-2xs text-slate-500 uppercase tracking-wider mb-2">{label}</p>
      <div className={cn("flex items-center gap-1.5 text-xl font-semibold tabular-nums", color)}>
        {icon}
        {value}
      </div>
    </div>
  );
}

function MetaField({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-2xs text-slate-400 mb-0.5">{label}</p>
      <p className="text-xs text-slate-700 capitalize">{value}</p>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={cn("h-2 w-2 rounded-full", color)} />
      <span className="text-2xs text-slate-500">{label}</span>
    </div>
  );
}
