import { apiDelete, apiGet, apiPost } from "./client";
import type {
  CreateJobRequest,
  CreateJobResponse,
  JobActionResponse,
  JobDetailResponse,
  JobsListParams,
  JobsListResponse,
} from "@/types/api";

export function createJob(data: CreateJobRequest): Promise<CreateJobResponse> {
  return apiPost<CreateJobResponse>("/v1/jobs", data);
}

export function listJobs(params?: JobsListParams): Promise<JobsListResponse> {
  return apiGet<JobsListResponse>("/v1/jobs", params as Record<string, unknown>);
}

export function getJob(jobId: string): Promise<JobDetailResponse> {
  return apiGet<JobDetailResponse>(`/v1/jobs/${jobId}`);
}

export function cancelJob(jobId: string): Promise<JobActionResponse> {
  return apiPost<JobActionResponse>(`/v1/jobs/${jobId}/cancel`);
}

export function deleteJob(jobId: string): Promise<JobActionResponse> {
  return apiDelete<JobActionResponse>(`/v1/jobs/${jobId}`);
}
