import type { Metadata } from "next";
import Link from "next/link";
import { Briefcase, PlusCircle } from "lucide-react";
import { UserShell } from "@/components/layout/user-shell";
import { JobsTable } from "@/components/jobs/jobs-table";
import { PageHeader } from "@/components/shared/page-header";

export const metadata: Metadata = { title: "Documents" };

export default function MyJobsPage() {
  return (
    <UserShell breadcrumbs={[{ label: "Documents" }]}>
      <div className="relative z-10 space-y-5 p-6">
        <PageHeader
          title="Documents"
          description="Track uploads, review pages that need attention, and download finished results."
          icon={Briefcase}
          actions={
            <Link
              href="/submit"
              className="inline-flex h-10 items-center justify-center gap-2 rounded-xl bg-slate-950 px-4 text-sm font-semibold text-white shadow-sm shadow-slate-900/20 transition-all hover:-translate-y-0.5 hover:bg-slate-800"
            >
              <PlusCircle className="h-4 w-4" />
              Upload documents
            </Link>
          }
        />
        <JobsTable basePath="/jobs" />
      </div>
    </UserShell>
  );
}
