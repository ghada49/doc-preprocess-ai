"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Clock, AlertTriangle, ChevronRight, RefreshCw, FileSearch } from "lucide-react";
import { getCorrectionQueue } from "@/lib/api/correction";
import type { CorrectionQueueItem, MaterialType } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/shared/empty-state";
import { Pagination } from "@/components/shared/pagination";
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
import { ArtifactImage } from "@/components/shared/artifact-image";
import { reviewReasonLabel, formatRelative, formatDate, truncateId } from "@/lib/utils";
import { cn } from "@/lib/utils";

interface CorrectionQueueTableProps {
  isAdmin?: boolean;
  workspacePath?: string;
}

export function CorrectionQueueTable({
  isAdmin = false,
  workspacePath = "/queue",
}: CorrectionQueueTableProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [materialFilter, setMaterialFilter] = useState<MaterialType | "all">("all");
  const [jobIdFilter, setJobIdFilter] = useState("");
  const [reviewReasonFilter, setReviewReasonFilter] = useState("");
  const [page, setPage] = useState(1);
  const pageSize = 20;

  useEffect(() => {
    setJobIdFilter(searchParams.get("job_id") ?? "");
    setReviewReasonFilter(searchParams.get("review_reason") ?? "");
    setPage(1);
  }, [searchParams]);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: [
      "correction-queue",
      { materialFilter, jobIdFilter, reviewReasonFilter, page, pageSize },
    ],
    queryFn: () =>
      getCorrectionQueue({
        job_id: jobIdFilter || undefined,
        material_type: materialFilter !== "all" ? materialFilter : undefined,
        review_reason: reviewReasonFilter || undefined,
        page,
        page_size: pageSize,
      }),
    staleTime: 10_000,
    refetchInterval: 15_000,
  });

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <TooltipProvider>
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-3">
          <Input
            value={jobIdFilter}
            onChange={(event) => {
              setJobIdFilter(event.target.value);
              setPage(1);
            }}
            placeholder={isAdmin ? "Filter by job ID" : "Filter by upload"}
            className="w-[220px]"
          />
          {isAdmin && (
            <Input
              value={reviewReasonFilter}
              onChange={(event) => {
                setReviewReasonFilter(event.target.value);
                setPage(1);
              }}
              placeholder="Review reason code"
              className="w-[180px]"
            />
          )}
          <Select
            value={materialFilter}
            onValueChange={(v) => {
              setMaterialFilter(v as MaterialType | "all");
              setPage(1);
            }}
          >
            <SelectTrigger className="w-44">
              <SelectValue placeholder="Material type" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All materials</SelectItem>
              <SelectItem value="book">Book</SelectItem>
              <SelectItem value="newspaper">Newspaper</SelectItem>
              <SelectItem value="microfilm">Microfilm</SelectItem>
            </SelectContent>
          </Select>

          {total > 0 && (
            <div className="flex items-center gap-1.5 rounded-full border border-orange-200 bg-orange-50 px-3 py-1.5 text-xs text-orange-700">
              <AlertTriangle className="h-3.5 w-3.5" />
              <span className="font-medium">
                {total} page{total !== 1 ? "s" : ""} need review
              </span>
            </div>
          )}

          <Button
            variant="ghost"
            size="icon"
            onClick={() => refetch()}
            className="ml-auto h-9 w-9 text-slate-500"
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
                  <div key={index} className="surface-panel grid grid-cols-[72px_1fr] gap-4 p-4">
                    <Skeleton className="h-24 w-[72px] rounded-xl" />
                    <div className="space-y-3 py-1">
                      <Skeleton className="h-5 w-24" />
                      <Skeleton className="h-4 w-40" />
                      <Skeleton className="h-4 w-full" />
                      <Skeleton className="h-8 w-28" />
                    </div>
                  </div>
                ))}
              </div>
            ) : items.length === 0 ? (
              <div className="surface-panel">
                <EmptyState
                  icon={FileSearch}
                  title="No pages need review"
                  description="Pages that need attention will appear here with a clear next step."
                />
              </div>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {items.map((item) => {
                  const subPageQuery =
                    item.sub_page_index != null
                      ? `?sub_page_index=${item.sub_page_index}`
                      : "";
                  const workspaceHref = `${workspacePath}/${item.job_id}/${item.page_number}/workspace${subPageQuery}`;

                  return (
                    <ReviewCard
                      key={`${item.job_id}-${item.page_number}-${item.sub_page_index ?? 0}`}
                      item={item}
                      onClick={() => router.push(workspaceHref)}
                    />
                  );
                })}
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
          <table className="w-full data-table">
            <thead>
              <tr>
                <th className="w-16">Preview</th>
                <th>{isAdmin ? "Job / Page" : "Upload / Page"}</th>
                <th>Material</th>
                <th>Issue</th>
                <th>Waiting</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                Array.from({ length: 6 }).map((_, i) => (
                  <tr key={i} className="border-b border-slate-100">
                    {[16, 180, 80, 220, 100, 40].map((w, j) => (
                      <td key={j} className="px-4 py-3.5">
                        <Skeleton className="h-4" style={{ width: w }} />
                      </td>
                    ))}
                  </tr>
                ))
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={6} className="p-0">
                    <EmptyState
                      title="No pages need review"
                      description="When a page needs attention, it will appear here with a clear next step."
                    />
                  </td>
                </tr>
              ) : (
                items.map((item) => {
                  const subPageQuery =
                    item.sub_page_index != null
                      ? `?sub_page_index=${item.sub_page_index}`
                      : "";
                  const workspaceHref = `${workspacePath}/${item.job_id}/${item.page_number}/workspace${subPageQuery}`;
                  return (
                    <tr
                      key={`${item.job_id}-${item.page_number}-${item.sub_page_index ?? 0}`}
                      onClick={() => router.push(workspaceHref)}
                      className="cursor-pointer transition-colors hover:bg-slate-50"
                    >
                      <td className="px-3 py-2">
                        <ArtifactImage
                          uri={item.output_image_uri}
                          containerClassName="h-12 w-10 rounded border border-slate-200"
                          className="rounded object-cover"
                          fallbackText=""
                        />
                      </td>

                      <td>
                        <div className="flex flex-col gap-0.5">
                          <span className="text-xs font-medium text-slate-700">
                            {isAdmin ? truncateId(item.job_id, 8) : `Upload ${truncateId(item.job_id, 6)}`}
                          </span>
                          <span className="text-xs text-slate-500">
                            Page {item.page_number}
                            {item.sub_page_index != null && (
                              <span className="ml-1 text-2xs font-medium text-indigo-500">
                                {item.sub_page_index === 0 ? "Left page" : "Right page"}
                              </span>
                            )}
                          </span>
                        </div>
                      </td>

                      <td>
                        <span className="text-xs capitalize text-slate-500">
                          {item.material_type}
                        </span>
                      </td>

                      <td>
                        <div className="flex flex-wrap gap-1">
                          {item.review_reasons.length > 0 ? (
                            item.review_reasons.map((r) => (
                              <span
                                key={r}
                                className="inline-flex items-center rounded-full border border-orange-200 bg-orange-50 px-2 py-0.5 text-2xs font-medium text-orange-700"
                              >
                                {reviewReasonLabel(r)}
                              </span>
                            ))
                          ) : (
                            <span className="text-xs text-slate-500">Please review this page</span>
                          )}
                        </div>
                      </td>

                      <td>
                        {item.waiting_since ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="flex cursor-default items-center gap-1.5 text-xs text-slate-500">
                                <Clock className="h-3 w-3" />
                                {formatRelative(item.waiting_since)}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent>{formatDate(item.waiting_since)}</TooltipContent>
                          </Tooltip>
                        ) : (
                          <span className="text-xs text-slate-300">-</span>
                        )}
                      </td>

                      <td>
                        <ChevronRight className="h-3.5 w-3.5 text-slate-400" />
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>

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

function ReviewCard({
  item,
  onClick,
}: {
  item: CorrectionQueueItem;
  onClick: () => void;
}) {
  const splitLabel =
    item.sub_page_index == null ? null : item.sub_page_index === 0 ? "Left page" : "Right page";

  return (
    <button
      type="button"
      onClick={onClick}
      className="surface-panel group grid min-h-[176px] grid-cols-[72px_1fr] gap-4 p-4 text-left transition-all duration-200 hover:-translate-y-0.5 hover:border-amber-200 hover:shadow-[0_24px_70px_-42px_rgba(180,83,9,0.35)]"
    >
      <ArtifactImage
        uri={item.output_image_uri}
        containerClassName="h-24 w-[72px] rounded-xl border border-slate-200 bg-slate-50 shadow-sm"
        className="rounded-xl object-cover"
        fallbackText=""
      />

      <div className="flex min-w-0 flex-col">
        <div className="mb-2 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-slate-950">
              Upload {truncateId(item.job_id, 6)}
            </p>
            <p className="mt-0.5 text-xs text-slate-500">
              Page {item.page_number}
              {splitLabel ? ` - ${splitLabel}` : ""}
            </p>
          </div>
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-2xs font-semibold text-amber-700 shadow-sm shadow-amber-100/60">
            <AlertTriangle className="h-3 w-3" />
            Review
          </span>
        </div>

        <div className="mb-3 flex flex-wrap gap-1.5">
          {item.review_reasons.length > 0 ? (
            item.review_reasons.slice(0, 3).map((reason) => (
              <span
                key={reason}
                className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-2xs font-medium text-slate-600"
              >
                {reviewReasonLabel(reason)}
              </span>
            ))
          ) : (
            <span className="text-xs text-slate-500">Please review this page.</span>
          )}
        </div>

        <div className="mt-auto flex items-center justify-between gap-3">
          <div className="min-w-0 text-xs text-slate-500">
            <span className="capitalize">{item.material_type}</span>
            {item.waiting_since && (
              <span className="ml-2 inline-flex items-center gap-1.5">
                <Clock className="h-3 w-3 text-slate-400" />
                {formatRelative(item.waiting_since)}
              </span>
            )}
          </div>
          <span className="inline-flex items-center gap-1 text-xs font-semibold text-slate-700 transition-colors group-hover:text-slate-950">
            Open review
            <ChevronRight className="h-3.5 w-3.5" />
          </span>
        </div>
      </div>
    </button>
  );
}
