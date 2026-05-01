import { apiPost, getAccessToken, API_BASE_URL } from "./client";
import type { PresignReadRequest, PresignReadResponse } from "@/types/api";

export interface ArtifactPreviewBlob {
  blob?: Blob;
  url?: string;
  originalWidth: number | null;
  originalHeight: number | null;
  previewWidth: number | null;
  previewHeight: number | null;
  scaleX: number | null;
  scaleY: number | null;
}

interface ArtifactPreviewUrlResponse {
  preview_url: string;
  preview_uri: string;
  expires_in: number;
  width: number;
  height: number;
  source_width: number;
  source_height: number;
  scale_x: number;
  scale_y: number;
  cache_hit: boolean;
}

export class ArtifactPreviewError extends Error {
  status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "ArtifactPreviewError";
    this.status = status;
  }
}

export function isRetryableArtifactPreviewError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === "AbortError") {
    return false;
  }

  if (error instanceof ArtifactPreviewError) {
    return (
      error.status == null ||
      error.status === 408 ||
      error.status === 425 ||
      error.status === 429 ||
      error.status >= 500
    );
  }

  return error instanceof TypeError;
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
    throw new Error("We could not load this file.");
  }

  return response.json() as Promise<T>;
}

/**
 * Call POST /v1/artifacts/preview and return a displayable preview URL plus
 * source dimensions. Production S3 previews come back as cached presigned URLs;
 * local/file fallback still accepts the legacy streamed PNG response.
 */
export async function fetchArtifactPreviewBlobUrl(
  uri: string,
  options: { pageIndex?: number; maxWidth?: number } = {}
): Promise<string> {
  const preview = await fetchArtifactPreviewBlob(uri, options);
  if (preview.url) return preview.url;
  if (preview.blob) return URL.createObjectURL(preview.blob);
  throw new ArtifactPreviewError("We could not load this preview.");
}

export async function fetchArtifactPreviewBlob(
  uri: string,
  options: { pageIndex?: number; maxWidth?: number } = {},
  signal?: AbortSignal
): Promise<ArtifactPreviewBlob> {
  const token = getAccessToken();
  const headers: HeadersInit = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const body: Record<string, unknown> = { uri };
  if (options.pageIndex != null) body["page_index"] = options.pageIndex;
  if (options.maxWidth != null) body["max_width"] = options.maxWidth;
  body["return_url"] = true;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/v1/artifacts/preview`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ArtifactPreviewError("We could not load this preview.");
  }

  if (!response.ok) {
    let detail = "We could not load this preview.";
    try {
      const json = await response.json();
      if (typeof json?.detail === "string") detail = json.detail;
    } catch { /* ignore */ }
    throw new ArtifactPreviewError(detail, response.status);
  }

  const contentType = response.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) {
    const json = (await response.json()) as ArtifactPreviewUrlResponse;
    return {
      url: json.preview_url,
      originalWidth: json.source_width,
      originalHeight: json.source_height,
      previewWidth: json.width,
      previewHeight: json.height,
      scaleX: json.scale_x,
      scaleY: json.scale_y,
    };
  }

  const originalWidth = parsePositiveIntHeader(response.headers.get("X-Original-Width"));
  const originalHeight = parsePositiveIntHeader(response.headers.get("X-Original-Height"));
  const previewWidth = parsePositiveIntHeader(response.headers.get("X-Preview-Width"));
  const previewHeight = parsePositiveIntHeader(response.headers.get("X-Preview-Height"));

  return {
    blob: await response.blob(),
    originalWidth,
    originalHeight,
    previewWidth,
    previewHeight,
    scaleX:
      originalWidth != null && previewWidth != null
        ? previewWidth / originalWidth
        : null,
    scaleY:
      originalHeight != null && previewHeight != null
        ? previewHeight / originalHeight
        : null,
  };
}

function parsePositiveIntHeader(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}
