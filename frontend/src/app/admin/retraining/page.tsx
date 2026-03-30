import type { Metadata } from "next";
import { RefreshCw } from "lucide-react";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { RetrainingView } from "@/components/models/retraining-view";

export const metadata: Metadata = { title: "Retraining" };

export default function RetrainingPage() {
  return (
    <AdminShell breadcrumbs={[{ label: "Retraining" }]}>
      <div className="p-6 space-y-5">
        <PageHeader
          title="Retraining Pipeline"
          description="Active and queued retraining jobs, trigger status, and cooldown state."
          icon={RefreshCw}
          iconColor="text-blue-400"
        />
        <RetrainingView />
      </div>
    </AdminShell>
  );
}
