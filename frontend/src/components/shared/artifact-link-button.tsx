"use client";

import { useMutation } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { Download, ExternalLink } from "lucide-react";
import { presignReadArtifact } from "@/lib/api/artifacts";
import { Button } from "@/components/ui/button";
import { getApiErrorMessage } from "@/lib/api/client";

interface ArtifactLinkButtonProps {
  uri: string | null;
  label?: string;
  size?: "xs" | "sm" | "md";
  variant?: "ghost" | "outline" | "secondary";
  mode?: "open" | "download";
  expiresIn?: number;
  className?: string;
}

export function ArtifactLinkButton({
  uri,
  label,
  size = "xs",
  variant = "ghost",
  mode = "open",
  expiresIn = 300,
  className,
}: ArtifactLinkButtonProps) {
  const mutation = useMutation({
    mutationFn: async () => {
      if (!uri) {
        throw new Error("No artifact available.");
      }

      return presignReadArtifact(uri, expiresIn);
    },
    onSuccess: (artifact) => {
      if (typeof window === "undefined") return;

      if (mode === "download") {
        const anchor = document.createElement("a");
        anchor.href = artifact.read_url;
        anchor.rel = "noopener noreferrer";
        anchor.target = "_blank";
        anchor.download = "";
        anchor.click();
        return;
      }

      window.open(artifact.read_url, "_blank", "noopener,noreferrer");
    },
    onError: (error) => {
      toast.error(getApiErrorMessage(error, "Failed to open artifact."));
    },
  });

  return (
    <Button
      type="button"
      size={size}
      variant={variant}
      onClick={() => mutation.mutate()}
      disabled={!uri}
      loading={mutation.isPending}
      className={className}
    >
      {mode === "download" ? (
        <Download className="h-3.5 w-3.5" />
      ) : (
        <ExternalLink className="h-3.5 w-3.5" />
      )}
      <span>{label ?? (mode === "download" ? "Download" : "Open")}</span>
    </Button>
  );
}
