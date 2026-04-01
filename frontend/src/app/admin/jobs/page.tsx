import type { Metadata } from "next";
import { Briefcase } from "lucide-react";
import { AdminShell } from "@/components/layout/admin-shell";
import { JobsTable } from "@/components/jobs/jobs-table";
import { PageHeader } from "@/components/shared/page-header";

export const metadata: Metadata = { title: "All Jobs" };

export default function AdminJobsPage() {
  return (
    <AdminShell breadcrumbs={[{ label: "Jobs" }]}>
      <div className="p-6 space-y-5">
        <PageHeader
          title="All Jobs"
          description="Global view of all processing jobs across all users."
          icon={Briefcase}
          iconColor="text-indigo-400"
        />
        <JobsTable isAdmin basePath="/admin/jobs" />
      </div>
    </AdminShell>
  );
}
