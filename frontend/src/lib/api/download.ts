import { apiGet, getAccessToken } from "./client";
import type { DownloadManifestResponse } from "@/types/api";

const API_BASE_URL =
  (process.env.NEXT_PUBLIC_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

/**
 * GET /v1/jobs/{job_id}/output/download-manifest
 * Returns a JSON manifest with per-page presigned download URLs.
 * Recommended for large collections (100+ pages).
 */
export function getDownloadManifest(jobId: string): Promise<DownloadManifestResponse> {
  return apiGet<DownloadManifestResponse>(`/v1/jobs/${jobId}/output/download-manifest`);
}

/**
 * Trigger a ZIP download for all job output images.
 *
 * Uses native fetch with the Bearer token so the authenticated streaming
 * response is piped to a temporary <a> element for file-save.
 * GET /v1/jobs/{job_id}/output/download.zip
 */
export async function downloadJobOutputZip(jobId: string): Promise<void> {
  const token = getAccessToken();
  const url = `${API_BASE_URL}/v1/jobs/${jobId}/output/download.zip`;

  const response = await fetch(url, {
    method: "GET",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });

  if (!response.ok) {
    throw new Error(`Download failed: ${response.status} ${response.statusText}`);
  }

  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);

  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = `job_${jobId}_output.zip`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(objectUrl);
}
