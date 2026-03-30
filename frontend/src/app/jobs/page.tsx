import type { Metadata } from "next";
import { Briefcase } from "lucide-react";
import { UserShell } from "@/components/layout/user-shell";
import { JobsTable } from "@/components/jobs/jobs-table";
import { PageHeader } from "@/components/shared/page-header";

export const metadata: Metadata = { title: "My Jobs" };

export default function MyJobsPage() {
  return (
    <UserShell breadcrumbs={[{ label: "My Jobs" }]}>
      <div className="p-6 space-y-5">
        <PageHeader
          title="My Jobs"
          description="Track the status of your document processing jobs."
          icon={Briefcase}
        />
        <JobsTable basePath="/jobs" />
      </div>
    </UserShell>
  );
}
