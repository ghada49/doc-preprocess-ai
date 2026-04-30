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
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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

const DEFAULT_POLICY_VERSION = "v1";
const DEFAULT_SHADOW_MODE = false;

export default function SubmitJobPage() {
  const router = useRouter();
  const queryClient = useQueryClient();

  const [collectionId, setCollectionId] = useState("");
  const [pipelineMode, setPipelineMode] = useState<PipelineMode | "">("");
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
    e.target.value = "";
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
    setFiles((prev) =>
      prev.map((item) => ({
        ...item,
        error: undefined,
        progress: item.objectUri ? 100 : 0,
      }))
    );

    const results = await Promise.allSettled(
      files.map((f, idx) =>
        uploadFile(f.file, idx + 1, (pct) => {
          setFiles((prev) =>
            prev.map((item, i) =>
              i === idx ? { ...item, error: undefined, progress: pct } : item
            )
          );
        }).then((result) => {
          setFiles((prev) =>
            prev.map((item, i) =>
              i === idx
                ? { ...item, error: undefined, objectUri: result.objectUri, progress: 100 }
                : item
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
      toast.error("Some files could not be uploaded. Please try again.");
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
        pipeline_mode: pipelineMode as PipelineMode,
        policy_version: DEFAULT_POLICY_VERSION,
        shadow_mode: DEFAULT_SHADOW_MODE,
      });
    },
    onSuccess: (result) => {
      toast.success(`Upload started. ${result.page_count} page${result.page_count !== 1 ? "s" : ""} queued.`);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      router.push(`/jobs/${result.job_id}`);
    },
    onError: () => {
      toast.error("We could not start processing. Please try again.");
    },
  });

  const canSubmit =
    uploadsDone &&
    collectionId.trim().length > 0 &&
    pipelineMode.length > 0 &&
    files.every((f) => !!f.objectUri) &&
    files.length > 0 &&
    !submitMut.isPending;

  return (
    <UserShell breadcrumbs={[{ label: "Documents", href: "/jobs" }, { label: "Upload" }]}>
      <div className="relative z-10 max-w-6xl space-y-6 p-6">
        <PageHeader
          title="Upload documents"
          description="Add scanned pages and start processing. We will flag anything that needs review."
          icon={PlusCircle}
        />

        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="space-y-6">
        <div className="surface-panel p-6">
          <div className="mb-5">
            <h2 className="text-sm font-semibold text-slate-900">Upload details</h2>
            <p className="mt-1 text-xs leading-relaxed text-slate-500">
              Name this collection and choose the result you need.
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Collection name</Label>
              <Input
                value={collectionId}
                onChange={(e) => setCollectionId(e.target.value)}
                placeholder="e.g. April archive scans"
              />
            </div>

            <div className="space-y-1.5">
              <Label>Processing type</Label>
              <Select
                value={pipelineMode}
                onValueChange={(value) => setPipelineMode(value as PipelineMode)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Choose processing type" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="preprocess">Preprocess only</SelectItem>
                  <SelectItem value="layout">Layout</SelectItem>
                  <SelectItem value="layout_with_ocr">Layout with OCR</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-2xs text-slate-400">
                Choose the result you need for this collection.
              </p>
            </div>
          </div>
        </div>

        <div className="surface-panel space-y-4 p-6">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-800">
              Scanned pages
              {files.length > 0 && (
                <span className="ml-2 text-xs font-normal text-slate-400">
                  {files.length} file{files.length !== 1 ? "s" : ""}
                </span>
              )}
            </h2>
          </div>

          <div
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleFileDrop}
            className="group relative overflow-hidden rounded-2xl border-2 border-dashed border-slate-300 bg-gradient-to-br from-slate-50 via-white to-sky-50/60 p-10 text-center transition-all duration-200 hover:border-sky-400 hover:shadow-[0_20px_60px_-42px_rgba(14,165,233,0.55)]"
          >
            <div className="pointer-events-none absolute inset-x-12 top-0 h-px bg-gradient-to-r from-transparent via-white to-transparent" />
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-slate-200 bg-white text-slate-400 shadow-sm shadow-slate-200/80 transition-all duration-200 group-hover:-translate-y-0.5 group-hover:text-sky-500 group-hover:shadow-md">
              <Upload className="h-7 w-7" />
            </div>
            <p className="mb-1 text-sm font-medium text-slate-700">Drag scanned pages here</p>
            <p className="mb-4 text-xs text-slate-500">or choose files from your computer</p>
            <label className="cursor-pointer">
              <span className="inline-flex items-center gap-1.5 rounded-xl border border-slate-300 bg-white px-4 py-2 text-xs font-semibold text-slate-700 shadow-sm shadow-slate-200/80 transition-all hover:-translate-y-0.5 hover:bg-slate-50">
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
            <p className="mt-3 text-2xs text-slate-400">Accepts .tiff and .tif files</p>
          </div>

          {files.length > 0 && (
            <div className="space-y-2">
              {files.map((f, idx) => (
                <div
                  key={idx}
                  className={cn(
                    "flex items-center gap-3 rounded-xl border px-3 py-3 shadow-sm transition-colors",
                    f.error
                      ? "border-red-200 bg-red-50"
                      : f.objectUri
                        ? "border-emerald-200 bg-emerald-50/70"
                        : "border-slate-200 bg-slate-50"
                  )}
                >
                  <FileType className="h-4 w-4 shrink-0 text-slate-400" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs text-slate-700">{f.file.name}</p>
                    <p className="text-2xs text-slate-400">
                      {(f.file.size / 1024 / 1024).toFixed(1)} MB - Page {idx + 1}
                    </p>
                    {uploading && f.progress > 0 && f.progress < 100 && (
                      <Progress value={f.progress} className="mt-1.5 h-1" />
                    )}
                    {f.error && (
                      <p className="mt-0.5 text-2xs text-red-600">{f.error}</p>
                    )}
                  </div>
                  <div className="shrink-0">
                    {f.objectUri ? (
                      <CheckCircle className="h-4 w-4 text-emerald-500" />
                    ) : f.error ? (
                      <span className="text-2xs text-red-600">Issue</span>
                    ) : null}
                  </div>
                  {!uploading && !f.objectUri && (
                    <button
                      onClick={() => removeFile(idx)}
                      className="shrink-0 text-slate-400 transition-colors hover:text-slate-600"
                      aria-label={`Remove ${f.file.name}`}
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

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
                ? "Uploading..."
                : `Upload ${files.length} file${files.length !== 1 ? "s" : ""}`}
            </Button>
          )}

          {uploadsDone && (
            <div className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
              <CheckCircle className="h-4 w-4" />
              All files uploaded. Ready to start processing.
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-4">
          <Button variant="ghost" onClick={() => router.push("/jobs")}>
            Cancel
          </Button>
          <Button
            onClick={() => submitMut.mutate()}
            loading={submitMut.isPending}
            disabled={!canSubmit}
            className="min-w-[160px] gap-2"
          >
            <PlusCircle className="h-4 w-4" />
            Start processing
          </Button>
        </div>
          </div>

          <aside className="soft-panel h-fit p-5 lg:sticky lg:top-24">
            <div className="mb-5">
              <p className="text-sm font-semibold text-slate-900">Setup</p>
              <p className="mt-1 text-xs leading-relaxed text-slate-500">
                Complete these steps to start processing.
              </p>
            </div>
            <div className="space-y-3">
              <SetupStep
                complete={collectionId.trim().length > 0}
                title="Collection name"
                description={collectionId.trim() || "Add a name"}
              />
              <SetupStep
                complete={pipelineMode.length > 0}
                title="Processing type"
                description={pipelineMode ? pipelineModeLabel(pipelineMode) : "Choose a type"}
              />
              <SetupStep
                complete={files.length > 0}
                title="Scanned pages"
                description={
                  files.length > 0
                    ? `${files.length} file${files.length !== 1 ? "s" : ""} selected`
                    : "Choose TIFF files"
                }
              />
              <SetupStep
                complete={uploadsDone}
                title="Upload"
                description={uploadsDone ? "Files uploaded" : "Ready when files are selected"}
              />
            </div>
          </aside>
        </div>
      </div>
    </UserShell>
  );
}

function SetupStep({
  complete,
  title,
  description,
}: {
  complete: boolean;
  title: string;
  description: string;
}) {
  return (
    <div
      className={cn(
        "flex gap-3 rounded-xl border px-3 py-3 transition-colors",
        complete ? "border-emerald-200 bg-emerald-50/70" : "border-slate-200 bg-white/80"
      )}
    >
      <div
        className={cn(
          "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border",
          complete
            ? "border-emerald-200 bg-emerald-500 text-white"
            : "border-slate-200 bg-slate-50 text-slate-300"
        )}
      >
        {complete ? <CheckCircle className="h-4 w-4" /> : <span className="h-2 w-2 rounded-full bg-current" />}
      </div>
      <div className="min-w-0">
        <p className="text-xs font-semibold text-slate-800">{title}</p>
        <p className="mt-0.5 truncate text-xs text-slate-500">{description}</p>
      </div>
    </div>
  );
}

function pipelineModeLabel(mode: PipelineMode): string {
  if (mode === "preprocess") return "Preprocess only";
  if (mode === "layout") return "Layout";
  return "Layout with OCR";
}
