"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/auth/auth-context";
import { isApiError } from "@/lib/api/client";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/shared/error-banner";

export default function LoginPage() {
  const { login, isAuthenticated, isAdmin, isLoading } = useAuth();
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signupSuccess, setSignupSuccess] = useState(false);

  useEffect(() => {
    if (isLoading) return;
    if (isAuthenticated) {
      router.replace(isAdmin ? "/admin/dashboard" : "/jobs");
    }
  }, [isAuthenticated, isAdmin, isLoading, router]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const flag = sessionStorage.getItem("signup_success");
      if (flag) {
        sessionStorage.removeItem("signup_success");
        setSignupSuccess(true);
      }
    }
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login({ username, password });
      // redirect handled by useEffect above
    } catch (err: unknown) {
      const status = isApiError(err) ? err.status : null;
      if (status === 401) {
        setError("Invalid username or password.");
      } else {
        setError("Could not connect to the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4">
      {/* Background grid */}
      <div
        className="fixed inset-0 opacity-[0.4] pointer-events-none"
        style={{
          backgroundImage:
            "linear-gradient(rgba(148,163,184,0.15) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,0.15) 1px, transparent 1px)",
          backgroundSize: "40px 40px",
        }}
      />

      <div className="w-full max-w-sm relative z-10">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-600 mb-4 shadow-lg shadow-indigo-500/20">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              className="h-6 w-6 text-white"
              xmlns="http://www.w3.org/2000/svg"
            >
              <path
                d="M4 4h6v6H4V4zm10 0h6v6h-6V4zM4 14h6v6H4v-6zm10 3a3 3 0 100-6 3 3 0 000 6z"
                fill="currentColor"
              />
            </svg>
          </div>
          <h1 className="text-xl font-semibold text-slate-900 tracking-tight">LibraryAI</h1>
          <p className="text-sm text-slate-500 mt-1">Document AI Operations Console</p>
        </div>

        {/* Card */}
        <div className="bg-white border border-slate-200 rounded-2xl p-8 shadow-sm">
          <h2 className="text-sm font-semibold text-slate-800 mb-6">Sign in to your account</h2>

          {signupSuccess && (
            <div className="mb-5 rounded-lg bg-emerald-50 border border-emerald-200 px-4 py-3 text-sm text-emerald-700">
              Account created. Please sign in.
            </div>
          )}

          {error && (
            <ErrorBanner message={error} onDismiss={() => setError(null)} className="mb-5" />
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                type="text"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="Enter your username"
                required
                autoFocus
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
              />
            </div>

            <Button
              type="submit"
              className="w-full mt-2"
              loading={submitting}
              disabled={!username || !password}
            >
              Sign in
            </Button>
          </form>
        </div>

        {/* Sign-up link */}
        <p className="text-center text-sm text-slate-500 mt-6">
          Don&apos;t have an account?{" "}
          <Link href="/signup" className="text-indigo-600 hover:text-indigo-500 transition-colors">
            Sign up
          </Link>
        </p>

        {/* Footer */}
        <p className="text-center text-2xs text-slate-400 mt-3">
          LibraryAI v2.0
        </p>
      </div>
    </div>
  );
}
