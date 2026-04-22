import { apiPost, getAccessToken, API_BASE_URL } from "./client";
import type { PresignReadRequest, PresignReadResponse } from "@/types/api";

export interface ArtifactPreviewBlob {
  blobUrl: string;
  originalWidth: number | null;
  originalHeight: number | null;
}

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

export async function fetchArtifactJson<T>(
  uri: string,
  expiresIn = 300
): Promise<T> {
  const readUrl = await presignReadUrl(uri, expiresIn);
  const response = await fetch(readUrl, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
    },
  });

  if (!response.ok) {
    throw new Error(`Artifact fetch failed (HTTP ${response.status})`);
  }

  return response.json() as Promise<T>;
}

/**
 * Call POST /v1/artifacts/preview, receive PNG bytes, and return an object
 * URL suitable for <img src={...}>.
 *
 * The caller must revoke the returned URL via URL.revokeObjectURL() when done.
 */
export async function fetchArtifactPreviewBlobUrl(
  uri: string,
  options: { pageIndex?: number; maxWidth?: number } = {}
): Promise<string> {
  const preview = await fetchArtifactPreviewBlob(uri, options);
  return preview.blobUrl;
}

export async function fetchArtifactPreviewBlob(
  uri: string,
  options: { pageIndex?: number; maxWidth?: number } = {}
): Promise<ArtifactPreviewBlob> {
  const token = getAccessToken();
  const headers: HeadersInit = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const body: Record<string, unknown> = { uri };
  if (options.pageIndex != null) body["page_index"] = options.pageIndex;
  if (options.maxWidth != null) body["max_width"] = options.maxWidth;

  const response = await fetch(`${API_BASE_URL}/v1/artifacts/preview`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let detail = `Preview failed (HTTP ${response.status})`;
    try {
      const json = await response.json();
      if (typeof json?.detail === "string") detail = json.detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }

  const originalWidth = parsePositiveIntHeader(response.headers.get("X-Original-Width"));
  const originalHeight = parsePositiveIntHeader(response.headers.get("X-Original-Height"));

  return {
    blobUrl: URL.createObjectURL(await response.blob()),
    originalWidth,
    originalHeight,
  };
}

function parsePositiveIntHeader(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}
