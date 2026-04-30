import { apiGet, apiPost } from "./client";
import type { ManualRetrainingResponse, RetrainingStatusResponse } from "@/types/api";

export function getRetrainingStatus(): Promise<RetrainingStatusResponse> {
  return apiGet<RetrainingStatusResponse>("/v1/retraining/status");
}

export function triggerManualRetraining(): Promise<ManualRetrainingResponse> {
  return apiPost<ManualRetrainingResponse>("/v1/retraining/trigger", {
    reason: "Manual retraining requested from admin UI",
  });
}
