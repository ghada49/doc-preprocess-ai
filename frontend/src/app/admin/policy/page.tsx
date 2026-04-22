"use client";

import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { Settings, Save, RefreshCw } from "lucide-react";
import { getPolicy, updatePolicy } from "@/lib/api/policy";
import { AdminShell } from "@/components/layout/admin-shell";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { ErrorBanner } from "@/components/shared/error-banner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { getApiErrorMessage } from "@/lib/api/client";
import { formatDate } from "@/lib/utils";

type RectificationPolicy = "conditional" | "disabled_direct_review";

const RECTIFICATION_POLICY_OPTIONS: { value: RectificationPolicy; label: string; help: string }[] =
  [
    {
      value: "conditional",
      label: "Conditional",
      help: "Attempt automatic rectification (IEP1D) when the first pass is not acceptable.",
    },
    {
      value: "disabled_direct_review",
      label: "Disable and send directly to review",
      help: "Skip rectification entirely. Pages that fail the first-pass quality check are sent directly to human review.",
    },
  ];

// ── YAML helpers ──────────────────────────────────────────────────────────────

function parseRectificationPolicy(yaml: string): RectificationPolicy {
  const match = yaml.match(/^\s+rectification_policy:\s*(\S+)/m);
  const raw = match?.[1];
  if (raw === "conditional" || raw === "disabled_direct_review") return raw;
  return "conditional";
}

