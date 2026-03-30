"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { useState, type ReactNode } from "react";
import { Toaster } from "react-hot-toast";
import { AuthProvider } from "@/lib/auth/auth-context";
import { isApiError } from "@/lib/api/client";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            gcTime: 5 * 60 * 1000,
            retry: (failureCount, error: unknown) => {
              const status = isApiError(error) ? error.status : null;
              // Don't retry on 4xx errors
              if (status && status >= 400 && status < 500) return false;
              return failureCount < 2;
            },
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        {children}
        <Toaster
          position="bottom-right"
          toastOptions={{
            style: {
              background: "#18181b",
              color: "#fafafa",
              border: "1px solid #3f3f46",
              borderRadius: "8px",
              fontSize: "13px",
            },
            success: {
              iconTheme: { primary: "#22c55e", secondary: "#18181b" },
            },
            error: {
              iconTheme: { primary: "#ef4444", secondary: "#18181b" },
            },
          }}
        />
        <ReactQueryDevtools initialIsOpen={false} />
      </AuthProvider>
    </QueryClientProvider>
  );
}
