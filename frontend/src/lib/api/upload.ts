import { apiPost, uploadToStorage } from "./client";
import type { PresignUploadResponse } from "@/types/api";

export async function presignUpload(): Promise<PresignUploadResponse> {
  return apiPost<PresignUploadResponse>("/v1/uploads/jobs/presign");
}

export interface UploadResult {
  objectUri: string;
  pageNumber: number;
}

export async function uploadFile(
  file: File,
  pageNumber: number,
  onProgress?: (pct: number) => void
): Promise<UploadResult> {
  const presign = await presignUpload();
  await uploadToStorage(presign.upload_url, file, onProgress);
  return { objectUri: presign.object_uri, pageNumber };
}
