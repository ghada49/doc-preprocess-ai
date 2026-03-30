"use client";

import { useParams } from "next/navigation";
import { UserShell } from "@/components/layout/user-shell";
import PtiffQaPanel from "@/components/ptiff-qa/ptiff-qa-panel";
import { truncateId } from "@/lib/utils";

export default function PtiffQaPage() {
  const { job_id } = useParams<{ job_id: string }>();

  return (
    <UserShell
      breadcrumbs={[
        { label: "My Jobs", href: "/jobs" },
        { label: truncateId(job_id, 8) + "…", href: `/jobs/${job_id}` },
        { label: "PTIFF QA" },
      ]}
    >
      <PtiffQaPanel jobId={job_id} backPath={`/jobs/${job_id}`} />
    </UserShell>
  );
}
