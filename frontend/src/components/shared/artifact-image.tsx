"use client";

import { useState, useEffect } from "react";
import { useArtifactPreview } from "@/lib/artifacts";
import { Spinner } from "@/components/ui/spinner";
import { AlertTriangle, FileImage } from "lucide-react";
import { cn } from "@/lib/utils";

interface ArtifactImageProps {
  uri: string | null;
  fallbackUri?: string | null;
  alt?: string;
  className?: string;
  containerClassName?: string;
  expiresIn?: number;
  fallbackText?: string;
  maxWidth?: number;
}

export function ArtifactImage({
  uri,
  fallbackUri = null,
  alt = "Artifact",
  className,
  containerClassName,
  expiresIn = 300,
  fallbackText,
  maxWidth = 1800,
}: ArtifactImageProps) {
  const [imgError, setImgError] = useState(false);
  const [imgLoaded, setImgLoaded] = useState(false);
  const [activeUri, setActiveUri] = useState(uri);

  const { data, isLoading, isError } = useArtifactPreview(activeUri, { maxWidth });
  const imageSrc = data?.blobUrl ?? null;
  const canFallback =
    Boolean(fallbackUri) &&
    Boolean(uri) &&
    fallbackUri !== uri &&
    activeUri === uri;

  useEffect(() => {
    setActiveUri(uri);
  }, [uri, fallbackUri]);

  // Reset local load errors whenever the artifact itself changes or a fresh
  // preview URL is generated for the same artifact.
  useEffect(() => {
    setImgError(false);
    setImgLoaded(false);
  }, [activeUri, imageSrc]);

  useEffect(() => {
    if (!canFallback) return;
    if (!(isError || imgError) || imageSrc) return;
    setImgError(false);
    setActiveUri(fallbackUri);
  }, [canFallback, fallbackUri, imageSrc, imgError, isError]);

  if (!activeUri) {
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

  if (!imageSrc && !(isError || imgError)) {
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

  if (!imageSrc && (isError || imgError)) {
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
    <div className={cn("relative overflow-hidden", containerClassName)}>
      {!imgLoaded && (
        <div className="absolute inset-0 flex items-center justify-center bg-slate-50">
          <Spinner size="md" />
        </div>
      )}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        key={imageSrc ?? activeUri ?? "empty"}
        src={imageSrc ?? undefined}
        alt={alt}
        className={cn(
          "h-full w-full object-contain",
          !imgLoaded && "invisible",
          className
        )}
        onLoad={() => {
          setImgLoaded(true);
          setImgError(false);
        }}
        onError={() => {
          setImgLoaded(false);
          setImgError(true);
        }}
      />
    </div>
  );
}
