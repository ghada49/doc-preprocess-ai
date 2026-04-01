import { apiGet, apiPatch } from "./client";
import type { PolicyRecord, UpdatePolicyRequest } from "@/types/api";

export function getPolicy(): Promise<PolicyRecord> {
  return apiGet<PolicyRecord>("/v1/policy");
}

export function updatePolicy(data: UpdatePolicyRequest): Promise<PolicyRecord> {
  return apiPatch<PolicyRecord>("/v1/policy", data);
}
