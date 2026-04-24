import type { Metadata } from "next";
import { ClipboardList } from "lucide-react";
import { UserShell } from "@/components/layout/user-shell";
import { PageHeader } from "@/components/shared/page-header";
import { CorrectionQueueTable } from "@/components/correction/queue-table";

export const metadata: Metadata = { title: "Needs Review" };

export default function MyQueuePage() {
  return (
    <UserShell breadcrumbs={[{ label: "Needs Review" }]}>
      <div className="relative z-10 space-y-5 p-6">
        <PageHeader
          title="Needs review"
          description="Review pages that could not be processed automatically."
          icon={ClipboardList}
          iconColor="text-orange-400"
        />
        <CorrectionQueueTable workspacePath="/queue" />
      </div>
    </UserShell>
  );
}
