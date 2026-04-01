"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { GitBranch, Search } from "lucide-react";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function LineageIndexPage() {
  const router = useRouter();
  const [jobId, setJobId] = useState("");
  const [pageNumber, setPageNumber] = useState("1");

  const handleSearch = () => {
    if (jobId.trim() && pageNumber) {
      router.push(`/admin/lineage/${jobId.trim()}/${pageNumber}`);
    }
  };

  return (
    <AdminShell breadcrumbs={[{ label: "Lineage" }]}>
      <div className="p-6 max-w-lg space-y-6">
        <PageHeader
          title="Lineage Inspector"
          description="Look up the complete audit trail for any page by job ID and page number."
          icon={GitBranch}
          iconColor="text-indigo-600"
        />

        <div className="bg-white border border-slate-200 rounded-xl p-6 space-y-4 shadow-sm">
          <div className="space-y-1.5">
            <Label>Job ID</Label>
            <Input
              value={jobId}
              onChange={(e) => setJobId(e.target.value)}
              placeholder="e.g. 550e8400-e29b-41d4-a716-446655440000"
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            />
          </div>
          <div className="space-y-1.5">
            <Label>Page Number</Label>
            <Input
              type="number"
              min={1}
              value={pageNumber}
              onChange={(e) => setPageNumber(e.target.value)}
              placeholder="1"
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            />
          </div>
          <Button
            onClick={handleSearch}
            disabled={!jobId.trim() || !pageNumber}
            className="w-full gap-2"
          >
            <Search className="h-4 w-4" />
            View Lineage
          </Button>
        </div>

        <p className="text-xs text-slate-400 text-center">
          Navigate to a job&apos;s detail page and click &ldquo;Lineage&rdquo; on any page row for quick access.
        </p>
      </div>
    </AdminShell>
  );
}
