import { apiGet, apiPost } from "./client";
import type {
  CreateJobRequest,
  CreateJobResponse,
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
