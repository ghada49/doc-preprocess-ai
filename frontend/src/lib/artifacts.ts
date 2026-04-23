"use client";

import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  presignReadArtifact,
  fetchArtifactPreviewBlob,
  fetchArtifactJson,
  isRetryableArtifactPreviewError,
} from "@/lib/api/artifacts";
import {
  artifactPreviewQueryKey,
  artifactReadQueryKey,
} from "./artifact-query-key";

export { artifactPreviewQueryKey, artifactReadQueryKey };

export function useArtifactRead(
  uri: string | null,
  expiresIn = 300
) {
  return useQuery({
    queryKey: artifactReadQueryKey(uri, expiresIn),
    queryFn: () => presignReadArtifact(uri!, expiresIn),
    enabled: Boolean(uri),
    staleTime: Math.max(0, expiresIn - 30) * 1000,
    gcTime: expiresIn * 1000,
  });
}

export function useArtifactJson<T>(
  uri: string | null,
  expiresIn = 300
) {
  return useQuery({
    queryKey: ["artifact-json", uri, expiresIn] as const,
    queryFn: () => fetchArtifactJson<T>(uri!, expiresIn),
    enabled: Boolean(uri),
    staleTime: Math.max(0, expiresIn - 30) * 1000,
    gcTime: expiresIn * 1000,
  });
}

/**
 * Fetch a browser-displayable PNG preview for any stored artifact URI.
 * Returns { blobUrl } — a blob: URL safe to use in <img src={...}>.
 * Blob URLs are automatically revoked when the URI changes or the
 * component using this hook unmounts (via the cleanup effect below).
 */
export function useArtifactPreview(
  uri: string | null,
  options: { pageIndex?: number; maxWidth?: number } = {},
  queryOptions: {
    scopeKey?: unknown;
    staleTimeMs?: number;
    gcTimeMs?: number;
    refetchOnMount?: boolean | "always";
    retry?: boolean | number | ((failureCount: number, error: unknown) => boolean);
    retryDelayMs?: number;
  } = {}
) {
  const result = useQuery({
    queryKey: artifactPreviewQueryKey(uri, options, queryOptions.scopeKey),
    queryFn: ({ signal }) => fetchArtifactPreviewBlob(uri!, options, signal),
    enabled: Boolean(uri),
    staleTime: queryOptions.staleTimeMs ?? 5 * 60 * 1000,
    gcTime: queryOptions.gcTimeMs ?? 10 * 60 * 1000,
    refetchOnMount: queryOptions.refetchOnMount,
    retry:
      queryOptions.retry ??
      ((failureCount, error) =>
        failureCount < 2 && isRetryableArtifactPreviewError(error)),
    retryDelay: (attemptIndex) =>
      queryOptions.retryDelayMs ?? Math.min(800 * 2 ** (attemptIndex - 1), 2500),
  });

  // Keep Blob data in the query cache and create a fresh object URL locally.
  // This avoids reusing revoked blob: URLs when the user switches between
  // sources and later returns to a previously viewed image.
  const blobUrl = useMemo(
    () => (result.data?.blob ? URL.createObjectURL(result.data.blob) : null),
    [result.data?.blob]
  );

  useEffect(() => {
    return () => {
      if (blobUrl) {
        URL.revokeObjectURL(blobUrl);
      }
    };
  }, [blobUrl]);

  return {
    ...result,
    isLoading: result.isLoading || (!!result.data && !blobUrl),
    data:
      result.data && blobUrl
        ? {
            blobUrl,
            originalWidth: result.data.originalWidth,
            originalHeight: result.data.originalHeight,
          }
        : undefined,
  };
}
