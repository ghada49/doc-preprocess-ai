"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  presignReadArtifact,
  fetchArtifactPreviewBlob,
  fetchArtifactJson,
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
  } = {}
) {
  const result = useQuery({
    queryKey: artifactPreviewQueryKey(uri, options, queryOptions.scopeKey),
    queryFn: () => fetchArtifactPreviewBlob(uri!, options),
    enabled: Boolean(uri),
    staleTime: queryOptions.staleTimeMs ?? 5 * 60 * 1000,
    gcTime: queryOptions.gcTimeMs ?? 10 * 60 * 1000,
    refetchOnMount: queryOptions.refetchOnMount,
  });

  // Revoke the blob URL when it changes or the component unmounts.
  useEffect(() => {
    const current = result.data?.blobUrl ?? null;
    return () => {
      if (current) {
        URL.revokeObjectURL(current);
      }
    };
  }, [result.data?.blobUrl]);

  return result;
}
