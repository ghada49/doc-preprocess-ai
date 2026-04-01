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
import { signup } from "@/lib/api/auth";

export default function SignupPage() {
  const { isAuthenticated, isAdmin, isLoading } = useAuth();
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  // If already authenticated, redirect to the appropriate landing page
  useEffect(() => {
    if (isLoading) return;
    if (isAuthenticated) {
      router.replace(isAdmin ? "/admin/dashboard" : "/jobs");
    }
  }, [isAuthenticated, isAdmin, isLoading, router]);

  const validate = (): boolean => {
    const errors: Record<string, string> = {};
    if (!username.trim()) {
      errors.username = "Username is required.";
    }
    if (!password) {
      errors.password = "Password is required.";
    } else if (password.length < 8) {
      errors.password = "Password must be at least 8 characters.";
    }
    if (password !== confirmPassword) {
      errors.confirmPassword = "Passwords do not match.";
    }
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!validate()) return;

    setSubmitting(true);
    try {
      await signup({ username: username.trim(), password });
      // Signal to login page that account creation succeeded
      sessionStorage.setItem("signup_success", "true");
      router.replace("/login");
    } catch (err: unknown) {
      const httpStatus = isApiError(err) ? err.status : null;
      if (httpStatus === 409) {
        setFieldErrors((prev) => ({ ...prev, username: "That username is already taken." }));
      } else if (httpStatus === 422) {
        setError("Invalid input. Please check your username and password.");
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
          <h2 className="text-sm font-semibold text-slate-800 mb-6">Create your account</h2>

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
                onChange={(e) => {
                  setUsername(e.target.value);
                  if (fieldErrors.username) setFieldErrors((p) => ({ ...p, username: "" }));
                }}
                placeholder="Choose a username"
                required
                autoFocus
              />
              {fieldErrors.username && (
                <p className="text-xs text-red-600 mt-1">{fieldErrors.username}</p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  if (fieldErrors.password) setFieldErrors((p) => ({ ...p, password: "" }));
                }}
                placeholder="••••••••"
                required
              />
              {fieldErrors.password && (
                <p className="text-xs text-red-600 mt-1">{fieldErrors.password}</p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="confirmPassword">Confirm password</Label>
              <Input
                id="confirmPassword"
                type="password"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => {
                  setConfirmPassword(e.target.value);
                  if (fieldErrors.confirmPassword)
                    setFieldErrors((p) => ({ ...p, confirmPassword: "" }));
                }}
                placeholder="••••••••"
                required
              />
              {fieldErrors.confirmPassword && (
                <p className="text-xs text-red-600 mt-1">{fieldErrors.confirmPassword}</p>
              )}
            </div>

            <Button
              type="submit"
              className="w-full mt-2"
              loading={submitting}
              disabled={!username || !password || !confirmPassword}
            >
              Create account
            </Button>
          </form>
        </div>

        {/* Sign-in link */}
        <p className="text-center text-sm text-slate-500 mt-6">
          Already have an account?{" "}
          <Link href="/login" className="text-indigo-600 hover:text-indigo-500 transition-colors">
            Sign in
          </Link>
        </p>

        {/* Footer */}
        <p className="text-center text-2xs text-slate-400 mt-3">
          LibraryAI v2.0 — Regular user accounts only
        </p>
      </div>
    </div>
  );
}
