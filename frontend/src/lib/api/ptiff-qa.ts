import { apiGet, apiPost } from "./client";
import type {
  PtiffQaResponse,
  PtiffApproveAllResponse,
  PtiffApprovePageResponse,
  PtiffEditPageResponse,
} from "@/types/api";

export function getPtiffQa(jobId: string): Promise<PtiffQaResponse> {
  return apiGet<PtiffQaResponse>(`/v1/jobs/${jobId}/ptiff-qa`);
}

export function approveAllPtiffQa(jobId: string): Promise<PtiffApproveAllResponse> {
  return apiPost<PtiffApproveAllResponse>(`/v1/jobs/${jobId}/ptiff-qa/approve-all`);
}

export function approvePtiffQaPage(
  jobId: string,
  pageNumber: number,
  subPageIndex?: number
): Promise<PtiffApprovePageResponse> {
  return apiPost<PtiffApprovePageResponse>(
    `/v1/jobs/${jobId}/pages/${pageNumber}/ptiff-qa/approve`,
    undefined,
    {
      params:
        subPageIndex != null ? { sub_page_index: subPageIndex } : undefined,
    }
  );
}

export function editPtiffQaPage(
  jobId: string,
  pageNumber: number,
  subPageIndex?: number
): Promise<PtiffEditPageResponse> {
  // Use canonical /edit route
  return apiPost<PtiffEditPageResponse>(
    `/v1/jobs/${jobId}/pages/${pageNumber}/ptiff-qa/edit`,
    undefined,
    {
      params:
        subPageIndex != null ? { sub_page_index: subPageIndex } : undefined,
    }
  );
}
