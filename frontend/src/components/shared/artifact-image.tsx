"use client";

import { useState, useEffect } from "react";
import { useArtifactRead } from "@/lib/artifacts";
import { Spinner } from "@/components/ui/spinner";
import { AlertTriangle, FileImage } from "lucide-react";
import { cn } from "@/lib/utils";

interface ArtifactImageProps {
  uri: string | null;
  alt?: string;
  className?: string;
  containerClassName?: string;
  expiresIn?: number;
  fallbackText?: string;
}

export function ArtifactImage({
  uri,
  alt = "Artifact",
  className,
  containerClassName,
  expiresIn = 300,
  fallbackText,
}: ArtifactImageProps) {
  const [imgError, setImgError] = useState(false);

  const { data, isLoading, isError } = useArtifactRead(uri, expiresIn);

  // Reset img error when URI changes
  useEffect(() => {
    setImgError(false);
  }, [uri]);

  if (!uri) {
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center bg-slate-50 rounded-lg border border-slate-200",
          containerClassName
        )}
      >
        <FileImage className="h-8 w-8 text-slate-300 mb-2" />
        <p className="text-xs text-slate-400">{fallbackText ?? "No artifact"}</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div
        className={cn(
          "flex items-center justify-center bg-slate-50 rounded-lg border border-slate-200",
          containerClassName
        )}
      >
        <Spinner size="md" />
      </div>
    );
  }

  if (isError || imgError) {
    return (
      <div
        className={cn(
          "flex flex-col items-center justify-center bg-red-50 rounded-lg border border-red-200",
          containerClassName
        )}
      >
        <AlertTriangle className="h-6 w-6 text-red-500 mb-1.5" />
        <p className="text-xs text-red-600">Failed to load</p>
      </div>
    );
  }

  return (
    <div className={cn("overflow-hidden", containerClassName)}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={data?.read_url}
        alt={alt}
        className={cn("w-full h-full object-contain", className)}
        onError={() => setImgError(true)}
      />
    </div>
  );
}
