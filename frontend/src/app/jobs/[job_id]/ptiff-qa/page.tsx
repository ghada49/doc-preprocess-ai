"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import { LayoutGrid, Images } from "lucide-react";
import { UserShell } from "@/components/layout/user-shell";
import PtiffQaPanel from "@/components/ptiff-qa/ptiff-qa-panel";
import PtiffQaViewer from "@/components/ptiff-qa/ptiff-qa-viewer";
import { truncateId } from "@/lib/utils";
import { cn } from "@/lib/utils";

type Tab = "overview" | "viewer";

export default function PtiffQaPage() {
  const { job_id } = useParams<{ job_id: string }>();
  const [activeTab, setActiveTab] = useState<Tab>("overview");

  return (
    <UserShell
      breadcrumbs={[
        { label: "Documents", href: "/jobs" },
        { label: truncateId(job_id, 8), href: `/jobs/${job_id}` },
        { label: "Review results" },
      ]}
    >
      <div className="max-w-6xl space-y-4 p-6">
        <div className="flex items-center gap-1 border-b border-slate-200">
          <TabButton
            active={activeTab === "overview"}
            onClick={() => setActiveTab("overview")}
            icon={<LayoutGrid className="h-3.5 w-3.5" />}
            label="Overview"
          />
          <TabButton
            active={activeTab === "viewer"}
            onClick={() => setActiveTab("viewer")}
            icon={<Images className="h-3.5 w-3.5" />}
            label="Page viewer"
          />
        </div>

        {activeTab === "overview" ? (
          <PtiffQaPanel jobId={job_id} backPath={`/jobs/${job_id}`} />
        ) : (
          <PtiffQaViewer jobId={job_id} />
        )}
      </div>
    </UserShell>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors",
        active
          ? "border-indigo-600 text-indigo-700"
          : "border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300"
      )}
    >
      {icon}
      {label}
    </button>
  );
}
