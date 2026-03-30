"use client";

import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { GitBranch } from "lucide-react";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { LineageView } from "@/components/lineage/lineage-view";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { truncateId } from "@/lib/utils";

export default function LineagePage() {
  const searchParams = useSearchParams();
  const { job_id, page_number } = useParams<{
    job_id: string;
    page_number: string;
  }>();

  const [subPageIndex, setSubPageIndex] = useState<string>("");

  useEffect(() => {
    setSubPageIndex(searchParams.get("sub_page_index") ?? "");
  }, [searchParams]);

  return (
    <AdminShell
      breadcrumbs={[
        { label: "Lineage" },
        { label: `Job ${truncateId(job_id, 8)}…` },
        { label: `Page ${page_number}` },
      ]}
    >
      <div className="p-6 space-y-5">
        <div className="flex items-start justify-between gap-4">
          <PageHeader
            title="Page Lineage"
            description="Complete audit trail for this page — invocations, gate decisions, corrections."
            icon={GitBranch}
            iconColor="text-indigo-600"
          />
          {/* Sub-page selector */}
          <div className="flex items-center gap-2 shrink-0">
            <Label className="text-slate-500 text-xs whitespace-nowrap">Sub-page index</Label>
            <Input
              type="number"
              min={0}
              value={subPageIndex}
              onChange={(e) => setSubPageIndex(e.target.value)}
              placeholder="—"
              className="w-20 h-8 text-xs"
            />
          </div>
        </div>

        <LineageView
          jobId={job_id}
          pageNumber={parseInt(page_number, 10)}
          subPageIndex={subPageIndex ? parseInt(subPageIndex, 10) : undefined}
        />
      </div>
    </AdminShell>
  );
}
