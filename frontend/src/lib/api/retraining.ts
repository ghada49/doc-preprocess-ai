import { apiGet } from "./client";
import type { RetrainingStatusResponse } from "@/types/api";

export function getRetrainingStatus(): Promise<RetrainingStatusResponse> {
  return apiGet<RetrainingStatusResponse>("/v1/retraining/status");
}
