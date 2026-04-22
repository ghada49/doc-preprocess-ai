"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  Search,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  AlertTriangle,
  RefreshCw,
} from "lucide-react";
import { listJobs } from "@/lib/api/jobs";
import type { JobSummary, JobStatus, PipelineMode } from "@/types/api";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/shared/status-badge";
import { Pagination } from "@/components/shared/pagination";
import { EmptyState } from "@/components/shared/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatDate, formatRelative, truncateId } from "@/lib/utils";
import { cn } from "@/lib/utils";

interface JobsTableProps {
  isAdmin?: boolean;
  basePath?: string; // e.g. "/admin/jobs" or "/jobs"
}

export function JobsTable({ isAdmin = false, basePath = "/jobs" }: JobsTableProps) {
  const router = useRouter();

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<JobStatus | "all">("all");
  const [pipelineFilter, setPipelineFilter] = useState<PipelineMode | "all">("all");
  const [page, setPage] = useState(1);
  const pageSize = 25;

  const { data, isLoading, isFetching, isError, refetch } = useQuery({
    queryKey: ["jobs", { search, statusFilter, pipelineFilter, page, pageSize }],
    queryFn: () =>
      listJobs({
        search: search || undefined,
        status: statusFilter !== "all" ? statusFilter : undefined,
        pipeline_mode: pipelineFilter !== "all" ? pipelineFilter : undefined,
        page,
        page_size: pageSize,
      }),
    staleTime: 15_000,
    refetchInterval: 20_000,
    retry: 1,
  });

  const jobs = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <TooltipProvider>
      <div className="flex flex-col gap-4">
        {/* Toolbar */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="relative flex-1 min-w-[200px] max-w-md">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-400" />
            <Input
              placeholder="Search job ID or collection…"
              value={search}
              onChange={(e) => { setSearch(e.target.value); setPage(1); }}
              className="pl-9"
            />
          </div>

          <Select
            value={statusFilter}
            onValueChange={(v) => { setStatusFilter(v as JobStatus | "all"); setPage(1); }}
          >
            <SelectTrigger className="w-36">
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              <SelectItem value="queued">Queued</SelectItem>
              <SelectItem value="running">Running</SelectItem>
              <SelectItem value="done">Done</SelectItem>
              <SelectItem value="failed">Failed</SelectItem>
            </SelectContent>
          </Select>

          <Select
            value={pipelineFilter}
            onValueChange={(v) => { setPipelineFilter(v as PipelineMode | "all"); setPage(1); }}
          >
            <SelectTrigger className="w-36">
              <SelectValue placeholder="Pipeline" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All pipelines</SelectItem>
              <SelectItem value="layout">Layout</SelectItem>
              <SelectItem value="preprocess">Preprocess</SelectItem>
            </SelectContent>
          </Select>

          <Button
            variant="ghost"
            size="icon"
            onClick={() => refetch()}
            className="h-9 w-9 text-slate-500"
            aria-label="Refresh"
          >
            <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          </Button>
        </div>

        {/* Table */}
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full data-table">
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Collection</th>
                  <th>Material</th>
                  <th>Pipeline</th>
                  {isAdmin && <th>Owner</th>}
                  <th>Status</th>
                  <th className="text-center">Pages</th>
                  <th className="text-center">Accepted</th>
                  <th className="text-center">Review</th>
                  <th className="text-center">Correction</th>
                  <th>Created</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  Array.from({ length: 8 }).map((_, i) => (
                    <tr key={i} className="border-b border-slate-100">
                      {Array.from({ length: isAdmin ? 12 : 11 }).map((_, j) => (
                        <td key={j} className="px-4 py-3.5">
                          <Skeleton className="h-4 w-full" />
                        </td>
                      ))}
                    </tr>
                  ))
                ) : isError ? (
                  <tr>
                    <td colSpan={isAdmin ? 12 : 11} className="p-0">
                      <EmptyState
                        title="Could not load jobs"
                        description="Check your connection and try refreshing."
                      />
                    </td>
                  </tr>
                ) : jobs.length === 0 ? (
                  <tr>
                    <td colSpan={isAdmin ? 12 : 11} className="p-0">
                      <EmptyState
                        title="No jobs found"
                        description={
                          search || statusFilter !== "all"
                            ? "Try clearing your filters."
                            : "Submit your first job to get started."
                        }
                      />
                    </td>
                  </tr>
                ) : (
                  jobs.map((job) => (
                    <JobRow
                      key={job.job_id}
                      job={job}
                      isAdmin={isAdmin}
                      basePath={basePath}
                      onClick={() => router.push(`${basePath}/${job.job_id}`)}
                    />
                  ))
                )}
              </tbody>
            </table>
          </div>

          {total > 0 && (
            <div className="border-t border-slate-200 px-4 py-3">
              <Pagination
                page={page}
                pageSize={pageSize}
                total={total}
                onPageChange={setPage}
              />
            </div>
          )}
        </div>
      </div>
    </TooltipProvider>
  );
}

