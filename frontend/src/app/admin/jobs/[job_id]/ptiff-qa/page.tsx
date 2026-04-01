"use client";

import { useParams } from "next/navigation";
import { AdminShell } from "@/components/layout/admin-shell";
import PtiffQaPanel from "@/components/ptiff-qa/ptiff-qa-panel";
import { truncateId } from "@/lib/utils";

export default function AdminPtiffQaPage() {
  const { job_id } = useParams<{ job_id: string }>();

  return (
    <AdminShell
      breadcrumbs={[
        { label: "Jobs", href: "/admin/jobs" },
        { label: truncateId(job_id, 8) + "…", href: `/admin/jobs/${job_id}` },
        { label: "PTIFF QA" },
      ]}
    >
      <PtiffQaPanel jobId={job_id} backPath={`/admin/jobs/${job_id}`} />
    </AdminShell>
  );
}
