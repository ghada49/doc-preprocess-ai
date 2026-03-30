import { apiPost } from "./client";
import type { PresignReadRequest, PresignReadResponse } from "@/types/api";

export async function presignReadArtifact(
  uri: string,
  expiresIn = 300
): Promise<PresignReadResponse> {
  const body: PresignReadRequest = { uri, expires_in: expiresIn };
  return apiPost<PresignReadResponse>("/v1/artifacts/presign-read", body);
}

export async function presignReadUrl(
  uri: string,
  expiresIn = 300
): Promise<string> {
  const response = await presignReadArtifact(uri, expiresIn);
  return response.read_url;
}
