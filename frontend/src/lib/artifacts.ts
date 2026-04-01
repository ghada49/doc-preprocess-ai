"use client";

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { presignReadArtifact, fetchArtifactPreviewBlobUrl } from "@/lib/api/artifacts";

export function artifactReadQueryKey(
  uri: string | null,
  expiresIn = 300
) {
  return ["artifact-read", uri, expiresIn] as const;
}

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

/**
 * Fetch a browser-displayable PNG preview for any stored artifact URI.
 * Returns { blobUrl } — a blob: URL safe to use in <img src={...}>.
 * Blob URLs are automatically revoked when the URI changes or the
 * component using this hook unmounts (via the cleanup effect below).
 */
export function useArtifactPreview(
  uri: string | null,
  options: { pageIndex?: number; maxWidth?: number } = {}
) {
  const optionsKey = JSON.stringify(options);

  const result = useQuery({
    queryKey: ["artifact-preview", uri, optionsKey] as const,
    queryFn: () => fetchArtifactPreviewBlobUrl(uri!, options),
    enabled: Boolean(uri),
    staleTime: 5 * 60 * 1000,
    gcTime: 10 * 60 * 1000,
  });

  // Revoke the blob URL when it changes or the component unmounts.
  const blobUrlRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = blobUrlRef.current;
    blobUrlRef.current = result.data ?? null;
    return () => {
      if (prev && prev !== blobUrlRef.current) {
        URL.revokeObjectURL(prev);
      }
    };
  }, [result.data]);

  return { ...result, data: result.data ? { blobUrl: result.data } : undefined };
}
