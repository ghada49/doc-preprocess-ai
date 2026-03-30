"use client";

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  buildAuthSession,
  isTokenExpired,
  login as apiLogin,
} from "@/lib/api/auth";
import {
  clearAuthSession,
  readAuthSession,
  writeAuthSession,
} from "@/lib/api/client";
import type { AuthSession, LoginRequest } from "@/types/api";

interface AuthState {
  session: AuthSession | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  isAdmin: boolean;
}

interface AuthContextValue extends AuthState {
  user: {
    sub: string;
    role: "user" | "admin";
    exp: number;
  } | null;
  username: string | null;
  login: (credentials: LoginRequest) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function toAuthState(session: AuthSession | null): AuthState {
  return {
    session,
    isLoading: false,
    isAuthenticated: Boolean(session),
    isAdmin: session?.role === "admin",
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    session: null,
    isLoading: true,
    isAuthenticated: false,
    isAdmin: false,
  });

  const resetAuth = useCallback(() => {
    clearAuthSession();
    setState(toAuthState(null));
  }, []);

  useEffect(() => {
    const session = readAuthSession();

    if (session && !isTokenExpired(session.exp)) {
      setState(toAuthState(session));
      return;
    }

    resetAuth();
  }, [resetAuth]);

  useEffect(() => {
    const handleAuthCleared = () => {
      setState(toAuthState(null));
    };

    window.addEventListener("libraryai:auth-cleared", handleAuthCleared);
    return () => {
      window.removeEventListener(
        "libraryai:auth-cleared",
        handleAuthCleared
      );
    };
  }, []);

  const login = useCallback(async (credentials: LoginRequest) => {
    const response = await apiLogin(credentials);
    const session = buildAuthSession(
      credentials.username.trim(),
      response.access_token
    );

    if (!session) {
      throw new Error("Invalid authentication token received.");
    }

    writeAuthSession(session);
    setState(toAuthState(session));
  }, []);

  const logout = useCallback(() => {
    resetAuth();
  }, [resetAuth]);

  const session = state.session;

  return (
    <AuthContext.Provider
      value={{
        ...state,
        user: session
          ? {
              sub: session.userId,
              role: session.role,
              exp: session.exp,
            }
          : null,
        username: session?.username ?? null,
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }

  return ctx;
}
