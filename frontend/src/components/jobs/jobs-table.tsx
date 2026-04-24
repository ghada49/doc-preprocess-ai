"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  Search,
  ChevronRight,
  AlertTriangle,
  RefreshCw,
  FileText,
  CheckCircle,
  Clock,
  XCircle,
} from "lucide-react";
import { listJobs } from "@/lib/api/jobs";
import type { JobSummary, JobStatus, PipelineMode } from "@/types/api";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/shared/status-badge";
import { Pagination } from "@/components/shared/pagination";
import { EmptyState } from "@/components/shared/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
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
  basePath?: string;
}

export function JobsTable({ isAdmin = false, basePath = "/jobs" }: JobsTableProps) {
  const router = useRouter();

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<JobStatus | "all">("all");
  const [pipelineFilter, setPipelineFilter] = useState<PipelineMode | "all">("all");
  const [page, setPage] = useState(1);
  const pageSize = 25;

  const { data, isLoading, isFetching, isError, refetch } = useQuery({
    queryKey: ["jobs", { search, statusFilter, pipelineFilter, page, pageSize, isAdmin }],
    queryFn: () =>
      listJobs({
        search: search || undefined,
        status: statusFilter !== "all" ? statusFilter : undefined,
        pipeline_mode: isAdmin && pipelineFilter !== "all" ? pipelineFilter : undefined,
        page,
        page_size: pageSize,
      }),
    staleTime: 15_000,
    refetchInterval: 20_000,
    retry: 1,
  });

  const jobs = data?.items ?? [];
  const total = data?.total ?? 0;
  const columnCount = isAdmin ? 12 : 10;

  return (
    <TooltipProvider>
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-[220px] flex-1 max-w-md">
            <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
            <Input
              placeholder={isAdmin ? "Search job ID or collection..." : "Search uploads or collections..."}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
              className="pl-9"
            />
          </div>

          <Select
            value={statusFilter}
            onValueChange={(v) => {
              setStatusFilter(v as JobStatus | "all");
              setPage(1);
            }}
          >
            <SelectTrigger className="w-40">
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              <SelectItem value="queued">Waiting</SelectItem>
              <SelectItem value="running">Processing</SelectItem>
              <SelectItem value="done">Ready</SelectItem>
              <SelectItem value="failed">Issue found</SelectItem>
            </SelectContent>
          </Select>

          {isAdmin && (
            <Select
              value={pipelineFilter}
              onValueChange={(v) => {
                setPipelineFilter(v as PipelineMode | "all");
                setPage(1);
              }}
            >
              <SelectTrigger className="w-36">
                <SelectValue placeholder="Pipeline" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All pipelines</SelectItem>
                <SelectItem value="layout">Layout</SelectItem>
                <SelectItem value="layout_with_ocr">Layout + OCR</SelectItem>
                <SelectItem value="preprocess">Preprocess</SelectItem>
              </SelectContent>
            </Select>
          )}

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

        {!isAdmin && (
          <div className="space-y-4">
            {isLoading ? (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {Array.from({ length: 6 }).map((_, index) => (
                  <div key={index} className="surface-panel p-5">
                    <Skeleton className="mb-5 h-5 w-28" />
                    <Skeleton className="mb-3 h-6 w-44" />
                    <Skeleton className="mb-5 h-4 w-full" />
                    <div className="grid grid-cols-3 gap-2">
                      <Skeleton className="h-14 w-full" />
                      <Skeleton className="h-14 w-full" />
                      <Skeleton className="h-14 w-full" />
                    </div>
                  </div>
                ))}
              </div>
            ) : isError ? (
              <div className="surface-panel">
                <EmptyState
                  title="Could not load uploads"
                  description="Check your connection and try refreshing."
                />
              </div>
            ) : jobs.length === 0 ? (
              <div className="surface-panel">
                <EmptyState
                  icon={FileText}
                  title={search || statusFilter !== "all" ? "No uploads found" : "No uploads yet"}
                  description={
                    search || statusFilter !== "all"
                      ? "Try clearing your filters."
                      : "Upload scanned pages to start processing."
                  }
                  action={
                    !search && statusFilter === "all"
                      ? { label: "Upload documents", onClick: () => router.push("/submit") }
                      : undefined
                  }
                />
              </div>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {jobs.map((job) => (
                  <JobCard
                    key={job.job_id}
                    job={job}
                    onClick={() => router.push(`${basePath}/${job.job_id}`)}
                  />
                ))}
              </div>
            )}

            {total > 0 && (
              <div className="surface-panel px-4 py-3">
                <Pagination
                  page={page}
                  pageSize={pageSize}
                  total={total}
                  onPageChange={setPage}
                />
              </div>
            )}
          </div>
        )}

        {isAdmin && (
        <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm shadow-slate-200/70">
          <div className="overflow-x-auto">
            <table className="w-full data-table">
              <thead>
                <tr>
                  <th>{isAdmin ? "Job" : "Upload"}</th>
                  <th>Collection</th>
                  <th>Material</th>
                  {isAdmin && <th>Pipeline</th>}
                  {isAdmin && <th>Owner</th>}
                  <th>Status</th>
                  <th className="text-center">Pages</th>
                  <th className="text-center">Ready</th>
                  <th className="text-center">Needs review</th>
                  <th className="text-center">Issues</th>
                  <th>Created</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  Array.from({ length: 8 }).map((_, i) => (
                    <tr key={i} className="border-b border-slate-100">
                      {Array.from({ length: columnCount }).map((_, j) => (
                        <td key={j} className="px-4 py-3.5">
                          <Skeleton className="h-4 w-full" />
                        </td>
                      ))}
                    </tr>
                  ))
                ) : isError ? (
                  <tr>
                    <td colSpan={columnCount} className="p-0">
                      <EmptyState
                        title={isAdmin ? "Could not load jobs" : "Could not load uploads"}
                        description="Check your connection and try refreshing."
                      />
                    </td>
                  </tr>
                ) : jobs.length === 0 ? (
                  <tr>
                    <td colSpan={columnCount} className="p-0">
                      <EmptyState
                        title={search || statusFilter !== "all" ? "No uploads found" : "No uploads yet"}
                        description={
                          search || statusFilter !== "all"
                            ? "Try clearing your filters."
                            : "Upload scanned pages to start processing."
                        }
                        action={
                          !isAdmin && !search && statusFilter === "all"
                            ? { label: "Upload documents", onClick: () => router.push("/submit") }
                            : undefined
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
        )}
      </div>
    </TooltipProvider>
  );
}

function JobCard({ job, onClick }: { job: JobSummary; onClick: () => void }) {
  const hasPendingReview = job.pending_human_correction_count > 0;
  const hasIssues = job.failed_count > 0;
  const activePages = Math.max(
    0,
    job.page_count - job.accepted_count - job.pending_human_correction_count - job.failed_count
  );
  const readyWidth = job.page_count > 0 ? (job.accepted_count / job.page_count) * 100 : 0;
  const reviewWidth =
    job.page_count > 0 ? (job.pending_human_correction_count / job.page_count) * 100 : 0;
  const issueWidth = job.page_count > 0 ? (job.failed_count / job.page_count) * 100 : 0;

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "surface-panel group flex min-h-[228px] flex-col p-5 text-left transition-all duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_24px_70px_-42px_rgba(15,23,42,0.42)]",
        hasPendingReview && "ring-1 ring-amber-200",
        hasIssues && "ring-1 ring-rose-200"
      )}
    >
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-slate-200 bg-slate-50 text-slate-500 shadow-sm">
            <FileText className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-slate-950">
              {job.collection_id || `Upload ${truncateId(job.job_id, 6)}`}
            </p>
            <p className="mt-0.5 text-2xs uppercase tracking-wider text-slate-400">
              {job.page_count} page{job.page_count !== 1 ? "s" : ""} - {job.material_type}
            </p>
          </div>
        </div>
        <StatusBadge status={job.status} type="job" />
      </div>

      <div className="mb-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-2xs font-medium text-slate-500">Progress</span>
          <span className="text-2xs text-slate-400">{formatRelative(job.created_at)}</span>
        </div>
        <div className="flex h-2 overflow-hidden rounded-full bg-slate-100">
          <div className="bg-emerald-500" style={{ width: `${readyWidth}%` }} />
          <div className="bg-amber-400" style={{ width: `${reviewWidth}%` }} />
          <div className="bg-rose-500" style={{ width: `${issueWidth}%` }} />
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <MiniMetric
          icon={<CheckCircle className="h-3.5 w-3.5" />}
          label="Ready"
          value={job.accepted_count}
          className="text-emerald-700"
        />
        <MiniMetric
          icon={<AlertTriangle className="h-3.5 w-3.5" />}
          label="Review"
          value={job.pending_human_correction_count}
          className={hasPendingReview ? "text-amber-700" : "text-slate-500"}
        />
        <MiniMetric
          icon={<XCircle className="h-3.5 w-3.5" />}
          label="Issues"
          value={job.failed_count}
          className={hasIssues ? "text-rose-700" : "text-slate-500"}
        />
      </div>

      <div className="mt-auto flex items-center justify-between pt-4">
        <div className="flex items-center gap-1.5 text-xs text-slate-500">
          <Clock className="h-3.5 w-3.5 text-slate-400" />
          {activePages > 0 ? `${activePages} processing` : "No pages processing"}
        </div>
        <span className="inline-flex items-center gap-1 text-xs font-semibold text-slate-700 transition-colors group-hover:text-slate-950">
          Open
          <ChevronRight className="h-3.5 w-3.5" />
        </span>
      </div>
    </button>
  );
}

