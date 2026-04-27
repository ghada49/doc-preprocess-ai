import { apiGet } from "./client";
import type {
  DashboardSummary,
  DeploymentStatusResponse,
  PromotionAuditResponse,
  QueueStatusResponse,
  ServiceHealthResponse,
  ServiceInventoryResponse,
  ModelGateComparisonsResponse,
} from "@/types/api";

export function getDashboardSummary(): Promise<DashboardSummary> {
  return apiGet<DashboardSummary>("/v1/admin/dashboard-summary");
}

export function getServiceHealth(windowHours = 24): Promise<ServiceHealthResponse> {
  return apiGet<ServiceHealthResponse>(`/v1/admin/service-health?window_hours=${windowHours}`);
}

export function getQueueStatus(): Promise<QueueStatusResponse> {
  return apiGet<QueueStatusResponse>("/v1/admin/queue-status");
}

export function getServiceInventory(): Promise<ServiceInventoryResponse> {
  return apiGet<ServiceInventoryResponse>("/v1/admin/service-inventory");
}

export function getDeploymentStatus(): Promise<DeploymentStatusResponse> {
  return apiGet<DeploymentStatusResponse>("/v1/admin/deployment-status");
}

export function getModelGateComparisons(params?: {
  job_id?: string;
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<ModelGateComparisonsResponse> {
  const q = new URLSearchParams();
  if (params?.job_id) q.set("job_id", params.job_id);
  if (params?.status) q.set("status", params.status);
  if (params?.limit != null) q.set("limit", String(params.limit));
  if (params?.offset != null) q.set("offset", String(params.offset));
  const qs = q.toString();
  return apiGet<ModelGateComparisonsResponse>(`/v1/admin/model-gate-comparisons${qs ? `?${qs}` : ""}`);
}

export function getPromotionAudit(params?: {
  service?: string;
  action?: string;
  limit?: number;
  offset?: number;
}): Promise<PromotionAuditResponse> {
  const q = new URLSearchParams();
  if (params?.service) q.set("service", params.service);
  if (params?.action) q.set("action", params.action);
  if (params?.limit != null) q.set("limit", String(params.limit));
  if (params?.offset != null) q.set("offset", String(params.offset));
  const qs = q.toString();
  return apiGet<PromotionAuditResponse>(`/v1/admin/promotion-audit${qs ? `?${qs}` : ""}`);
}