function JobRow({
  job,
  isAdmin,
  basePath,
  onClick,
}: {
  job: JobSummary;
  isAdmin: boolean;
  basePath: string;
  onClick: () => void;
}) {
  const hasPendingCorrections = job.pending_human_correction_count > 0;
  const hasFailed = job.failed_count > 0;

  return (
    <tr
      onClick={onClick}
      className={cn(
        "cursor-pointer transition-colors",
        hasPendingCorrections && "bg-orange-50/70 hover:bg-orange-50",
        !hasPendingCorrections && "hover:bg-slate-50"
      )}
    >
      {/* Job ID */}
      <td>
        <div className="flex items-center gap-2">
          {hasPendingCorrections && (
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-orange-400 animate-pulse-slow" />
          )}
          <code className="text-xs text-indigo-600 font-mono">
            {truncateId(job.job_id, 8)}…
          </code>
        </div>
      </td>

      {/* Collection */}
      <td>
        <span className="text-slate-700 text-xs font-medium truncate max-w-[120px] block">
          {job.collection_id}
        </span>
      </td>

      {/* Material */}
      <td>
        <span className="text-slate-500 text-xs capitalize">{job.material_type}</span>
      </td>

      {/* Pipeline */}
      <td>
        <span className="text-slate-500 text-xs capitalize">{job.pipeline_mode}</span>
      </td>

      {/* Owner (admin only) */}
      {isAdmin && (
        <td>
          <span className="text-slate-500 text-xs">
            {job.created_by_username ?? <span className="text-slate-400 italic">unknown</span>}
          </span>
        </td>
      )}

      {/* Status */}
      <td>
        <StatusBadge status={job.status} type="job" />
      </td>

      {/* Page counts */}
      <td className="text-center">
        <span className="text-xs text-slate-700 tabular-nums font-medium">
          {job.page_count}
        </span>
      </td>
      <td className="text-center">
        <span className="text-xs tabular-nums text-emerald-600 font-medium">
          {job.accepted_count}
        </span>
      </td>
      <td className="text-center">
        <span className={cn("text-xs tabular-nums font-medium", hasFailed ? "text-red-500" : "text-yellow-600")}>
          {job.review_count}
        </span>
      </td>
      <td className="text-center">
        <span
          className={cn(
            "text-xs tabular-nums font-medium",
            hasPendingCorrections ? "text-orange-600 font-semibold" : "text-slate-400"
          )}
        >
          {hasPendingCorrections ? (
            <span className="flex items-center justify-center gap-1">
              <AlertTriangle className="h-3 w-3" />
              {job.pending_human_correction_count}
            </span>
          ) : (
            job.pending_human_correction_count
          )}
        </span>
      </td>

      {/* Created */}
      <td>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="text-xs text-slate-500 cursor-default">
              {formatRelative(job.created_at)}
            </span>
          </TooltipTrigger>
          <TooltipContent>{formatDate(job.created_at)}</TooltipContent>
        </Tooltip>
      </td>

      {/* Action */}
      <td>
        <ChevronRight className="h-3.5 w-3.5 text-slate-400" />
      </td>
    </tr>
  );
}
