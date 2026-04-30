"use client";

import { Fragment, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import {
  RefreshCw,
  FileSearch,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Clock,
  Download,
  Eye,
  LayoutGrid,
  ChevronDown,
  ChevronRight,
  Ban,
  Trash2,
} from "lucide-react";
import { cancelJob, deleteJob, getJob } from "@/lib/api/jobs";
import { downloadJobOutputZip } from "@/lib/api/download";
import { UserShell } from "@/components/layout/user-shell";
import { LayoutOverlay } from "@/components/jobs/layout-overlay";
import { PageHeader } from "@/components/shared/page-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { ArtifactImage } from "@/components/shared/artifact-image";
import { ArtifactLinkButton } from "@/components/shared/artifact-link-button";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBanner } from "@/components/shared/error-banner";
import { ConfirmModal } from "@/components/shared/confirm-modal";
import { Spinner } from "@/components/ui/spinner";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  formatDate,
  formatDuration,
  truncateId,
  hasActivePages,
  isJobActive,
  reviewReasonLabel,
} from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { JobPage } from "@/types/api";

export default function JobDetailPage() {
  const { job_id } = useParams<{ job_id: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [isDownloading, setIsDownloading] = useState(false);
  const [pendingAction, setPendingAction] = useState<"cancel" | "delete" | null>(null);
  const [expandedLayoutKey, setExpandedLayoutKey] = useState<string | null | undefined>(
    undefined
  );

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["job", job_id],
    queryFn: () => getJob(job_id),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 8_000;
      const operationalPages = filterOperationalPages(data.pages ?? []);
      const active =
        isJobActive(data.summary.status) ||
        (operationalPages.length > 0 && hasActivePages(operationalPages));
      return active ? 6_000 : false;
    },
  });

  const pages = data?.pages ?? [];
  const operationalPages = filterOperationalPages(pages);
  const supportsLayoutResults = data?.summary?.pipeline_mode !== "preprocess";

  useEffect(() => {
    setExpandedLayoutKey(undefined);
  }, [job_id]);

  useEffect(() => {
    if (!supportsLayoutResults || expandedLayoutKey !== undefined) return;
    const firstLayoutPage = operationalPages.find(
      (page) => page.output_layout_uri && (page.output_image_uri ?? page.input_image_uri)
    );
    if (!firstLayoutPage) {
      setExpandedLayoutKey(null);
      return;
    }
    setExpandedLayoutKey(`${firstLayoutPage.page_number}-${firstLayoutPage.sub_page_index ?? 0}`);
  }, [expandedLayoutKey, operationalPages, supportsLayoutResults]);

  async function handleDownloadZip() {
    setIsDownloading(true);
    try {
      await downloadJobOutputZip(job_id);
    } catch {
      alert("There was a problem downloading results. Please try again.");
    } finally {
      setIsDownloading(false);
    }
  }

  const actionMutation = useMutation({
    mutationFn: (type: "cancel" | "delete") =>
      type === "cancel" ? cancelJob(job_id) : deleteJob(job_id),
    onSuccess: (_result, type) => {
      toast.success(type === "cancel" ? "Job canceled." : "Job removed.");
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["correction-queue"] });
      setPendingAction(null);
      if (type === "delete") {
        router.push("/jobs");
      } else {
        refetch();
      }
    },
    onError: (_error, type) => {
      toast.error(
        type === "cancel"
          ? "We could not cancel this job."
          : "We could not remove this job."
      );
    },
  });

  if (isLoading) {
    return (
      <UserShell breadcrumbs={[{ label: "Documents", href: "/jobs" }, { label: "Loading..." }]}>
        <div className="flex items-center justify-center py-20">
          <Spinner size="lg" />
        </div>
      </UserShell>
    );
  }

  if (isError || !data) {
    return (
      <UserShell breadcrumbs={[{ label: "Documents", href: "/jobs" }, { label: "Not found" }]}>
        <div className="p-6">
          <ErrorBanner
            variant="fullscreen"
            title="Upload not found"
            message="This upload could not be loaded."
          />
        </div>
      </UserShell>
    );
  }

  const summary = data.summary;
  const isActive = isJobActive(summary.status) || hasActivePages(operationalPages);
  const hasSplitPages = operationalPages.some((page) => page.sub_page_index != null);

  return (
    <UserShell
        breadcrumbs={[
          { label: "Documents", href: "/jobs" },
          { label: summary.collection_id || `Upload ${truncateId(summary.job_id, 6)}` },
        ]}
      headerRight={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => refetch()}
          className="gap-1.5 text-slate-500"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} />
          {isActive && <span className="text-xs text-sky-600">Updating</span>}
        </Button>
      }
    >
      <TooltipProvider>
        <div className="relative z-10 max-w-6xl space-y-6 p-6">
          <PageHeader
            title={summary.collection_id || `Upload ${truncateId(summary.job_id, 8)}`}
            description={
              isActive
                ? "Processing runs in the background. Larger TIFF batches can take several minutes, and this page updates automatically."
                : supportsLayoutResults
                ? "Review progress, open finished pages, inspect page layout, and download results when they are ready."
                : "Review progress, open finished pages, and download results when they are ready."
            }
            icon={FileSearch}
            badge={<StatusBadge status={summary.status} type="job" />}
            actions={
              <div className="flex flex-wrap items-center gap-2">
                {summary.pending_human_correction_count > 0 && (
                  <Button
                    size="sm"
                    onClick={() => router.push(`/queue?job_id=${summary.job_id}`)}
                    className="gap-1.5"
                  >
                    <AlertTriangle className="h-3.5 w-3.5" />
                    Review {summary.pending_human_correction_count} page
                    {summary.pending_human_correction_count !== 1 ? "s" : ""}
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="outline"
                    onClick={() => router.push(`/jobs/${summary.job_id}/ptiff-qa`)}
                  className="gap-1.5"
                >
                  <Eye className="h-3.5 w-3.5" />
                  Review results
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={handleDownloadZip}
                  disabled={isDownloading}
                  className="gap-1.5"
                >
                  <Download className="h-3.5 w-3.5" />
                  {isDownloading ? "Downloading..." : "Download results"}
                </Button>
                {isActive && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setPendingAction("cancel")}
                    className="gap-1.5"
                  >
                    <Ban className="h-3.5 w-3.5" />
                    Cancel job
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => setPendingAction("delete")}
                  className="gap-1.5"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Remove
                </Button>
              </div>
            }
          />

          {summary.status === "failed" && (
            <div className="flex items-start gap-3 rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
              <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <p className="font-semibold">There was a problem processing this upload.</p>
                <p className="mt-1 text-xs leading-relaxed">
                  Please try again or review the pages marked below.
                </p>
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <SummaryCard label="Pages" value={summary.page_count} color="text-slate-900" />
            <SummaryCard
              label="Ready"
              value={summary.accepted_count}
              color="text-emerald-600"
              icon={<CheckCircle className="h-3.5 w-3.5" />}
            />
            <SummaryCard
              label="Needs review"
              value={summary.pending_human_correction_count}
              color="text-orange-600"
              icon={<AlertTriangle className="h-3.5 w-3.5" />}
            />
            <SummaryCard
              label="Issues"
              value={summary.failed_count}
              color="text-red-500"
              icon={<XCircle className="h-3.5 w-3.5" />}
            />
          </div>

          <div className="surface-panel p-5">
            <div className="grid grid-cols-2 gap-4 text-xs sm:grid-cols-4">
              <MetaField label="Collection" value={summary.collection_id} />
              <MetaField label="Material" value={summary.material_type} />
              <MetaField label="Started" value={formatDate(summary.created_at)} />
              <MetaField
                label="Completed"
                value={summary.completed_at ? formatDate(summary.completed_at) : "Still processing"}
              />
            </div>

            {summary.page_count > 0 && (
              <div className="mt-5 border-t border-slate-200 pt-4">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-2xs text-slate-500">Progress</span>
                  <span className="text-2xs text-slate-500">
                    {Math.min(
                      summary.page_count,
                      summary.accepted_count +
                        summary.review_count +
                        summary.failed_count +
                        summary.pending_human_correction_count
                    )}{" "}
                    / {summary.page_count} pages
                  </span>
                </div>
                <div className="flex h-2 overflow-hidden rounded-full bg-slate-200">
                  <div
                    className="bg-emerald-500 transition-all"
                    style={{ width: `${(summary.accepted_count / summary.page_count) * 100}%` }}
                  />
                  <div
                    className="bg-orange-400 transition-all"
                    style={{
                      width: `${(summary.pending_human_correction_count / summary.page_count) * 100}%`,
                    }}
                  />
                  <div
                    className="bg-red-500 transition-all"
                    style={{ width: `${(summary.failed_count / summary.page_count) * 100}%` }}
                  />
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-3">
                  <LegendDot color="bg-emerald-500" label={`${summary.accepted_count} ready`} />
                  {summary.pending_human_correction_count > 0 && (
                    <LegendDot color="bg-orange-400" label={`${summary.pending_human_correction_count} need review`} />
                  )}
                  {summary.failed_count > 0 && (
                    <LegendDot color="bg-red-500" label={`${summary.failed_count} issue${summary.failed_count !== 1 ? "s" : ""}`} />
                  )}
                </div>
              </div>
            )}
          </div>

          <div>
            <h2 className="mb-3 text-sm font-semibold text-slate-800">
              Pages
              <span className="ml-2 text-xs font-normal text-slate-400">
                ({operationalPages.length})
              </span>
            </h2>
            <div className="surface-panel overflow-hidden p-0">
              <table className="w-full data-table">
                <thead>
                  <tr>
                    <th className="w-14">Page</th>
                    {hasSplitPages && <th className="w-24">Split</th>}
                    <th>Status</th>
                    <th>Message</th>
                    <th>Time</th>
                    <th>Result</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {isFetching && operationalPages.length === 0 ? (
                    Array.from({ length: 6 }).map((_, index) => (
                      <tr key={index}>
                        {Array.from({ length: hasSplitPages ? 7 : 6 }).map((__, cell) => (
                          <td key={cell} className="px-4 py-3.5">
                            <Skeleton className="h-4 w-full" />
                          </td>
                        ))}
                      </tr>
                    ))
                  ) : (
                      operationalPages.map((page) => (
                      <Fragment key={`${page.page_number}-${page.sub_page_index ?? 0}`}>
                        <PageRow
                          page={page}
                          showSplitColumn={hasSplitPages}
                          isLayoutOpen={
                            expandedLayoutKey ===
                            `${page.page_number}-${page.sub_page_index ?? 0}`
                          }
                          onToggleLayout={() =>
                            setExpandedLayoutKey((current) => {
                              const nextKey = `${page.page_number}-${page.sub_page_index ?? 0}`;
                              return current === nextKey ? null : nextKey;
                            })
                          }
                          onOpenWorkspace={() =>
                            router.push(
                              `/queue/${summary.job_id}/${page.page_number}/workspace${
                                page.sub_page_index != null
                                  ? `?sub_page_index=${page.sub_page_index}`
                                  : ""
                              }`
                            )
                          }
                        />
                        {expandedLayoutKey ===
                          `${page.page_number}-${page.sub_page_index ?? 0}` &&
                          page.output_layout_uri &&
                          (page.output_image_uri ?? page.input_image_uri) && (
                            <tr className="bg-slate-50/80">
                              <td colSpan={hasSplitPages ? 7 : 6} className="px-4 py-4">
                                <LayoutOverlay
                                  imageUri={page.output_image_uri ?? page.input_image_uri}
                                  layoutUri={page.output_layout_uri}
                                  pageLabel={`Page ${page.page_number}${
                                    page.sub_page_index != null
                                      ? ` ${page.sub_page_index === 0 ? "Left" : "Right"}`
                                      : ""
                                  }`}
                                  userMode
                                />
                              </td>
                            </tr>
                          )}
                      </Fragment>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <ConfirmModal
          open={pendingAction != null}
          onOpenChange={(open) => {
            if (!open && !actionMutation.isPending) setPendingAction(null);
          }}
          title={pendingAction === "cancel" ? "Cancel this job?" : "Remove this job?"}
          description={
            pendingAction === "cancel"
              ? "Unfinished pages will stop processing and be marked as failed. Finished pages will stay available."
              : "This removes the job from the workspace, including its page records and review entries."
          }
          confirmLabel={pendingAction === "cancel" ? "Cancel job" : "Remove job"}
          variant="danger"
          loading={actionMutation.isPending}
          onConfirm={() => {
            if (pendingAction) actionMutation.mutate(pendingAction);
          }}
        />
      </TooltipProvider>
    </UserShell>
  );
}

function filterOperationalPages(pages: JobPage[]): JobPage[] {
  const pageNumbersWithChildren = new Set(
    pages.filter((page) => page.sub_page_index != null).map((page) => page.page_number)
  );
  const filtered = pages.filter((page) => {
    if (page.sub_page_index == null && pageNumbersWithChildren.has(page.page_number)) {
      return false;
    }
    return true;
  });

  return filtered.sort((a, b) => {
    if (a.page_number !== b.page_number) return a.page_number - b.page_number;
    const aOrder = a.reading_order ?? (a.sub_page_index != null ? a.sub_page_index + 1 : 0);
    const bOrder = b.reading_order ?? (b.sub_page_index != null ? b.sub_page_index + 1 : 0);
    return aOrder - bOrder;
  });
}

function PageRow({
  page,
  showSplitColumn,
  isLayoutOpen,
  onToggleLayout,
  onOpenWorkspace,
}: {
  page: JobPage;
  showSplitColumn: boolean;
  isLayoutOpen: boolean;
  onToggleLayout: () => void;
  onOpenWorkspace: () => void;
}) {
  const needsReview = page.status === "pending_human_correction";
  const hasIssue = page.status === "failed";
  const displayImageUri = page.output_image_uri ?? page.input_image_uri;
  const canInspectLayout = Boolean(page.output_layout_uri && displayImageUri);

  return (
    <tr
      className={cn(
        needsReview && "bg-orange-50/70 hover:bg-orange-50",
        hasIssue && "bg-red-50/60 hover:bg-red-50",
        !needsReview && !hasIssue && "hover:bg-slate-50"
      )}
    >
      <td>
        <span className="font-mono text-xs tabular-nums text-slate-500">
          {page.page_number}
        </span>
      </td>
      {showSplitColumn && (
        <td>
          <span className="text-xs font-medium text-slate-500">
            {page.sub_page_index == null
              ? "-"
              : page.sub_page_index === 0
                ? "Left page"
                : "Right page"}
          </span>
        </td>
      )}
      <td>
        <StatusBadge status={page.status} type="page" />
      </td>
      <td>
        <p className="max-w-sm text-xs leading-relaxed text-slate-600">
          {pageMessage(page)}
        </p>
      </td>
      <td>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="cursor-default text-xs tabular-nums text-slate-500">
              {formatDuration(page.processing_time_ms)}
            </span>
          </TooltipTrigger>
          <TooltipContent>Processing time</TooltipContent>
        </Tooltip>
      </td>
      <td>
        <div className="flex items-center gap-3">
          <ArtifactImage
            uri={page.output_image_uri}
            fallbackUri={page.input_image_uri}
            containerClassName="h-9 w-8 rounded border border-slate-200"
            className="rounded object-cover"
            fallbackText=""
          />
          <div className="flex flex-wrap items-center gap-1.5">
            <ArtifactLinkButton uri={displayImageUri} label="Open page" size="xs" />
            {canInspectLayout && (
              <Button
                size="xs"
                variant={isLayoutOpen ? "secondary" : "ghost"}
                onClick={onToggleLayout}
                className="gap-1"
              >
                {isLayoutOpen ? (
                  <ChevronDown className="h-3 w-3" />
                ) : (
                  <LayoutGrid className="h-3 w-3" />
                )}
                Layout
              </Button>
            )}
          </div>
        </div>
      </td>
      <td>
        <div className="flex flex-wrap items-center gap-1.5">
          {needsReview && (
            <Button size="xs" onClick={onOpenWorkspace} className="gap-1">
              <ChevronRight className="h-3 w-3" />
              Review
            </Button>
          )}
        </div>
      </td>
    </tr>
  );
}

function pageMessage(page: JobPage): string {
  if (page.status === "queued") {
    return "Waiting for a worker.";
  }
  if (page.status === "failed") {
    return "We could not process this page automatically.";
  }
  if (page.status === "pending_human_correction") {
    return page.review_reasons?.length
      ? page.review_reasons.map(reviewReasonLabel).join(", ")
      : "Please review this page.";
  }
  if (page.status === "accepted") {
    return "Ready to use.";
  }
  if (page.status === "review") {
    return "Reviewed.";
  }
  return "Processing this page.";
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
    <div className="surface-panel p-4">
      <p className="mb-2 text-2xs uppercase tracking-wider text-slate-500">{label}</p>
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
      <p className="mb-0.5 text-2xs text-slate-400">{label}</p>
      <p className="text-xs capitalize text-slate-700">{value}</p>
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
