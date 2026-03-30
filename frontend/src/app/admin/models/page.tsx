import type { Metadata } from "next";
import { FlaskConical } from "lucide-react";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { ModelEvalDashboard } from "@/components/models/eval-dashboard";

export const metadata: Metadata = { title: "Model Evaluation" };

export default function ModelEvalPage() {
  return (
    <AdminShell breadcrumbs={[{ label: "Model Evaluation" }]}>
      <div className="p-6 space-y-5">
        <PageHeader
          title="Model Evaluation"
          description="Candidate model records, gate results, promotion, and rollback controls."
          icon={FlaskConical}
          iconColor="text-indigo-400"
        />
        <ModelEvalDashboard />
      </div>
    </AdminShell>
  );
}
