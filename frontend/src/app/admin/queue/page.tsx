import type { Metadata } from "next";
import { ClipboardList } from "lucide-react";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { CorrectionQueueTable } from "@/components/correction/queue-table";

export const metadata: Metadata = { title: "Global Correction Queue" };

export default function AdminQueuePage() {
  return (
    <AdminShell breadcrumbs={[{ label: "Correction Queue" }]}>
      <div className="p-6 space-y-5">
        <PageHeader
          title="Global Correction Queue"
          description="All pages currently awaiting human correction across all jobs and users."
          icon={ClipboardList}
          iconColor="text-orange-400"
        />
        <CorrectionQueueTable isAdmin workspacePath="/admin/queue" />
      </div>
    </AdminShell>
  );
}
