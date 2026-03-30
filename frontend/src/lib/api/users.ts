import { apiGet, apiPost, apiPatch } from "./client";
import type { UserRecord, CreateUserRequest, UsersListResponse } from "@/types/api";

export async function listUsers(): Promise<UsersListResponse> {
  const response = await apiGet<UserRecord[] | UsersListResponse>("/v1/users");

  if (Array.isArray(response)) {
    return {
      total: response.length,
      items: response,
    };
  }

  return response;
}

export function createUser(data: CreateUserRequest): Promise<UserRecord> {
  return apiPost<UserRecord>("/v1/users", data);
}

export function deactivateUser(userId: string): Promise<UserRecord> {
  return apiPatch<UserRecord>(`/v1/users/${userId}/deactivate`);
}
