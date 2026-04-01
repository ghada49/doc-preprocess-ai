import axios, {
  AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
} from "axios";
import type { AuthSession } from "@/types/api";

const AUTH_SESSION_KEY = "libraryai_auth_session";
const API_BASE_URL = resolveApiBaseUrl();

export interface ApiError {
  name: "ApiError";
  message: string;
  status: number | null;
  detail: string | null;
  errors: unknown;
  isNetworkError: boolean;
  cause: unknown;
}

function resolveApiBaseUrl(): string {
  const configured =
    process.env.NEXT_PUBLIC_API_BASE_URL ??
    process.env.NEXT_PUBLIC_API_URL ??
    "";

  return normalizeBaseUrl(configured);
}

function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  return trimmed.endsWith("/") ? trimmed.slice(0, -1) : trimmed;
}

function isPublicPathname(pathname: string): boolean {
  return pathname === "/" || pathname === "/login" || pathname === "/signup";
}

function dispatchAuthCleared(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event("libraryai:auth-cleared"));
}

export function readAuthSession(): AuthSession | null {
  if (typeof window === "undefined") return null;

  const raw = window.sessionStorage.getItem(AUTH_SESSION_KEY);
  if (!raw) return null;

  try {
    return JSON.parse(raw) as AuthSession;
  } catch {
    window.sessionStorage.removeItem(AUTH_SESSION_KEY);
    return null;
  }
}

export function writeAuthSession(session: AuthSession): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(session));
}

export function clearAuthSession(): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(AUTH_SESSION_KEY);
}

export function getAccessToken(): string | null {
  return readAuthSession()?.token ?? null;
}

export function isApiError(error: unknown): error is ApiError {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: string }).name === "ApiError"
  );
}

export function getApiErrorMessage(
  error: unknown,
  fallback = "Request failed."
): string {
  if (isApiError(error)) {
    return error.detail ?? error.message ?? fallback;
  }

  if (error instanceof Error && error.message) {
    return error.message;
  }

  return fallback;
}

function normalizeApiError(error: unknown): ApiError {
  const axiosError = error as AxiosError<{
    detail?: string | { msg?: string }[] | null;
    errors?: unknown;
    message?: string | null;
  }>;

  const detail = extractErrorDetail(axiosError);

  return {
    name: "ApiError",
    message: detail ?? axiosError.message ?? "Request failed.",
    status: axiosError.response?.status ?? null,
    detail,
    errors: axiosError.response?.data?.errors ?? null,
    isNetworkError: !axiosError.response,
    cause: error,
  };
}

function extractErrorDetail(
  error: AxiosError<{
    detail?: string | { msg?: string }[] | null;
    message?: string | null;
  }>
): string | null {
  const detail = error.response?.data?.detail;
  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => item?.msg)
      .filter((value): value is string => Boolean(value));

    if (messages.length > 0) {
      return messages.join("; ");
    }
  }

  if (typeof error.response?.data?.message === "string") {
    return error.response?.data?.message ?? null;
  }

  return null;
}

export const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL || undefined,
  headers: {
    "Content-Type": "application/json",
  },
  timeout: 30_000,
});

apiClient.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }

  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    const apiError = normalizeApiError(error);

    if (
      apiError.status === 401 &&
      typeof window !== "undefined" &&
      !isPublicPathname(window.location.pathname)
    ) {
      clearAuthSession();
      dispatchAuthCleared();
      window.location.href = "/login";
    }

    return Promise.reject(apiError);
  }
);

export async function apiGet<T>(
  path: string,
  params?: Record<string, unknown>,
  config?: AxiosRequestConfig
): Promise<T> {
  const response = await apiClient.get<T>(path, { params, ...config });
  return response.data;
}

export async function apiPost<T>(
  path: string,
  data?: unknown,
  config?: AxiosRequestConfig
): Promise<T> {
  const response = await apiClient.post<T>(path, data, config);
  return response.data;
}

export async function apiPatch<T>(
  path: string,
  data?: unknown,
  config?: AxiosRequestConfig
): Promise<T> {
  const response = await apiClient.patch<T>(path, data, config);
  return response.data;
}

export async function uploadToStorage(
  uploadUrl: string,
  file: File,
  onProgress?: (pct: number) => void
): Promise<void> {
  await axios.put(uploadUrl, file, {
    headers: { "Content-Type": "image/tiff" },
    onUploadProgress: (event) => {
      if (event.total && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    },
  });
}

export { API_BASE_URL, AUTH_SESSION_KEY };
