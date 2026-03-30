import { apiGet } from "./client";
import type { DashboardSummary, ServiceHealthResponse } from "@/types/api";

export function getDashboardSummary(): Promise<DashboardSummary> {
  return apiGet<DashboardSummary>("/v1/admin/dashboard-summary");
}

export function getServiceHealth(): Promise<ServiceHealthResponse> {
  return apiGet<ServiceHealthResponse>("/v1/admin/service-health");
}
