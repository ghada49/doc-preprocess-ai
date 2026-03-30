import { AlertTriangle, X } from "lucide-react";
import { getApiErrorMessage } from "@/lib/api/client";
import { cn } from "@/lib/utils";

interface ErrorBannerProps {
  title?: string;
  message: string;
  onDismiss?: () => void;
  className?: string;
  variant?: "inline" | "fullscreen";
}

export function ErrorBanner({
  title = "Error",
  message,
  onDismiss,
  className,
  variant = "inline",
}: ErrorBannerProps) {
  if (variant === "fullscreen") {
    return (
      <div className="flex flex-col items-center justify-center min-h-[300px] py-16 px-6 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-red-50 mb-4 border border-red-200">
          <AlertTriangle className="h-7 w-7 text-red-500" />
        </div>
        <h3 className="text-sm font-semibold text-slate-800 mb-1">{title}</h3>
        <p className="text-xs text-slate-500 max-w-sm leading-relaxed">{message}</p>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-4",
        className
      )}
    >
      <AlertTriangle className="h-4 w-4 text-red-500 mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        {title !== "Error" && (
          <p className="text-xs font-semibold text-red-700 mb-0.5">{title}</p>
        )}
        <p className="text-xs text-red-600 leading-relaxed">{message}</p>
      </div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          className="text-red-400 hover:text-red-600 transition-colors"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}

export function ApiErrorBanner({ error }: { error: unknown }) {
  const message = getApiErrorMessage(error, "An unexpected error occurred.");
  return <ErrorBanner message={message} />;
}
