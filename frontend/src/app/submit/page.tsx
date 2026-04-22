"use client";

import { useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { Upload, X, FileType, PlusCircle, CheckCircle } from "lucide-react";
import { UserShell } from "@/components/layout/user-shell";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Progress } from "@/components/ui/progress";
import { getApiErrorMessage } from "@/lib/api/client";
import { uploadFile } from "@/lib/api/upload";
import { createJob } from "@/lib/api/jobs";
import type { PipelineMode } from "@/types/api";
import { cn } from "@/lib/utils";

interface FileUploadState {
  file: File;
  progress: number;
  objectUri?: string;
  error?: string;
}

export default function SubmitJobPage() {
  const router = useRouter();
  const queryClient = useQueryClient();

  // Form state
  const [collectionId, setCollectionId] = useState("");
  const [pipelineMode, setPipelineMode] = useState<PipelineMode>("layout");
  const [policyVersion, setPolicyVersion] = useState("v1");
  const [shadowMode, setShadowMode] = useState(false);

  // File upload state
  const [files, setFiles] = useState<FileUploadState[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadsDone, setUploadsDone] = useState(false);

  const handleFileDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const dropped = Array.from(e.dataTransfer.files).filter(
      (f) => f.name.toLowerCase().endsWith(".tiff") || f.name.toLowerCase().endsWith(".tif")
    );
    setFiles((prev) => [
      ...prev,
      ...dropped.map((f) => ({ file: f, progress: 0 })),
    ]);
    setUploadsDone(false);
  }, []);

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files ?? []);
    setFiles((prev) => [
      ...prev,
      ...selected.map((f) => ({ file: f, progress: 0 })),
    ]);
    setUploadsDone(false);
  };

  const removeFile = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
    setUploadsDone(false);
  };

  const handleUploadFiles = async () => {
    if (files.length === 0) return;
    setUploading(true);
    setUploadsDone(false);

    const results = await Promise.allSettled(
      files.map((f, idx) =>
        uploadFile(f.file, idx + 1, (pct) => {
          setFiles((prev) =>
            prev.map((item, i) => (i === idx ? { ...item, progress: pct } : item))
          );
        }).then((result) => {
          setFiles((prev) =>
            prev.map((item, i) =>
              i === idx ? { ...item, objectUri: result.objectUri, progress: 100 } : item
            )
          );
          return result;
        })
      )
    );

    const hasErrors = results.some((r) => r.status === "rejected");
    if (hasErrors) {
      results.forEach((r, i) => {
        if (r.status === "rejected") {
          setFiles((prev) =>
            prev.map((item, idx) =>
              idx === i ? { ...item, error: "Upload failed" } : item
            )
          );
        }
      });
      toast.error("Some files failed to upload. Please retry.");
    } else {
      setUploadsDone(true);
      toast.success(`${files.length} file${files.length > 1 ? "s" : ""} uploaded successfully.`);
    }
    setUploading(false);
  };

  const submitMut = useMutation({
    mutationFn: () => {
      const pages = files
        .filter((f) => f.objectUri)
        .map((f, idx) => ({ page_number: idx + 1, input_uri: f.objectUri! }));
      return createJob({
        collection_id: collectionId,
        pages,
        pipeline_mode: pipelineMode,
        policy_version: policyVersion,
        shadow_mode: shadowMode,
      });
    },
    onSuccess: (result) => {
      toast.success(`Job created — ${result.page_count} pages queued.`);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      router.push(`/jobs/${result.job_id}`);
    },
    onError: (err: unknown) => {
      toast.error(getApiErrorMessage(err, "Failed to create job."));
    },
  });

  const canSubmit =
    uploadsDone &&
    collectionId.trim().length > 0 &&
    files.every((f) => !!f.objectUri) &&
    files.length > 0 &&
    !submitMut.isPending;

  return (
    <UserShell breadcrumbs={[{ label: "My Jobs", href: "/jobs" }, { label: "Submit Job" }]}>
      <div className="p-6 max-w-2xl space-y-6">
        <PageHeader
          title="Submit Job"
          description="Upload TIFF pages and configure your processing pipeline."
          icon={PlusCircle}
        />

        {/* Job Configuration */}
        <div className="bg-white border border-slate-200 rounded-xl p-6 space-y-5 shadow-sm">
          <h2 className="text-sm font-semibold text-slate-800">Job Configuration</h2>

          <div className="grid grid-cols-2 gap-4">
            <div className="col-span-2 space-y-1.5">
              <Label>Collection ID</Label>
              <Input
                value={collectionId}
                onChange={(e) => setCollectionId(e.target.value)}
                placeholder="e.g. coll-2026-manuscript-01"
              />
            </div>

            <div className="space-y-1.5">
              <p className="text-xs text-slate-500 italic">Material type is auto-detected by IEP0</p>
            </div>

            <div className="space-y-1.5">
              <Label>Pipeline Mode</Label>
              <Select
                value={pipelineMode}
                onValueChange={(v) => setPipelineMode(v as PipelineMode)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="layout">Layout (full pipeline)</SelectItem>
                  <SelectItem value="preprocess">Preprocess only</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label>Policy Version</Label>
              <Input
                value={policyVersion}
                onChange={(e) => setPolicyVersion(e.target.value)}
                placeholder="e.g. v1"
              />
            </div>

            <div className="col-span-2 flex items-center justify-between py-2">
              <div>
                <p className="text-sm text-slate-800 font-medium">Shadow Mode</p>
                <p className="text-xs text-slate-500">Run in shadow without affecting production</p>
              </div>
              <Switch checked={shadowMode} onCheckedChange={setShadowMode} />
            </div>
          </div>
        </div>

        {/* File Upload */}
        <div className="bg-white border border-slate-200 rounded-xl p-6 space-y-4 shadow-sm">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-800">
              Page Files
              {files.length > 0 && (
                <span className="ml-2 text-xs text-slate-400 font-normal">
                  {files.length} file{files.length !== 1 ? "s" : ""}
                </span>
              )}
            </h2>
          </div>

          {/* Drop zone */}
          <div
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleFileDrop}
            className="relative border-2 border-dashed border-slate-300 rounded-xl p-8 text-center hover:border-indigo-500 transition-colors group"
          >
            <Upload className="h-8 w-8 text-slate-300 mx-auto mb-3 group-hover:text-indigo-500 transition-colors" />
            <p className="text-sm text-slate-500 mb-1">Drag TIFF files here</p>
            <p className="text-xs text-slate-400 mb-4">or</p>
            <label className="cursor-pointer">
              <span className="inline-flex items-center gap-1.5 rounded-lg bg-white border border-slate-300 hover:bg-slate-50 transition-colors px-3 py-1.5 text-xs text-slate-600 font-medium">
                <FileType className="h-3.5 w-3.5" />
                Browse files
              </span>
              <input
                type="file"
                accept=".tiff,.tif"
                multiple
                onChange={handleFileInput}
                className="sr-only"
              />
            </label>
            <p className="text-2xs text-slate-400 mt-3">Accepts .tiff and .tif files</p>
          </div>

          {/* File list */}
          {files.length > 0 && (
            <div className="space-y-2">
              {files.map((f, idx) => (
                <div
                  key={idx}
                  className={cn(
                    "flex items-center gap-3 rounded-lg border px-3 py-2.5",
                    f.error
                      ? "border-red-200 bg-red-50"
                      : f.objectUri
                      ? "border-emerald-200 bg-emerald-50/60"
                      : "border-slate-200 bg-slate-50"
                  )}
                >
                  <FileType className="h-4 w-4 text-slate-400 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-slate-700 truncate">{f.file.name}</p>
                    <p className="text-2xs text-slate-400">
                      {(f.file.size / 1024 / 1024).toFixed(1)} MB · Page {idx + 1}
                    </p>
                    {uploading && f.progress > 0 && f.progress < 100 && (
                      <Progress value={f.progress} className="mt-1.5 h-1" />
                    )}
                    {f.error && (
                      <p className="text-2xs text-red-600 mt-0.5">{f.error}</p>
                    )}
                  </div>
                  <div className="shrink-0">
                    {f.objectUri ? (
                      <CheckCircle className="h-4 w-4 text-emerald-500" />
                    ) : f.error ? (
                      <span className="text-2xs text-red-600">Failed</span>
                    ) : null}
                  </div>
                  {!uploading && !f.objectUri && (
                    <button
                      onClick={() => removeFile(idx)}
                      className="text-slate-400 hover:text-slate-600 transition-colors shrink-0"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Upload button */}
          {files.length > 0 && !uploadsDone && (
            <Button
              variant="secondary"
              onClick={handleUploadFiles}
              loading={uploading}
              disabled={uploading}
              className="w-full gap-2"
            >
              <Upload className="h-4 w-4" />
              {uploading
                ? "Uploading…"
                : `Upload ${files.length} file${files.length !== 1 ? "s" : ""}`}
            </Button>
          )}

          {uploadsDone && (
            <div className="flex items-center gap-2 text-emerald-600 text-sm">
              <CheckCircle className="h-4 w-4" />
              All files uploaded — ready to submit
            </div>
          )}
        </div>

        {/* Submit */}
        <div className="flex items-center justify-between gap-4">
          <Button
            variant="ghost"
            onClick={() => router.push("/jobs")}
          >
            Cancel
          </Button>
          <Button
            onClick={() => submitMut.mutate()}
            loading={submitMut.isPending}
            disabled={!canSubmit}
            className="gap-2 min-w-[140px]"
          >
            <PlusCircle className="h-4 w-4" />
            Create Job
          </Button>
        </div>
      </div>
    </UserShell>
  );
}
