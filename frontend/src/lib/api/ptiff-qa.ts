import { apiGet, apiPost } from "./client";
import type {
  PtiffQaResponse,
  PtiffApproveAllResponse,
  PtiffApprovePageResponse,
  PtiffEditPageResponse,
  PtiffQaViewerResponse,
  FlagPageResponse,
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

/**
 * GET /v1/jobs/{job_id}/ptiff-qa/viewer
 * Returns a single page's PTIFF preview URL + prev/next navigation.
 * Pass page_number + sub_page_index to navigate to a specific page;
 * omit both to get the first page.
 */
export function getViewerPage(
  jobId: string,
  pageNumber?: number,
  subPageIndex?: number | null
): Promise<PtiffQaViewerResponse> {
  const params: Record<string, number> = {};
  if (pageNumber != null) params.page_number = pageNumber;
  if (subPageIndex != null) params.sub_page_index = subPageIndex;
  return apiGet<PtiffQaViewerResponse>(
    `/v1/jobs/${jobId}/ptiff-qa/viewer`,
    Object.keys(params).length > 0 ? params : undefined
  );
}

/**
 * POST /v1/jobs/{job_id}/pages/{page_number}/ptiff-qa/flag
 * Flag an accepted or ptiff_qa_pending page for human correction.
 * Transitions the page to pending_human_correction.
 */
export function flagPageForCorrection(
  jobId: string,
  pageNumber: number,
  subPageIndex?: number | null
): Promise<FlagPageResponse> {
  return apiPost<FlagPageResponse>(
    `/v1/jobs/${jobId}/pages/${pageNumber}/ptiff-qa/flag`,
    undefined,
    {
      params:
        subPageIndex != null ? { sub_page_index: subPageIndex } : undefined,
    }
  );
}
