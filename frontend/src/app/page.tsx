"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth/auth-context";

// ─── ICONS ────────────────────────────────────────────────────────────────────

function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M4 4h6v6H4V4zm10 0h6v6h-6V4zM4 14h6v6H4v-6zm10 3a3 3 0 100-6 3 3 0 000 6z"
        fill="currentColor"
      />
    </svg>
  );
}

function IconLayers({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconShield({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M9 12l2 2 4-4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconUsers({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconAudit({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconBuilding({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M3 21h18M3 10h18M3 7l9-4 9 4M4 10v11M20 10v11M8 14v3M12 14v3M16 14v3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconGear({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M12 15a3 3 0 100-6 3 3 0 000 6z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconChart({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M18 20V10M12 20V4M6 20v-6"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconCheck({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M20 6L9 17l-5-5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconArrowRight({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M4 10h12M10 4l6 6-6 6"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// ─── PIPELINE PREVIEW ─────────────────────────────────────────────────────────

const PREVIEW_PAGES = [
  { label: "page_001.tiff", state: "accepted", colorKey: "emerald" },
  { label: "page_002.tiff", state: "layout detection", colorKey: "cyan" },
  { label: "page_003.tiff", state: "preprocessing", colorKey: "blue" },
  { label: "page_004.tiff", state: "pending correction", colorKey: "orange" },
  { label: "page_005.tiff", state: "queued", colorKey: "slate" },
];

const STATE_COLORS: Record<string, string> = {
  emerald: "bg-emerald-50 text-emerald-700 border border-emerald-200",
  cyan: "bg-cyan-50 text-cyan-700 border border-cyan-200",
  blue: "bg-blue-50 text-blue-700 border border-blue-200",
  orange: "bg-orange-50 text-orange-700 border border-orange-200",
  slate: "bg-slate-100 text-slate-600 border border-slate-200",
};

const STATE_ACCENTS: Record<string, string> = {
  emerald: "bg-emerald-500",
  cyan: "bg-cyan-500",
  blue: "bg-blue-500",
  orange: "bg-orange-500",
  slate: "bg-slate-400",
};

const PRIMARY_BUTTON_CLASS =
  "group inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-indigo-600 via-indigo-600 to-violet-600 text-white ring-1 ring-indigo-500/20 shadow-[0_18px_40px_-20px_rgba(79,70,229,0.75)] transition-all duration-300 hover:-translate-y-0.5 hover:shadow-[0_24px_56px_-20px_rgba(79,70,229,0.55)]";

const SECONDARY_BUTTON_CLASS =
  "inline-flex items-center justify-center rounded-xl border border-slate-200 bg-white/95 text-slate-700 shadow-sm shadow-slate-200/70 transition-all duration-300 hover:-translate-y-0.5 hover:border-slate-300 hover:bg-white hover:text-slate-900 hover:shadow-lg hover:shadow-slate-200/70";

const HOME_CARD_CLASS =
  "group rounded-2xl border border-slate-200/90 bg-white/95 shadow-[0_12px_36px_-24px_rgba(15,23,42,0.28)] transition-all duration-300 hover:-translate-y-1 hover:border-slate-300 hover:shadow-[0_24px_60px_-28px_rgba(15,23,42,0.22)]";

function PipelinePreview() {
  const [activeIndex, setActiveIndex] = useState(1);

  useEffect(() => {
    const interval = window.setInterval(() => {
      setActiveIndex((current) => (current + 1) % PREVIEW_PAGES.length);
    }, 2200);

    return () => window.clearInterval(interval);
  }, []);

  return (
    <div className="relative homepage-floating-panel">
      <div className="absolute -inset-10 rounded-[2rem] bg-[radial-gradient(circle_at_top,rgba(79,70,229,0.16),transparent_56%)] blur-3xl pointer-events-none" />
      <div className="absolute -right-10 top-8 h-32 w-32 rounded-full bg-violet-200/[0.45] blur-3xl pointer-events-none" />
      <div className="absolute -left-6 bottom-10 h-28 w-28 rounded-full bg-sky-100/60 blur-3xl pointer-events-none" />
      <div className="absolute inset-x-10 -bottom-5 h-10 rounded-full bg-slate-900/[0.08] blur-2xl pointer-events-none" />

      <div className="relative overflow-hidden rounded-[28px] border border-white/80 bg-white/95 shadow-[0_34px_90px_-42px_rgba(15,23,42,0.34)] ring-1 ring-slate-200/80 backdrop-blur-sm">
        <div className="absolute inset-x-16 top-0 h-px bg-gradient-to-r from-transparent via-white to-transparent opacity-90" />
        <div className="absolute -left-10 top-14 h-32 w-32 rounded-full bg-indigo-100/60 blur-3xl pointer-events-none" />
        {/* Window chrome */}
        <div className="px-4 py-3 border-b border-slate-200/80 flex items-center justify-between bg-slate-50/[0.85] backdrop-blur-sm">
          <div className="flex items-center gap-3">
            <div className="flex gap-1.5">
              <div className="w-2.5 h-2.5 rounded-full bg-rose-300/90" />
              <div className="w-2.5 h-2.5 rounded-full bg-amber-300/90" />
              <div className="w-2.5 h-2.5 rounded-full bg-emerald-300/90" />
            </div>
            <span className="text-xs font-mono text-slate-500">job_7f3a2b1e · col-2024-aub</span>
          </div>
          <div className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white/90 px-2.5 py-1 shadow-sm shadow-slate-200/70">
            <span className="relative flex h-2.5 w-2.5">
              <span className="absolute inline-flex h-full w-full rounded-full bg-blue-400/[0.65] blur-[1px] homepage-live-dot" />
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-blue-500 homepage-live-dot" />
            </span>
            <span className="text-xs text-blue-600 font-medium">running</span>
          </div>
        </div>

        {/* Column headers */}
        <div className="px-4 py-2 grid grid-cols-[1fr_auto] gap-4 border-b border-slate-100 bg-slate-50/60">
          <span className="text-2xs font-semibold text-slate-400 uppercase tracking-wider">Page</span>
          <span className="text-2xs font-semibold text-slate-400 uppercase tracking-wider">Status</span>
        </div>

        {/* Page rows */}
        <div className="divide-y divide-slate-100">
          {PREVIEW_PAGES.map((page, index) => {
            const isActive = index === activeIndex;

            return (
              <div
                key={page.label}
                className={`relative flex items-center justify-between gap-4 px-4 py-2.5 transition-all duration-500 ${
                  isActive ? "bg-slate-50/95" : "hover:bg-slate-50/70"
                }`}
              >
                <div
                  className={`absolute left-0 top-2 bottom-2 w-1 rounded-r-full transition-all duration-500 ${
                    isActive ? STATE_ACCENTS[page.colorKey] : "bg-transparent"
                  }`}
                />
                <div className="flex items-center gap-2.5 min-w-0">
                  <div
                    className={`flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-xl border transition-all duration-500 ${
                      isActive
                        ? "border-slate-200 bg-white shadow-sm shadow-slate-200/70"
                        : "border-transparent bg-slate-100/80"
                    }`}
                  >
                    <svg
                      viewBox="0 0 14 16"
                      fill="none"
                      className={`h-3.5 w-3 transition-colors duration-500 ${
                        isActive ? "text-slate-500" : "text-slate-400"
                      }`}
                    >
                      <path
                        d="M2 1h7l3 3v11H2V1z"
                        stroke="currentColor"
                        strokeWidth="1.2"
                        strokeLinejoin="round"
                      />
                      <path
                        d="M9 1v3h3"
                        stroke="currentColor"
                        strokeWidth="1.2"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </div>
                  <span
                    className={`block truncate text-xs font-mono transition-colors duration-500 ${
                      isActive ? "text-slate-900" : "text-slate-700"
                    }`}
                  >
                    {page.label}
                  </span>
                </div>
                <span
                  className={`flex-shrink-0 inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-2xs font-medium whitespace-nowrap transition-all duration-500 ${
                    STATE_COLORS[page.colorKey]
                  } ${isActive ? "shadow-sm shadow-slate-200/70" : ""}`}
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full transition-colors duration-500 ${
                      isActive ? STATE_ACCENTS[page.colorKey] : "bg-current/40"
                    }`}
                  />
                  {page.state}
                </span>
              </div>
            );
          })}
        </div>

        <div className="border-t border-slate-100 bg-white/80 px-4 py-2">
          <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-gradient-to-r from-indigo-500 via-sky-500 to-violet-500 transition-all duration-700"
              style={{ width: `${((activeIndex + 1) / PREVIEW_PAGES.length) * 100}%` }}
            />
          </div>
        </div>

        {/* Summary footer */}
        <div className="px-4 py-3 border-t border-slate-100 bg-slate-50/75 flex items-center gap-5">
          <span className="text-2xs text-slate-500">5 pages</span>
          <span className="text-2xs text-emerald-600">1 accepted</span>
          <span className="text-2xs text-blue-600">3 processing</span>
          <span className="text-2xs text-orange-600">1 flagged</span>
        </div>

        {/* Quality gate strip */}
        <div className="relative overflow-hidden px-4 py-2.5 border-t border-slate-100 bg-white flex items-center gap-2">
          <div className="pointer-events-none absolute inset-y-0 left-0 w-24 bg-gradient-to-r from-white via-indigo-100/40 to-transparent homepage-sheen" />
          <svg viewBox="0 0 16 16" fill="none" className="w-3.5 h-3.5 text-indigo-500 flex-shrink-0">
            <path d="M8 14s6-2.667 6-6.667V3.333L8 1.333 2 3.333v4C2 11.333 8 14 8 14z" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
            <path d="M5.5 8l2 2 3-3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          <span className="text-2xs text-slate-500">Quality gate active · policy v3 · pipeline: layout</span>
        </div>
      </div>

      {/* Decorative side panels */}
      <div className="absolute -right-3 top-8 w-px h-16 bg-gradient-to-b from-transparent via-indigo-400/30 to-transparent" />
      <div className="absolute -left-3 bottom-8 w-px h-16 bg-gradient-to-b from-transparent via-violet-400/30 to-transparent" />
    </div>
  );
}

// ─── NAV ──────────────────────────────────────────────────────────────────────

function Nav() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`fixed top-0 inset-x-0 z-50 transition-all duration-300 ${
        scrolled
          ? "bg-white/90 backdrop-blur-md border-b border-slate-200 shadow-sm shadow-slate-100"
          : ""
      }`}
    >
      <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
        {/* Logo */}
        <Link href="/" className="flex items-center gap-2.5 group">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-600 to-violet-600 transition-all duration-300 group-hover:-translate-y-0.5 group-hover:shadow-lg group-hover:shadow-indigo-500/25">
            <LogoMark className="h-4 w-4 text-white" />
          </div>
          <span className="font-semibold text-slate-900 tracking-tight">LibraryAI</span>
        </Link>

        {/* Nav links */}
        <nav className="hidden md:flex items-center gap-8">
          <a
            href="#features"
            className="text-sm text-slate-500 hover:text-slate-900 transition-colors duration-150"
          >
            Features
          </a>
          <a
            href="#how-it-works"
            className="text-sm text-slate-500 hover:text-slate-900 transition-colors duration-150"
          >
            How it works
          </a>
        </nav>

        {/* CTAs */}
        <div className="flex items-center gap-2">
          <Link
            href="/login"
            className="hidden sm:inline-flex h-9 px-4 text-sm text-slate-600 hover:text-slate-900 hover:bg-slate-100 rounded-lg transition-colors duration-150 items-center"
          >
            Log in
          </Link>
          <Link
            href="/signup"
            className={`${PRIMARY_BUTTON_CLASS} h-9 px-4 text-sm font-medium rounded-lg`}
          >
            Get Started
          </Link>
        </div>
      </div>
    </header>
  );
}

// ─── HERO ─────────────────────────────────────────────────────────────────────

function Hero() {
  return (
    <section className="relative overflow-hidden pt-32 pb-20 lg:pb-28">
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_90%_58%_at_50%_-12%,rgba(79,70,229,0.12),transparent_62%)] pointer-events-none" />
      <div className="absolute inset-y-0 left-[-10%] w-[42rem] bg-[radial-gradient(circle_at_center,rgba(129,140,248,0.12),transparent_62%)] pointer-events-none" />
      <div className="absolute right-[-8%] top-20 h-[28rem] w-[28rem] rounded-full bg-[radial-gradient(circle,rgba(167,139,250,0.14),transparent_64%)] pointer-events-none" />
      <div className="absolute inset-x-0 bottom-0 h-32 bg-gradient-to-b from-transparent to-white/80 pointer-events-none" />

      {/* Subtle grid */}
      <div
        className="absolute inset-0 opacity-[0.4] pointer-events-none"
        style={{
          backgroundImage:
            "linear-gradient(rgba(148,163,184,0.15) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,0.15) 1px, transparent 1px)",
          backgroundSize: "48px 48px",
        }}
      />

      <div className="relative max-w-6xl mx-auto px-6">
        <div className="grid lg:grid-cols-[1fr_480px] gap-16 items-center">
          {/* Left: copy */}
          <div className="max-w-[34rem]">
            {/* Badge */}
            <div className="inline-flex items-center gap-2 mb-7 rounded-full border border-indigo-200/80 bg-white/80 px-3.5 py-1.5 text-xs font-medium text-indigo-700 shadow-sm shadow-indigo-100/70 backdrop-blur-sm">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full rounded-full bg-indigo-400/80 homepage-live-dot" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-indigo-500 homepage-live-dot" />
              </span>
              Production-grade document AI pipeline
            </div>

            {/* Headline */}
            <h1 className="mb-6 max-w-[11ch] text-[2.95rem] font-bold leading-[1.02] tracking-[-0.04em] text-slate-900 sm:text-[5rem] lg:text-[4.25rem]">
              Every scanned page,{" "}
              <span className="text-gradient">precisely processed.</span>
            </h1>

            {/* Description */}
            <p className="mb-9 max-w-[31rem] text-[1.0625rem] leading-8 text-slate-600 sm:text-lg">
              LibraryAI runs multi-stage AI processing with enforced quality gates,
              structured human correction, and complete lineage on every page.
              No silent failures. No lost work.
            </p>

            {/* CTAs */}
            <div className="flex flex-wrap items-center gap-3">
              <Link href="/signup" className={`${PRIMARY_BUTTON_CLASS} h-11 px-6 text-sm font-semibold`}>
                Get Started
                <IconArrowRight className="w-4 h-4 transition-transform duration-300 group-hover:translate-x-0.5" />
              </Link>
              <Link href="/login" className={`${SECONDARY_BUTTON_CLASS} h-11 px-6 text-sm font-medium`}>
                Log in
              </Link>
            </div>

            {/* Social proof strip */}
            <div className="mt-10 flex flex-wrap items-center gap-4 border-t border-slate-200/80 pt-8">
              {[
                ["No silent failures", "emerald"],
                ["Full audit trail", "indigo"],
                ["Human-in-the-loop", "violet"],
              ].map(([label, color]) => (
                <div key={label} className="flex items-center gap-2 rounded-full border border-slate-200/70 bg-white/80 px-3 py-1.5 shadow-sm shadow-slate-200/50">
                  <div
                    className={`w-1.5 h-1.5 rounded-full ${
                      color === "emerald"
                        ? "bg-emerald-500"
                        : color === "indigo"
                        ? "bg-indigo-500"
                        : "bg-violet-500"
                    }`}
                  />
                  <span className="text-xs font-medium text-slate-500">{label}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Right: pipeline preview */}
          <div className="hidden lg:block lg:pl-4">
            <PipelinePreview />
          </div>
        </div>
      </div>
    </section>
  );
}

// ─── FEATURES ─────────────────────────────────────────────────────────────────

const FEATURES = [
  {
    Icon: IconLayers,
    title: "Multi-Stage Pipeline",
    description:
      "Preprocessing, geometry detection, normalization, rectification, and layout analysis run in coordinated branches. The system selects the best output or escalates when branches disagree.",
    accent: "indigo",
  },
  {
    Icon: IconShield,
    title: "Enforced Quality Gates",
    description:
      "Each page is evaluated against configurable quality thresholds. Pages below confidence bounds are never auto-accepted — they route to human review with the reason recorded.",
    accent: "violet",
  },
  {
    Icon: IconUsers,
    title: "Structured Human Correction",
    description:
      "When the system flags a page, operators see all branch outputs, current geometry, and review reasons in one workspace. Corrections resubmit the page for reprocessing.",
    accent: "sky",
  },
  {
    Icon: IconAudit,
    title: "Complete Lineage",
    description:
      "Every model invocation, quality score, gate decision, and correction is recorded. Any page can be fully audited from raw OTIFF input to accepted final output.",
    accent: "emerald",
  },
];

const FEATURE_ACCENT: Record<string, string> = {
  indigo: "bg-indigo-50 text-indigo-600 border-indigo-200",
  violet: "bg-violet-50 text-violet-600 border-violet-200",
  sky: "bg-sky-50 text-sky-600 border-sky-200",
  emerald: "bg-emerald-50 text-emerald-600 border-emerald-200",
};

function Features() {
  return (
    <section id="features" className="py-24 border-t border-slate-200">
      <div className="max-w-6xl mx-auto px-6">
        <div className="text-center mb-14">
          <p className="text-xs font-semibold text-indigo-600 uppercase tracking-[0.15em] mb-4">
            Built differently
          </p>
          <h2 className="text-3xl sm:text-4xl font-bold tracking-tight text-slate-900 mb-4 leading-tight">
            Every decision is recorded.
            <br />
            Every page is accounted for.
          </h2>
          <p className="text-slate-600 max-w-lg mx-auto leading-relaxed text-[0.9375rem]">
            Designed for workflows where losing a page or silently accepting a bad
            result is not acceptable.
          </p>
        </div>

        <div className="grid sm:grid-cols-2 gap-4">
          {FEATURES.map(({ Icon, title, description, accent }) => (
            <div
              key={title}
              className={`${HOME_CARD_CLASS} p-6`}
            >
              <div
                className={`mb-5 inline-flex h-11 w-11 items-center justify-center rounded-2xl border shadow-sm shadow-slate-200/60 transition-all duration-300 group-hover:-translate-y-0.5 group-hover:shadow-md group-hover:shadow-slate-200/80 ${FEATURE_ACCENT[accent]}`}
              >
                <Icon className="h-5 w-5" />
              </div>
              <h3 className="font-semibold text-slate-900 mb-2 text-[0.9375rem]">{title}</h3>
              <p className="text-sm text-slate-600 leading-relaxed">{description}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ─── HOW IT WORKS ─────────────────────────────────────────────────────────────

const STEPS = [
  {
    number: "01",
    title: "Upload",
    description:
      "Submit scanned TIFF pages. The system creates a processing job and enqueues each page independently.",
  },
  {
    number: "02",
    title: "Process",
    description:
      "Multi-stage AI pipeline runs preprocessing, geometry detection, normalization, and layout analysis in parallel branches.",
  },
  {
    number: "03",
    title: "Review",
    description:
      "Pages below quality thresholds route to the correction queue. Operators inspect branch outputs and resolve.",
  },
  {
    number: "04",
    title: "Verify",
    description:
      "Accepted pages carry a complete lineage record — traceable from raw input to verified output.",
  },
];

function HowItWorks() {
  return (
    <section
      id="how-it-works"
      className="py-24 border-t border-slate-200 bg-slate-50/80"
    >
      <div className="max-w-6xl mx-auto px-6">
        <div className="text-center mb-14">
          <p className="text-xs font-semibold text-indigo-600 uppercase tracking-[0.15em] mb-4">
            Workflow
          </p>
          <h2 className="text-3xl sm:text-4xl font-bold tracking-tight text-slate-900 leading-tight">
            From raw scan to verified output
          </h2>
        </div>

        {/* Steps row */}
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-6 lg:gap-4">
          {STEPS.map(({ number, title, description }, idx) => (
            <div key={number} className="relative flex flex-col">
              {/* Connector */}
              {idx < STEPS.length - 1 && (
                <div className="hidden lg:block absolute top-6 left-full w-full z-0">
                  <div className="ml-2 mr-2 h-px bg-gradient-to-r from-slate-300 via-slate-200 to-transparent" />
                </div>
              )}

              {/* Step number */}
              <div className="flex items-center gap-3 mb-4">
                <div className="relative z-10 flex-shrink-0 w-12 h-12 rounded-2xl bg-white border border-slate-200 shadow-sm flex items-center justify-center">
                  <span className="text-xs font-bold font-mono text-indigo-600">{number}</span>
                </div>
              </div>

              <h3 className="font-semibold text-slate-900 mb-2">{title}</h3>
              <p className="text-sm text-slate-600 leading-relaxed">{description}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ─── TRUST ────────────────────────────────────────────────────────────────────

const GUARANTEES = [
  "Quality thresholds are policy-driven and configurable per material type",
  "Every routing decision is logged with a reason code",
  "Model versions are evaluated against golden datasets before promotion",
  "Correction history is part of the permanent lineage record",
  "No page transitions to a terminal state without an explicit decision",
];

function Trust() {
  return (
    <section className="py-24 border-t border-slate-200 relative overflow-hidden bg-white">
      {/* Centered glow */}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_60%_40%_at_50%_50%,rgba(79,70,229,0.04),transparent)] pointer-events-none" />

      <div className="relative max-w-6xl mx-auto px-6">
        <div className="grid lg:grid-cols-2 gap-16 items-center">
          {/* Left: statement */}
          <div>
            <p className="text-xs font-semibold text-indigo-600 uppercase tracking-[0.15em] mb-4">
              Reliability
            </p>
            <h2 className="text-3xl sm:text-4xl font-bold tracking-tight text-slate-900 leading-tight mb-5">
              The system does not accept results it cannot justify.
            </h2>
            <p className="text-slate-600 leading-relaxed text-[0.9375rem] mb-8">
              LibraryAI was built for institutions where document accuracy is an
              operational requirement. Every uncertainty is surfaced. Every
              acceptance is defensible.
            </p>
            <div className="flex flex-col sm:flex-row gap-3">
              <Link href="/signup" className={`${PRIMARY_BUTTON_CLASS} h-11 px-6 text-sm font-semibold`}>
                Get Started
                <IconArrowRight className="w-4 h-4 transition-transform duration-300 group-hover:translate-x-0.5" />
              </Link>
              <Link href="/login" className={`${SECONDARY_BUTTON_CLASS} h-11 px-6 text-sm font-medium`}>
                Log in
              </Link>
            </div>
          </div>

          {/* Right: guarantee list */}
          <div className="space-y-2.5">
            {GUARANTEES.map((g) => (
              <div
                key={g}
                className="flex items-start gap-3 rounded-xl border border-slate-200 bg-white/[0.85] px-4 py-3.5 shadow-sm shadow-slate-200/[0.55] transition-all duration-300 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-md hover:shadow-slate-200/70"
              >
                <div className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-md border border-indigo-200 bg-indigo-50 shadow-sm shadow-indigo-100/70">
                  <svg viewBox="0 0 12 12" fill="none" className="w-3 h-3 text-indigo-600">
                    <path
                      d="M10 3L5 8.5 2 5.5"
                      stroke="currentColor"
                      strokeWidth="1.4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <span className="text-sm text-slate-700 leading-relaxed">{g}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

// ─── AUDIENCE ─────────────────────────────────────────────────────────────────

const AUDIENCES = [
  {
    Icon: IconBuilding,
    title: "Libraries & Archives",
    description:
      "Process large document collections with reproducible, auditable results that meet institutional standards.",
    accent: "indigo",
  },
  {
    Icon: IconGear,
    title: "Digitization Teams",
    description:
      "Manage high-throughput scanning workflows with built-in quality enforcement and human escalation paths.",
    accent: "violet",
  },
  {
    Icon: IconChart,
    title: "AI Operations",
    description:
      "Monitor model performance, manage evaluation gates, and promote versions with full traceability.",
    accent: "sky",
  },
  {
    Icon: IconCheck,
    title: "Quality Assurance",
    description:
      "Review flagged pages in a structured workspace, inspect correction history, and trace routing decisions.",
    accent: "emerald",
  },
];

const AUDIENCE_ACCENT: Record<string, string> = {
  indigo: "text-indigo-600 bg-indigo-50 border-indigo-200",
  violet: "text-violet-600 bg-violet-50 border-violet-200",
  sky: "text-sky-600 bg-sky-50 border-sky-200",
  emerald: "text-emerald-600 bg-emerald-50 border-emerald-200",
};

function Audience() {
  return (
    <section className="py-24 border-t border-slate-200 bg-slate-50/80">
      <div className="max-w-6xl mx-auto px-6">
        <div className="text-center mb-14">
          <p className="text-xs font-semibold text-indigo-600 uppercase tracking-[0.15em] mb-4">
            Who it&apos;s for
          </p>
          <h2 className="text-3xl sm:text-4xl font-bold tracking-tight text-slate-900">
            Built for critical workflows
          </h2>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {AUDIENCES.map(({ Icon, title, description, accent }) => (
            <div
              key={title}
              className={`${HOME_CARD_CLASS} p-5`}
            >
              <div
                className={`mb-4 inline-flex h-11 w-11 items-center justify-center rounded-2xl border shadow-sm shadow-slate-200/60 transition-all duration-300 group-hover:-translate-y-0.5 group-hover:shadow-md group-hover:shadow-slate-200/80 ${AUDIENCE_ACCENT[accent]}`}
              >
                <Icon className="h-5 w-5" />
              </div>
              <h3 className="font-semibold text-slate-900 text-sm mb-2">{title}</h3>
              <p className="text-xs text-slate-600 leading-relaxed">{description}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ─── FINAL CTA ────────────────────────────────────────────────────────────────

function FinalCTA() {
  return (
    <section className="py-24 border-t border-slate-200 relative overflow-hidden bg-white">
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_60%_80%_at_50%_100%,rgba(79,70,229,0.06),transparent)] pointer-events-none" />

      <div className="relative max-w-2xl mx-auto px-6 text-center">
        <h2 className="text-3xl sm:text-4xl font-bold tracking-tight text-slate-900 mb-4 leading-tight">
          Start processing with confidence.
        </h2>
        <p className="text-slate-600 text-[0.9375rem] leading-relaxed mb-8">
          Create an account and run your first job. No configuration required to get
          started — quality gates and correction workflows are built in from day one.
        </p>
        <div className="flex flex-col sm:flex-row items-center justify-center gap-3">
          <Link
            href="/signup"
            className={`${PRIMARY_BUTTON_CLASS} h-12 w-full px-8 text-sm font-semibold sm:w-auto`}
          >
            Create a free account
            <IconArrowRight className="w-4 h-4 transition-transform duration-300 group-hover:translate-x-0.5" />
          </Link>
          <Link
            href="/login"
            className={`${SECONDARY_BUTTON_CLASS} h-12 w-full px-8 text-sm font-medium sm:w-auto`}
          >
            Log in to existing account
          </Link>
        </div>
      </div>
    </section>
  );
}

// ─── FOOTER ───────────────────────────────────────────────────────────────────

function Footer() {
  return (
    <footer className="border-t border-slate-200 py-12 bg-white">
      <div className="max-w-6xl mx-auto px-6">
        <div className="grid sm:grid-cols-[1fr_auto] gap-8 items-start">
          {/* Brand */}
          <div className="max-w-xs">
            <div className="flex items-center gap-2.5 mb-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600">
                <LogoMark className="h-3.5 w-3.5 text-white" />
              </div>
              <span className="font-semibold text-slate-900 text-sm">LibraryAI</span>
            </div>
            <p className="text-xs text-slate-500 leading-relaxed">
              Production-grade AI pipeline for archival document digitization.
              Multi-stage processing, quality enforcement, and complete lineage.
            </p>
          </div>

          {/* Links */}
          <div className="flex flex-col sm:flex-row gap-8">
            <div>
              <p className="text-2xs font-semibold text-slate-400 uppercase tracking-wider mb-3">
                Product
              </p>
              <div className="flex flex-col gap-2">
                <a href="#features" className="text-xs text-slate-500 hover:text-slate-900 transition-colors">
                  Features
                </a>
                <a href="#how-it-works" className="text-xs text-slate-500 hover:text-slate-900 transition-colors">
                  How it works
                </a>
              </div>
            </div>
            <div>
              <p className="text-2xs font-semibold text-slate-400 uppercase tracking-wider mb-3">
                Account
              </p>
              <div className="flex flex-col gap-2">
                <Link href="/login" className="text-xs text-slate-500 hover:text-slate-900 transition-colors">
                  Log in
                </Link>
                <Link href="/signup" className="text-xs text-slate-500 hover:text-slate-900 transition-colors">
                  Sign up
                </Link>
              </div>
            </div>
          </div>
        </div>

        <div className="mt-10 pt-6 border-t border-slate-100 flex flex-col sm:flex-row items-center justify-between gap-3">
          <p className="text-2xs text-slate-400">
            &copy; {new Date().getFullYear()} LibraryAI. All rights reserved.
          </p>
          <p className="text-2xs text-slate-300">
            Document AI Operations · v2.0
          </p>
        </div>
      </div>
    </footer>
  );
}

// ─── ROOT PAGE ────────────────────────────────────────────────────────────────

export default function RootPage() {
  const { isAuthenticated, isAdmin, isLoading } = useAuth();
  const router = useRouter();

  // Redirect authenticated users to their dashboard
  useEffect(() => {
    if (isLoading) return;
    if (isAuthenticated) {
      router.replace(isAdmin ? "/admin/dashboard" : "/jobs");
    }
  }, [isAuthenticated, isAdmin, isLoading, router]);

  // While auth state loads, show nothing to avoid flash
  if (isLoading) return null;

  // Authenticated users will be redirected; don't render homepage for them
  if (isAuthenticated) return null;

  return (
    <div className="min-h-screen bg-white text-slate-900" style={{ scrollBehavior: "smooth" }}>
      <Nav />
      <Hero />
      <Features />
      <HowItWorks />
      <Trust />
      <Audience />
      <FinalCTA />
      <Footer />
    </div>
  );
}
