"use client";

import { useQuery } from "@tanstack/react-query";
import { presignReadArtifact } from "@/lib/api/artifacts";

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
