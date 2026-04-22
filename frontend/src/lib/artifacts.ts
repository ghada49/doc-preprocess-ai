"use client";

import { useEffect, useRef } from "react";
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

  // Revoke the previous blob URL when it changes.
  // useRef is required to be StrictMode-safe: React dev mode runs cleanup
  // immediately after every effect, so a plain closure would revoke the URL
  // before the image renders. The ref is updated inside the effect so the
  // cleanup can compare prev vs current and skip the no-op double-invoke.
  const blobUrlRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = blobUrlRef.current;
    blobUrlRef.current = result.data?.blobUrl ?? null;
    return () => {
      if (prev && prev !== blobUrlRef.current) {
        URL.revokeObjectURL(prev);
      }
    };
  }, [result.data?.blobUrl]);

  return result;
}
