"use client";

import { useParams, useSearchParams } from "next/navigation";
import { UserShell } from "@/components/layout/user-shell";
import { CorrectionWorkspace } from "@/components/correction/workspace";

export default function UserWorkspacePage() {
  const searchParams = useSearchParams();
  const { job_id, page_number } = useParams<{
    job_id: string;
    page_number: string;
  }>();
  const subPageIndexParam = searchParams.get("sub_page_index");
  const subPageIndex =
    subPageIndexParam != null ? parseInt(subPageIndexParam, 10) : undefined;

  return (
    <UserShell
      breadcrumbs={[
        { label: "Queue", href: "/queue" },
        { label: "Workspace" },
      ]}
      className="p-0 overflow-hidden"
    >
      <div className="h-full">
        <CorrectionWorkspace
          key={`${job_id}:${page_number}:${subPageIndex ?? "root"}`}
          jobId={job_id}
          pageNumber={parseInt(page_number, 10)}
          subPageIndex={Number.isNaN(subPageIndex) ? undefined : subPageIndex}
          backPath="/queue"
          isAdmin={false}
        />
      </div>
    </UserShell>
  );
}
