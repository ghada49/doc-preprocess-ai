"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Clock, AlertTriangle, ChevronRight, RefreshCw } from "lucide-react";
import { getCorrectionQueue } from "@/lib/api/correction";
import type { MaterialType } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/shared/status-badge";
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
  workspacePath?: string; // base path for workspace links
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
        {/* Toolbar */}
        <div className="flex items-center gap-3 flex-wrap">
          <Input
            value={jobIdFilter}
            onChange={(event) => {
              setJobIdFilter(event.target.value);
              setPage(1);
            }}
            placeholder="Filter by job ID"
            className="w-[220px]"
          />
          <Input
            value={reviewReasonFilter}
            onChange={(event) => {
              setReviewReasonFilter(event.target.value);
              setPage(1);
            }}
            placeholder="Review reason"
            className="w-[180px]"
          />
          <Select
            value={materialFilter}
            onValueChange={(v) => { setMaterialFilter(v as MaterialType | "all"); setPage(1); }}
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
            <div className="flex items-center gap-1.5 text-xs text-orange-700 bg-orange-50 border border-orange-200 rounded-lg px-3 py-1.5">
              <AlertTriangle className="h-3.5 w-3.5" />
              <span className="font-medium">{total} page{total !== 1 ? "s" : ""} awaiting review</span>
            </div>
          )}

          <Button
            variant="ghost"
            size="icon"
            onClick={() => refetch()}
            className="h-9 w-9 text-slate-500 ml-auto"
          >
            <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          </Button>
        </div>

        {/* Table */}
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th className="w-16">Preview</th>
                <th>Job / Page</th>
                <th>Material</th>
                <th>Review Reasons</th>
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
                        <Skeleton className={`h-4`} style={{ width: w }} />
                      </td>
                    ))}
                  </tr>
                ))
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={6} className="p-0">
                    <EmptyState
                      title="Queue is empty"
                      description="No pages currently awaiting human correction."
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
                      className="cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      {/* Thumbnail */}
                      <td className="px-3 py-2">
                        <ArtifactImage
                          uri={item.output_image_uri}
                          containerClassName="h-12 w-10 rounded border border-slate-200"
                          className="rounded object-cover"
                          fallbackText=""
                        />
                      </td>

                      {/* Job / Page */}
                      <td>
                        <div className="flex flex-col gap-0.5">
                          <code className="text-xs text-indigo-600 font-mono">
                            {truncateId(item.job_id, 8)}…
                          </code>
                          <span className="text-xs text-slate-500">
                            Page {item.page_number}
                            {item.sub_page_index != null && ` · sub ${item.sub_page_index}`}
                          </span>
                        </div>
                      </td>

                      {/* Material */}
                      <td>
                        <span className="text-xs text-slate-500 capitalize">
                          {item.material_type}
                        </span>
                      </td>

                      {/* Reasons */}
                      <td>
                        <div className="flex flex-wrap gap-1">
                          {item.review_reasons.map((r) => (
                            <span
                              key={r}
                              className="inline-flex items-center rounded px-1.5 py-0.5 text-2xs font-medium bg-orange-50 text-orange-700 border border-orange-200"
                            >
                              {reviewReasonLabel(r)}
                            </span>
                          ))}
                        </div>
                      </td>

                      {/* Waiting */}
                      <td>
                        {item.waiting_since ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="flex items-center gap-1.5 text-xs text-slate-500 cursor-default">
                                <Clock className="h-3 w-3" />
                                {formatRelative(item.waiting_since)}
                              </span>
                            </TooltipTrigger>
                            <TooltipContent>{formatDate(item.waiting_since)}</TooltipContent>
                          </Tooltip>
                        ) : (
                          <span className="text-xs text-slate-300">—</span>
                        )}
                      </td>

                      {/* CTA */}
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
      </div>
    </TooltipProvider>
  );
}
