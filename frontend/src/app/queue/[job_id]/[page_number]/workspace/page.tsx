"use client";

import { Suspense } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { UserShell } from "@/components/layout/user-shell";
import { CorrectionWorkspace } from "@/components/correction/workspace";
import { Spinner } from "@/components/ui/spinner";

function WorkspaceContent({ jobId, pageNumber }: { jobId: string; pageNumber: string }) {
  const searchParams = useSearchParams();
  const subPageIndexParam = searchParams.get("sub_page_index");
  const subPageIndex =
    subPageIndexParam != null ? parseInt(subPageIndexParam, 10) : undefined;

  return (
    <CorrectionWorkspace
      key={`${jobId}:${pageNumber}:${subPageIndex ?? "root"}`}
      jobId={jobId}
      pageNumber={parseInt(pageNumber, 10)}
      subPageIndex={Number.isNaN(subPageIndex) ? undefined : subPageIndex}
      backPath="/queue"
      isAdmin={false}
    />
  );
}

export default function UserWorkspacePage() {
  const { job_id, page_number } = useParams<{
    job_id: string;
    page_number: string;
  }>();

  return (
    <UserShell
      breadcrumbs={[
        { label: "Queue", href: "/queue" },
        { label: "Workspace" },
      ]}
      className="p-0 overflow-hidden"
    >
      <div className="h-full">
        <Suspense
          fallback={
            <div className="flex h-full min-h-[600px] items-center justify-center">
              <Spinner size="lg" />
            </div>
          }
        >
          <WorkspaceContent jobId={job_id} pageNumber={page_number} />
        </Suspense>
      </div>
    </UserShell>
  );
}
