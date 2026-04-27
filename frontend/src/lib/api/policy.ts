import { apiGet, apiPatch, isApiError } from "./client";
import type { PolicyRecord, UpdatePolicyRequest } from "@/types/api";

export async function getPolicy(): Promise<PolicyRecord | null> {
  try {
    return await apiGet<PolicyRecord>("/v1/policy");
  } catch (err) {
    if (isApiError(err) && err.status === 404) return null;
    throw err;
  }
}

export function updatePolicy(data: UpdatePolicyRequest): Promise<PolicyRecord> {
  return apiPatch<PolicyRecord>("/v1/policy", data);
}
