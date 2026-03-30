import { apiGet } from "./client";
import type { LineageResponse } from "@/types/api";

export function getLineage(
  jobId: string,
  pageNumber: number,
  subPageIndex?: number
): Promise<LineageResponse> {
  const params = subPageIndex != null ? { sub_page_index: subPageIndex } : undefined;
  return apiGet<LineageResponse>(
    `/v1/lineage/${jobId}/${pageNumber}`,
    params as Record<string, unknown>
  );
}