function setRectificationPolicyInYaml(yaml: string, value: RectificationPolicy): string {
  const line = `  rectification_policy: ${value}`;
  // Key already present — replace in place
  if (/^\s+rectification_policy:\s*\S*/m.test(yaml)) {
    return yaml.replace(/^(\s+)rectification_policy:\s*\S*/m, line);
  }
  // preprocessing: section exists — insert as first child
  if (/^preprocessing:/m.test(yaml)) {
    return yaml.replace(/^(preprocessing:.*)$/m, `$1\n${line}`);
  }
  // No preprocessing section — append one
  const trimmed = yaml.trimEnd();
  return `${trimmed}\npreprocessing:\n${line}\n`;
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function PolicyPage() {
  const queryClient = useQueryClient();

  const { data: policy, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["policy"],
    queryFn: getPolicy,
    staleTime: 30_000,
  });

  const [configYaml, setConfigYaml] = useState("");
  const [justification, setJustification] = useState("");
  const [newVersion, setNewVersion] = useState("");
  const [isDirty, setIsDirty] = useState(false);

  useEffect(() => {
    if (policy) {
      setConfigYaml(policy.config_yaml);
      setNewVersion(bumpVersion(policy.version));
      setIsDirty(false);
    }
  }, [policy]);

  const saveMut = useMutation({
    mutationFn: () =>
      updatePolicy({
        config_yaml: configYaml,
        justification: justification.trim(),
        version: newVersion.trim(),
      }),
    onSuccess: (updated) => {
      toast.success(`Policy updated to ${updated.version}`);
      queryClient.invalidateQueries({ queryKey: ["policy"] });
      setJustification("");
      setIsDirty(false);
    },
    onError: (err: unknown) => {
      toast.error(getApiErrorMessage(err, "Failed to update policy."));
    },
  });

  if (isLoading) {
    return (
      <AdminShell breadcrumbs={[{ label: "Policy" }]}>
        <div className="flex items-center justify-center py-16">
          <Spinner size="lg" />
        </div>
      </AdminShell>
    );
  }

  if (isError || !policy) {
    return (
      <AdminShell breadcrumbs={[{ label: "Policy" }]}>
        <div className="p-6">
          <ErrorBanner
            variant="fullscreen"
            title="Failed to Load"
            message={getApiErrorMessage(error, "Could not load policy configuration.")}
          />
        </div>
      </AdminShell>
    );
  }

  const currentRectificationPolicy = parseRectificationPolicy(configYaml);
  const selectedOption = RECTIFICATION_POLICY_OPTIONS.find(
    (o) => o.value === currentRectificationPolicy
  )!;

  const canSave =
    isDirty &&
    configYaml.trim().length > 0 &&
    justification.trim().length > 0 &&
    newVersion.trim().length > 0 &&
    !saveMut.isPending;

  return (
    <AdminShell breadcrumbs={[{ label: "Policy" }]}>
      <div className="p-6 max-w-4xl space-y-6">
        <PageHeader
          title="Policy Configuration"
          description="View and update the active system policy. Changes are versioned and audited."
          icon={Settings}
          iconColor="text-indigo-600"
          badge={<Badge variant="info">{policy.version}</Badge>}
          actions={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => refetch()}
              className="gap-1.5 text-slate-500"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          }
        />

        {/* Current policy metadata */}
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
          <div className="grid grid-cols-3 gap-4 text-xs">
            <div>
              <p className="text-2xs text-slate-400 mb-0.5">Version</p>
              <Badge variant="info">{policy.version}</Badge>
            </div>
            <div>
              <p className="text-2xs text-slate-400 mb-0.5">Applied At</p>
              <p className="text-slate-700">{formatDate(policy.applied_at)}</p>
            </div>
            <div>
              <p className="text-2xs text-slate-400 mb-0.5">Applied By</p>
              <p className="text-slate-700">{policy.applied_by ?? "—"}</p>
            </div>
          </div>
          {policy.justification && (
            <div className="mt-4 pt-4 border-t border-slate-200">
              <p className="text-2xs text-slate-400 mb-1">Justification</p>
              <p className="text-xs text-slate-500 italic">
                &ldquo;{policy.justification}&rdquo;
              </p>
            </div>
          )}
        </div>

        {/* Editor */}
        <div className="bg-white border border-slate-200 rounded-xl p-5 space-y-4 shadow-sm">
          <h2 className="text-sm font-semibold text-slate-800">Edit Policy</h2>

          {/* Rectification policy toggle */}
          <div className="space-y-1.5">
            <Label>Rectification Policy</Label>
            <Select
              value={currentRectificationPolicy}
              onValueChange={(val) => {
                const updated = setRectificationPolicyInYaml(
                  configYaml,
                  val as RectificationPolicy
                );
                setConfigYaml(updated);
                setIsDirty(true);
              }}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RECTIFICATION_POLICY_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-slate-500">{selectedOption.help}</p>
            {currentRectificationPolicy === "disabled_direct_review" && (
              <p className="text-xs text-amber-600 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
                Pages that fail the first-pass quality check will skip rectification and go
                directly to human review. Pages already in the{" "}
                <span className="font-mono">rectification</span> state will not be affected.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label>Configuration YAML</Label>
            <Textarea
              value={configYaml}
              onChange={(e) => {
                setConfigYaml(e.target.value);
                setIsDirty(true);
              }}
              className="font-mono text-xs min-h-[320px]"
              placeholder="# YAML configuration…"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <Label>New Version Tag</Label>
              <Input
                value={newVersion}
                onChange={(e) => setNewVersion(e.target.value)}
                placeholder="e.g. v2"
                className="font-mono"
              />
            </div>
            <div className="space-y-1.5">
              <Label>
                Justification <span className="text-red-500">*</span>
              </Label>
              <Input
                value={justification}
                onChange={(e) => setJustification(e.target.value)}
                placeholder="Reason for this policy change…"
              />
            </div>
          </div>

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-200">
            {isDirty && (
              <p className="text-xs text-amber-600 flex-1">Unsaved changes</p>
            )}
            <Button
              onClick={() => {
                setConfigYaml(policy.config_yaml);
                setIsDirty(false);
              }}
              variant="ghost"
              size="sm"
              disabled={!isDirty}
            >
              Discard
            </Button>
            <Button
              onClick={() => saveMut.mutate()}
              loading={saveMut.isPending}
              disabled={!canSave}
              size="sm"
              className="gap-2"
            >
              <Save className="h-4 w-4" />
              Save Policy
            </Button>
          </div>
        </div>
      </div>
    </AdminShell>
  );
}

function bumpVersion(version: string): string {
  const match = version.match(/^v?(\d+)$/);
  if (match) return `v${parseInt(match[1]) + 1}`;
  return `${version}-new`;
}
