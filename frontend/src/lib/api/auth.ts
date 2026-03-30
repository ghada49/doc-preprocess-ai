import { jwtDecode } from "jwt-decode";
import type {
  AuthSession,
  JWTPayload,
  LoginRequest,
  LoginResponse,
  SignupRequest,
  SignupResponse,
} from "@/types/api";
import { apiClient } from "./client";

export async function login(
  credentials: LoginRequest
): Promise<LoginResponse> {
  const formData = new URLSearchParams();
  formData.append("username", credentials.username);
  formData.append("password", credentials.password);

  const response = await apiClient.post<LoginResponse>(
    "/v1/auth/token",
    formData,
    {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    }
  );

  return response.data;
}

export async function signup(
  data: SignupRequest
): Promise<SignupResponse> {
  const response = await apiClient.post<SignupResponse>(
    "/v1/auth/signup",
    data
  );

  return response.data;
}

export function decodeToken(token: string): JWTPayload | null {
  try {
    return jwtDecode<JWTPayload>(token);
  } catch {
    return null;
  }
}

export function isTokenExpired(tokenOrExp: string | number): boolean {
  const exp =
    typeof tokenOrExp === "number"
      ? tokenOrExp
      : decodeToken(tokenOrExp)?.exp ?? 0;

  return exp * 1000 <= Date.now();
}

export function buildAuthSession(
  username: string,
  token: string
): AuthSession | null {
  const payload = decodeToken(token);
  if (!payload) return null;

  return {
    token,
    username,
    userId: payload.sub,
    role: payload.role,
    exp: payload.exp,
  };
}
