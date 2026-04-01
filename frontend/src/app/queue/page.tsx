import type { Metadata } from "next";
import { ClipboardList } from "lucide-react";
import { UserShell } from "@/components/layout/user-shell";
import { PageHeader } from "@/components/shared/page-header";
import { CorrectionQueueTable } from "@/components/correction/queue-table";

export const metadata: Metadata = { title: "Correction Queue" };

export default function MyQueuePage() {
  return (
    <UserShell breadcrumbs={[{ label: "Correction Queue" }]}>
      <div className="p-6 space-y-5">
        <PageHeader
          title="My Correction Queue"
          description="Pages from your jobs that require human review."
          icon={ClipboardList}
          iconColor="text-orange-400"
        />
        <CorrectionQueueTable workspacePath="/queue" />
      </div>
    </UserShell>
  );
}