function MiniMetric({
  icon,
  label,
  value,
  className,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  className?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-2">
      <div className={cn("flex items-center gap-1.5 text-sm font-semibold tabular-nums", className)}>
        {icon}
        {value}
      </div>
      <div className="mt-1 text-2xs font-medium text-slate-400">{label}</div>
    </div>
  );
}

function JobRow({
  job,
  isAdmin,
  onClick,
}: {
  job: JobSummary;
  isAdmin: boolean;
  onClick: () => void;
}) {
  const hasPendingReview = job.pending_human_correction_count > 0;
  const hasIssues = job.failed_count > 0;

  return (
    <tr
      onClick={onClick}
      className={cn(
        "cursor-pointer transition-colors",
        hasPendingReview && "bg-orange-50/70 hover:bg-orange-50",
        !hasPendingReview && "hover:bg-slate-50"
      )}
    >
      <td>
        <div className="flex items-center gap-2">
          {hasPendingReview && (
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-orange-400 animate-pulse-slow" />
          )}
          <span className={cn("text-xs font-medium", isAdmin ? "font-mono text-indigo-600" : "text-slate-700")}>
            {isAdmin ? `${truncateId(job.job_id, 8)}...` : `Upload ${truncateId(job.job_id, 6)}`}
          </span>
        </div>
      </td>

      <td>
        <span className="block max-w-[140px] truncate text-xs font-medium text-slate-700">
          {job.collection_id}
        </span>
      </td>

      <td>
        <span className="text-xs capitalize text-slate-500">{job.material_type}</span>
      </td>

      {isAdmin && (
        <td>
          <span className="text-xs capitalize text-slate-500">{job.pipeline_mode}</span>
        </td>
      )}

      {isAdmin && (
        <td>
          <span className="text-xs text-slate-500">
            {job.created_by_username ?? <span className="text-slate-400 italic">unknown</span>}
          </span>
        </td>
      )}

      <td>
        <StatusBadge status={job.status} type="job" />
      </td>

      <td className="text-center">
        <span className="text-xs font-medium tabular-nums text-slate-700">
          {job.page_count}
        </span>
      </td>
      <td className="text-center">
        <span className="text-xs font-medium tabular-nums text-emerald-600">
          {job.accepted_count}
        </span>
      </td>
      <td className="text-center">
        <span
          className={cn(
            "text-xs font-medium tabular-nums",
            hasPendingReview ? "font-semibold text-orange-600" : "text-slate-400"
          )}
        >
          {hasPendingReview ? (
            <span className="flex items-center justify-center gap-1">
              <AlertTriangle className="h-3 w-3" />
              {job.pending_human_correction_count}
            </span>
          ) : (
            job.pending_human_correction_count
          )}
        </span>
      </td>
      <td className="text-center">
        <span
          className={cn(
            "text-xs font-medium tabular-nums",
            hasIssues ? "font-semibold text-red-500" : "text-slate-400"
          )}
        >
          {job.failed_count}
        </span>
      </td>

      <td>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="cursor-default text-xs text-slate-500">
              {formatRelative(job.created_at)}
            </span>
          </TooltipTrigger>
          <TooltipContent>{formatDate(job.created_at)}</TooltipContent>
        </Tooltip>
      </td>

      <td>
        <ChevronRight className="h-3.5 w-3.5 text-slate-400" />
      </td>
    </tr>
  );
}
