import { apiGet, apiPost, isApiError } from "./client";
import type {
  CorrectionQueueParams,
  CorrectionQueueResponse,
  CorrectionWorkspaceDetail,
  RejectPageResponse,
  SubmitCorrectionRequest,
  SubmitCorrectionResponse,
} from "@/types/api";

interface CorrectionActionOptions {
  subPageIndex?: number;
  notes?: string | null;
}

export function getCorrectionQueue(
  params?: CorrectionQueueParams
): Promise<CorrectionQueueResponse> {
  return apiGet<CorrectionQueueResponse>(
    "/v1/correction-queue",
    params as Record<string, unknown>
  );
}

export function getCorrectionWorkspace(
  jobId: string,
  pageNumber: number,
  subPageIndex?: number
): Promise<CorrectionWorkspaceDetail> {
  const params =
    subPageIndex != null ? { sub_page_index: subPageIndex } : undefined;

  return apiGet<CorrectionWorkspaceDetail>(
    `/v1/correction-queue/${jobId}/${pageNumber}`,
    params as Record<string, unknown>
  );
}

export function submitCorrection(
  jobId: string,
  pageNumber: number,
  data: SubmitCorrectionRequest,
  options?: CorrectionActionOptions
): Promise<SubmitCorrectionResponse> {
  const params =
    options?.subPageIndex != null
      ? { sub_page_index: options.subPageIndex }
      : undefined;

  const body = {
    ...data,
    notes: options?.notes?.trim() ? options.notes.trim() : undefined,
  };

  return apiPost<SubmitCorrectionResponse>(
    `/v1/jobs/${jobId}/pages/${pageNumber}/correction`,
    body,
    { params }
  );
}

export async function rejectPage(
  jobId: string,
  pageNumber: number,
  options?: CorrectionActionOptions
): Promise<RejectPageResponse> {
  const params =
    options?.subPageIndex != null
      ? { sub_page_index: options.subPageIndex }
      : undefined;

  const body = options?.notes?.trim()
    ? { notes: options.notes.trim() }
    : undefined;

  try {
    return await apiPost<RejectPageResponse>(
      `/v1/jobs/${jobId}/pages/${pageNumber}/correction/reject`,
      body,
      { params }
    );
  } catch (error) {
    if (!isApiError(error) || error.status !== 404) {
      throw error;
    }

    return apiPost<RejectPageResponse>(
      `/v1/jobs/${jobId}/pages/${pageNumber}/correction-reject`,
      body,
      { params }
    );
  }
}
