"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import {
  AlertTriangle,
  CheckCircle,
  ChevronLeft,
  Eye,
  GitBranch,
  Info,
  XCircle,
} from "lucide-react";
import {
  getCorrectionWorkspace,
  rejectPage,
  submitCorrection,
} from "@/lib/api/correction";
import type {
  CorrectionWorkspaceDetail,
  PageStructure,
  QuadPoint,
} from "@/types/api";
import { reviewReasonLabel, snakeToTitle, truncateId } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { getApiErrorMessage } from "@/lib/api/client";
import { useArtifactPreview } from "@/lib/artifacts";
import { ConfirmModal } from "@/components/shared/confirm-modal";
import { ErrorBanner } from "@/components/shared/error-banner";
import { ArtifactLinkButton } from "@/components/shared/artifact-link-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { ImageViewer } from "./image-viewer";
import { LayoutOverlay } from "@/components/jobs/layout-overlay";
import {
  type SourceView,
  getDefaultWorkspaceSource,
  getWorkspaceEmptyMessage,
  getWorkspaceInteractionState,
  getWorkspacePreviewErrorMessage,
  resolveWorkspaceSourceUri,
} from "./workspace-source";
import { scaleQuadPoints } from "./image-viewer-helpers";

interface WorkspaceProps {
  jobId: string;
  pageNumber: number;
  subPageIndex?: number;
  backPath?: string;
  isAdmin?: boolean;
}

export function CorrectionWorkspace({
  jobId,
  pageNumber,
  subPageIndex,
  backPath = "/queue",
  isAdmin = false,
}: WorkspaceProps) {
  const router = useRouter();
  const queryClient = useQueryClient();

  const {
    data: workspace,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["correction-workspace", jobId, pageNumber, subPageIndex],
    queryFn: () => getCorrectionWorkspace(jobId, pageNumber, subPageIndex),
    staleTime: 0,
    retry: false,
  });

  const [activeSource, setActiveSource] = useState<SourceView>("current");
  const [quadPoints, setQuadPoints] = useState<QuadPoint[] | null>(null);
  // splitX is stored in original image pixel coordinates (not preview pixels).
  const [splitX, setSplitX] = useState<number | null>(null);
  // previewNaturalWidth: width of the preview PNG currently displayed (≤ 2400px).
  // Used to scale splitX between original pixels and preview pixels.
  const [previewNaturalWidth, setPreviewNaturalWidth] = useState<number | null>(null);
  const [previewNaturalHeight, setPreviewNaturalHeight] = useState<number | null>(null);
  const [pageStructure, setPageStructure] = useState<PageStructure>("single");
  const [reviewerNotes, setReviewerNotes] = useState("");
  const [showRejectModal, setShowRejectModal] = useState(false);

  const activeUri = resolveWorkspaceSourceUri(workspace, activeSource);
  const {
    data: viewerData,
    isLoading: viewerLoading,
    isError: viewerIsError,
  } = useArtifactPreview(
    activeUri,
    { maxWidth: 2400 },
    {
      scopeKey: [jobId, pageNumber, subPageIndex ?? "root", activeSource],
      staleTimeMs: 0,
      refetchOnMount: "always",
    }
  );

  useEffect(() => {
    setActiveSource("current");
  }, [jobId, pageNumber, subPageIndex]);

  // Reset preview size whenever the displayed image URL changes so that the
  // split scale is recomputed against the newly loaded preview.
  useEffect(() => {
    setPreviewNaturalWidth(null);
    setPreviewNaturalHeight(null);
  }, [activeUri]);

  useEffect(() => {
    if (!workspace) return;
    // Initialize quad from quad_points, or convert crop_box to quad if no quad_points
    if (workspace.current_quad_points) {
      setQuadPoints(workspace.current_quad_points);
    } else if (workspace.current_crop_box) {
      const [x1, y1, x2, y2] = workspace.current_crop_box;
      setQuadPoints([[x1, y1], [x2, y1], [x2, y2], [x1, y2]]);
    } else {
      setQuadPoints(null);
    }
    setSplitX(workspace.current_split_x ?? null);
    setPageStructure(workspace.suggested_page_structure ?? "single");
    setActiveSource((current) => getDefaultWorkspaceSource(workspace, current));
  }, [workspace]);

  const workspacePathForSubPage = (nextSubPageIndex: number) =>
    `${backPath}/${jobId}/${pageNumber}/workspace?sub_page_index=${nextSubPageIndex}`;
  const jobDetailPath = `${isAdmin ? "/admin/jobs" : "/jobs"}/${jobId}`;

  const submitMut = useMutation({
    mutationFn: () =>
      submitCorrection(
        jobId,
        pageNumber,
        {
          crop_box: null,
          deskew_angle: null,
          page_structure: subPageIndex == null ? pageStructure : undefined,
          // Send split_x in preview-pixel space so the backend can scale it
          // to source-image space via split_x * source_width / split_x_natural_width.
          // splitX * splitScale cancels pageImageWidth, giving the raw preview position.
          split_x: isSpreadSelection ? (splitX != null ? Math.round(splitX * splitScale) : null) : null,
          split_x_natural_width: isSpreadSelection && previewNaturalWidth != null ? Math.round(previewNaturalWidth) : undefined,
          selection_mode: !isSpreadSelection ? "quad" : undefined,
          quad_points:
            !isSpreadSelection && quadPoints
              ? quadPoints.map(([x, y]) => [Math.round(x), Math.round(y)] as QuadPoint)
              : null,
          source_artifact_uri: activeUri,
        },
        {
          subPageIndex,
          notes: reviewerNotes,
        }
      ),
    onSuccess: () => {
      toast.success("Correction submitted.");
      queryClient.invalidateQueries({ queryKey: ["correction-queue"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      if (subPageIndex == null && pageStructure === "spread") {
        const firstChildIndex = workspace?.child_pages[0]?.sub_page_index ?? 0;
        router.push(workspacePathForSubPage(firstChildIndex));
      } else {
        const nextPendingChild = workspace?.child_pages.find(
          (child) =>
            child.status === "pending_human_correction" &&
            child.sub_page_index !== subPageIndex
        );
        router.push(
          nextPendingChild
            ? workspacePathForSubPage(nextPendingChild.sub_page_index)
            : jobDetailPath
        );
      }
    },
    onError: (err: unknown) => {
      const status = (err as { status?: number })?.status;
      if (status === 409) {
        toast.error("Page is no longer pending correction.");
      } else if (status === 422) {
        toast.error(
          getApiErrorMessage(
            err,
            "Correction could not be submitted. Check the correction fields."
          )
        );
      } else {
        toast.error(getApiErrorMessage(err, "Failed to submit correction."));
      }
    },
  });

  const rejectMut = useMutation({
    mutationFn: () =>
      rejectPage(jobId, pageNumber, {
        subPageIndex,
        notes: reviewerNotes,
      }),
    onSuccess: (result) => {
      toast.success(`Page rejected -> ${snakeToTitle(result.new_state)}`);
      queryClient.invalidateQueries({ queryKey: ["correction-queue"] });
      router.push(backPath);
    },
    onError: (err) => {
      toast.error(getApiErrorMessage(err, "Failed to reject page."));
    },
  });

  if (isLoading) {
    return (
      <div className="flex h-full min-h-[600px] items-center justify-center">
        <Spinner size="lg" />
      </div>
    );
  }

  if (isError || !workspace) {
    const status = (error as { status?: number })?.status;
    return (
      <ErrorBanner
        variant="fullscreen"
        title={status === 409 ? "Page Not Available" : "Failed to Load"}
        message={
          status === 409
            ? "This page is no longer in pending_human_correction state."
            : status === 404
              ? "Page not found in the correction queue."
              : "An error occurred loading the correction workspace."
        }
      />
    );
  }

  const interactionState = getWorkspaceInteractionState(
    workspace,
    pageStructure,
    activeSource,
    activeUri
  );
  const isChildPage = interactionState.isChildPage;
  const hasChildPages = interactionState.hasChildPages;
  const isParentLineageAnchor = interactionState.isParentLineageAnchor;
  const canChoosePageStructure = interactionState.canChoosePageStructure;
  const isSpreadSelection = interactionState.isSpreadSelection;
  const canEditGeometry = interactionState.canEditGeometry;

  // Scale factor: preview pixels / original image pixels.
  // When image dimensions are unknown we fall back to 1 (legacy behaviour).
  const pageImageWidth = viewerData?.originalWidth ?? workspace.page_image_width ?? null;
  const pageImageHeight = viewerData?.originalHeight ?? workspace.page_image_height ?? null;
  const splitScale =
    pageImageWidth != null && previewNaturalWidth != null && previewNaturalWidth > 0
      ? previewNaturalWidth / pageImageWidth
      : 1;
  const quadScaleX =
    pageImageWidth != null && previewNaturalWidth != null && previewNaturalWidth > 0
      ? previewNaturalWidth / pageImageWidth
      : 1;
  const quadScaleY =
    pageImageHeight != null && previewNaturalHeight != null && previewNaturalHeight > 0
      ? previewNaturalHeight / pageImageHeight
      : 1;
  // Scale quad points to preview pixels for both child pages and single pages.
  const displayedQuadPoints = canEditGeometry
    ? scaleQuadPoints(quadPoints, quadScaleX, quadScaleY)
    : null;

  const canSubmitCorrection =
    interactionState.canSubmitCorrection &&
    !submitMut.isPending &&
    !rejectMut.isPending;
  const canEditOnDisplayedSource = interactionState.canEditOnDisplayedSource;
  const viewerEmptyMessage = getWorkspaceEmptyMessage(workspace, activeSource);
  const viewerErrorMessage = getWorkspacePreviewErrorMessage(workspace, activeSource);

  return (
    <div className="flex h-full flex-col bg-slate-50/80">
      <div className="shrink-0 border-b border-slate-200 bg-white/95 px-5 py-3 shadow-sm backdrop-blur-sm">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => router.push(backPath)}
              className="gap-1.5 text-slate-500 hover:text-slate-900"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              Queue
            </Button>
            <Separator orientation="vertical" className="h-4" />
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500">Job</span>
              <code className="font-mono text-xs text-indigo-600">
                {truncateId(jobId, 8)}...
              </code>
              <span className="text-xs text-slate-300">|</span>
              <span className="text-xs text-slate-700">
                Page {pageNumber}
                {workspace.sub_page_index != null && ` / Page ${workspace.sub_page_index}`}
              </span>
              <Badge variant="warning" className="capitalize">
                {workspace.material_type}
              </Badge>
            </div>
          </div>

          <div className="flex items-center gap-1.5">
            {workspace.review_reasons.map((reason) => (
              <span
                key={reason}
                className="inline-flex items-center rounded border border-orange-200 bg-orange-50 px-2 py-1 text-2xs font-medium text-orange-700"
              >
                <AlertTriangle className="mr-1 h-2.5 w-2.5" />
                {reviewReasonLabel(reason)}
              </span>
            ))}
            <ArtifactLinkButton
              uri={activeUri}
              label="Open"
              variant="outline"
              size="xs"
              className="gap-1"
            />
            <ArtifactLinkButton
              uri={activeUri}
              label="Download"
              mode="download"
              variant="ghost"
              size="xs"
              className="gap-1"
            />
          </div>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <div className="flex w-52 shrink-0 flex-col overflow-y-auto border-r border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-200 p-3">
            <p className="text-2xs font-semibold uppercase tracking-wider text-slate-500">
              View Source
            </p>
          </div>

          <div className="flex flex-col gap-1 p-2">
            {workspace.parent_source_uri && (
              <SourceButton
                label="Original Parent"
                description="Parent scan"
                active={activeSource === "parent"}
                available={!!workspace.parent_source_uri}
                onClick={() => setActiveSource("parent")}
                icon={<Eye className="h-3.5 w-3.5" />}
              />
            )}
            <SourceButton
              label="Current"
              description={currentArtifactLabel(workspace.current_output_role)}
              active={activeSource === "current"}
              available={!!workspace.current_output_uri}
              onClick={() => setActiveSource("current")}
              icon={<Eye className="h-3.5 w-3.5" />}
            />
            {(!workspace.parent_source_uri ||
              workspace.original_otiff_uri !== workspace.parent_source_uri) && (
              <SourceButton
                label="Original OTIFF"
                description="Raw scan"
                active={activeSource === "original"}
                available={!!workspace.original_otiff_uri}
                onClick={() => setActiveSource("original")}
                icon={<Eye className="h-3.5 w-3.5" />}
              />
            )}
            <SourceButton
              label="Normalized"
              description="Normalized"
              active={activeSource === "normalized"}
              available={!!workspace.branch_outputs.iep1c_normalized}
              onClick={() => setActiveSource("normalized")}
              icon={<GitBranch className="h-3.5 w-3.5" />}
            />
            <SourceButton
              label="Rectified"
              description="Rectified"
              active={activeSource === "rectified"}
              available={!!workspace.branch_outputs.iep1d_rectified}
              onClick={() => setActiveSource("rectified")}
              icon={<GitBranch className="h-3.5 w-3.5" />}
            />
          </div>

          <div className="mt-auto space-y-2 border-t border-slate-200 p-3">
            <p className="mb-2 text-2xs font-semibold uppercase tracking-wider text-slate-500">
              Metadata
            </p>
            <MetaRow label="Pipeline" value={workspace.pipeline_mode} />
            <MetaRow
              label="Current Artifact"
              value={currentArtifactLabel(workspace.current_output_role)}
            />
            {workspace.branch_outputs.iep1a_geometry && (
              <MetaRow
                label="IEP1A split"
                value={workspace.branch_outputs.iep1a_geometry.split_required ? "Yes" : "No"}
              />
            )}
          </div>
        </div>

        <div className="min-w-0 flex-1 bg-slate-50/70 p-4">
          <div className="flex h-full min-h-0 flex-col gap-4">
            <ImageViewer
              imageUrl={viewerData?.blobUrl ?? null}
              isLoading={viewerLoading}
              isError={viewerIsError}
              emptyMessage={viewerEmptyMessage}
              errorMessage={viewerErrorMessage}
              quadPoints={canEditOnDisplayedSource ? displayedQuadPoints : null}
              deskewAngle={0}
              showCropOverlay={false}
              showQuadOverlay={canEditOnDisplayedSource}
              onQuadPointsChange={
                canEditOnDisplayedSource
                  ? (nextQuad) => {
                      const unscaled =
                        pageImageWidth != null &&
                        pageImageHeight != null &&
                        previewNaturalWidth != null &&
                        previewNaturalHeight != null &&
                        previewNaturalWidth > 0 &&
                        previewNaturalHeight > 0
                          ? scaleQuadPoints(
                              nextQuad,
                              pageImageWidth / previewNaturalWidth,
                              pageImageHeight / previewNaturalHeight
                            )
                          : nextQuad;
                      setQuadPoints(unscaled);
                    }
                  : undefined
              }
              splitX={isSpreadSelection && activeUri ? (splitX != null ? splitX * splitScale : null) : null}
              showSplitOverlay={isSpreadSelection && !!activeUri}
              onSplitXChange={isSpreadSelection && activeUri ? (x) => setSplitX(x / splitScale) : undefined}
              onNaturalSizeChange={(w, h) => {
                setPreviewNaturalWidth(w);
                setPreviewNaturalHeight(h);
              }}
            />
            {canEditGeometry && !activeUri && (
              <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-500">
                No displayable artifact is available for the selected source. Choose another source before submitting a correction.
              </div>
            )}
            {canEditGeometry && activeUri && activeSource !== "current" && (
              <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-500">
                Edits will be applied using the selected <strong>{sourceViewLabel(activeSource)}</strong> artifact and the result will become the new current artifact.
              </div>
            )}
            {workspace.current_output_uri && workspace.current_layout_uri && (
              <LayoutOverlay
                imageUri={workspace.current_output_uri}
                layoutUri={workspace.current_layout_uri}
                pageLabel={`Correction Page ${workspace.page_number}${
                  workspace.sub_page_index != null ? ` / ${workspace.sub_page_index}` : ""
                }`}
              />
            )}
          </div>
        </div>

        <div className="flex w-64 shrink-0 flex-col overflow-y-auto border-l border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-200 p-3">
            <p className="text-2xs font-semibold uppercase tracking-wider text-slate-500">
              Correction Controls
            </p>
          </div>

          <div className="flex-1 space-y-5 p-3">
            {canChoosePageStructure && (
              <>
                <div className="space-y-2">
                  <Label className="text-xs text-slate-600">Page Structure</Label>
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      type="button"
                      onClick={() => setPageStructure("single")}
                      className={cn(
                        "rounded-lg border px-3 py-2 text-left transition-colors",
                        pageStructure === "single"
                          ? "border-indigo-200 bg-indigo-50 text-indigo-700"
                          : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                      )}
                    >
                      <div className="text-xs font-semibold">Single page</div>
                      <div className="mt-1 text-2xs text-slate-500">
                        Review this artifact as one page.
                      </div>
                    </button>
                    <button
                      type="button"
                      onClick={() => setPageStructure("spread")}
                      className={cn(
                        "rounded-lg border px-3 py-2 text-left transition-colors",
                        pageStructure === "spread"
                          ? "border-indigo-200 bg-indigo-50 text-indigo-700"
                          : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                      )}
                    >
                      <div className="text-xs font-semibold">Two-page spread</div>
                      <div className="mt-1 text-2xs text-slate-500">
                        Create Page 0 and Page 1 child workspaces.
                      </div>
                    </button>
                  </div>
                  {workspace.branch_outputs.iep1a_geometry?.split_required ? (
                    <p className="flex items-center gap-1 text-2xs text-amber-600">
                      <AlertTriangle className="h-3 w-3" />
                      IEP1 suggests this artifact is a two-page spread.
                    </p>
                  ) : (
                    <p className="text-2xs text-slate-400">
                      Confirm the page structure before reviewing crop and deskew.
                    </p>
                  )}
                  {isSpreadSelection && (
                    <div className="flex items-start gap-1.5 rounded border border-cyan-200 bg-cyan-50 p-2 text-2xs text-cyan-700">
                      <GitBranch className="mt-0.5 h-3 w-3 shrink-0" />
                      <span>
                        Submitting this choice creates or reuses child pages, then opens
                        Page 0 and Page 1 for separate correction.
                      </span>
                    </div>
                  )}
                </div>

                <Separator />
              </>
            )}

            {hasChildPages && (
              <>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label className="text-xs text-slate-600">Child Pages</Label>
                    <span className="text-2xs text-slate-400">
                      Parent stays as lineage anchor
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    {workspace.child_pages.map((child) => (
                      <button
                        key={child.sub_page_index}
                        type="button"
                        onClick={() => router.push(workspacePathForSubPage(child.sub_page_index))}
                        className={cn(
                          "rounded-lg border px-3 py-2 text-left transition-colors",
                          workspace.sub_page_index === child.sub_page_index
                            ? "border-indigo-200 bg-indigo-50"
                            : "border-slate-200 bg-white hover:bg-slate-50"
                        )}
                      >
                        <div className="text-xs font-semibold text-slate-700">
                          Page {child.sub_page_index}
                        </div>
                        <div className="mt-1 text-2xs text-slate-500">
                          {snakeToTitle(child.status)}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>

                <Separator />
              </>
            )}

            {!canEditGeometry && (
              <>
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                  <p className="text-xs font-medium text-slate-700">
                    {isParentLineageAnchor ? "Parent already split" : "Spread structure confirmed"}
                  </p>
                  <p className="mt-1 text-2xs leading-relaxed text-slate-500">
                    {isParentLineageAnchor
                      ? "This parent is lineage-only now. Continue correction in Page 0 and Page 1."
                      : "Crop and deskew are applied on Page 0 and Page 1 separately after the child pages are created."}
                  </p>
                </div>

                <Separator />
              </>
            )}

            {canEditGeometry && (
              <>
                <div className="space-y-2">
                  <Label className="text-xs text-slate-600">
                    Page Quad{" "}
                    <span className="font-normal text-slate-400">
                      {isChildPage ? "[x, y] in parent image" : "[x, y] in image"}
                    </span>
                  </Label>
                  <div className="grid grid-cols-2 gap-2">
                    {(quadPoints ?? []).map((point, index) => (
                      <div key={`quad-${index}`} className="space-y-0.5">
                        <span className="text-2xs text-slate-500">
                          {["TL", "TR", "BR", "BL"][index]}
                        </span>
                        <div className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-2xs tabular-nums text-slate-600">
                          [{Math.round(point[0])}, {Math.round(point[1])}]
                        </div>
                      </div>
                    ))}
                  </div>
                  {!quadPoints && (
                    <p className="flex items-center gap-1 text-2xs text-slate-400">
                      <Info className="h-3 w-3" />
                      No geometry. Drag on the image to set it.
                    </p>
                  )}
                  <p className="flex items-start gap-1 text-2xs text-slate-400">
                    <Info className="mt-0.5 h-3 w-3 shrink-0" />
                    {isChildPage
                      ? "Drag each corner handle on the parent image, or drag to draw a new region. The child artifact is rectified from this quad when you submit."
                      : "Drag each corner handle to adjust, or drag anywhere to draw a new region. Perspective correction is applied on submit."}
                  </p>
                </div>

                <Separator />
              </>
            )}

            <div className="space-y-2">
              <Label className="text-xs text-slate-600">Reviewer Notes</Label>
              <Textarea
                value={reviewerNotes}
                onChange={(event) => setReviewerNotes(event.target.value)}
                placeholder="Optional notes for audit trail..."
                className="min-h-[80px] text-xs"
              />
            </div>

            <div className="space-y-1.5 rounded-lg border border-slate-200 bg-slate-50 p-3">
              <p className="mb-2 text-2xs font-semibold uppercase tracking-wider text-slate-500">
                Will Submit
              </p>
              <SubmitRow
                label="Source"
                value={activeUri ? sourceViewLabel(activeSource) : "Unavailable"}
              />
              <SubmitRow
                label="Structure"
                value={
                  isChildPage
                    ? `Page ${workspace.sub_page_index}`
                    : pageStructure === "spread"
                      ? "Two-page spread"
                      : "Single page"
                }
              />
              <SubmitRow
                label="Quad"
                value={
                  canEditGeometry
                    ? quadPoints
                      ? quadPoints
                          .map(([x, y], index) => `${["TL", "TR", "BR", "BL"][index]}(${Math.round(x)},${Math.round(y)})`)
                          .join(" ")
                      : "null"
                    : "child workflow"
                }
              />
              <SubmitRow
                label="Rectify"
                value={
                  canEditGeometry
                    ? quadPoints ? "perspective warp" : "none"
                    : "child workflow"
                }
              />
              {isSpreadSelection && (
                <SubmitRow
                  label="Split X"
                  value={splitX != null ? `${Math.round(splitX)}px` : "center (default)"}
                />
              )}
              {isSpreadSelection && (
                <div className="flex items-center gap-1 text-2xs text-cyan-600">
                  <GitBranch className="h-2.5 w-2.5 shrink-0" />
                  <span>Creates or reuses Page 0 and Page 1</span>
                </div>
              )}
            </div>
          </div>

          <div className="shrink-0 space-y-2 border-t border-slate-200 p-3">
            {isParentLineageAnchor ? (
              <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-2xs text-slate-500">
                Parent split is already committed. Open a child page above to continue editing.
              </div>
            ) : (
              <>
                <Button
                  className="w-full gap-2"
                  onClick={() => submitMut.mutate()}
                  loading={submitMut.isPending}
                  disabled={!canSubmitCorrection}
                >
                  <CheckCircle className="h-4 w-4" />
                  {isSpreadSelection ? "Create Child Pages" : "Submit Correction"}
                </Button>
                {!activeUri && (
                  <p className="text-2xs text-amber-600">
                    Select a displayable source artifact before submitting a correction.
                  </p>
                )}
                <Button
                  variant="danger"
                  className="w-full gap-2"
                  onClick={() => setShowRejectModal(true)}
                  disabled={submitMut.isPending || rejectMut.isPending}
                >
                  <XCircle className="h-4 w-4" />
                  Reject Page
                </Button>
              </>
            )}
          </div>
        </div>
      </div>

      <ConfirmModal
        open={showRejectModal}
        onOpenChange={setShowRejectModal}
        title="Reject Page?"
        description="This will route the page to the review state. This action cannot be undone from this screen."
        confirmLabel="Reject Page"
        variant="danger"
        loading={rejectMut.isPending}
        onConfirm={() => {
          setShowRejectModal(false);
          rejectMut.mutate();
        }}
      />
    </div>
  );
}

function currentArtifactLabel(role: CorrectionWorkspaceDetail["current_output_role"]): string {
  switch (role) {
    case "human_corrected":
      return "Human corrected";
    case "split_child":
      return "Split child";
    case "normalized_output":
      return "Normalized output";
    case "original_upload":
      return "Original upload";
    default:
      return "Unavailable";
  }
}

function sourceViewLabel(source: SourceView): string {
  switch (source) {
    case "parent":
      return "Original Parent";
    case "original":
      return "Original OTIFF";
    case "normalized":
      return "Normalized";
    case "rectified":
      return "Rectified";
    case "current":
    default:
      return "Current";
  }
}

function SourceButton({
  label,
  description,
  active,
  available,
  onClick,
  icon,
  badge,
}: {
  label: string;
  description: string;
  active: boolean;
  available: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  badge?: "success" | "warning";
}) {
  return (
    <button
      onClick={available ? onClick : undefined}
      disabled={!available}
      className={cn(
        "flex w-full items-start gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors duration-100",
        active
          ? "border-indigo-200 bg-indigo-50"
          : "border-transparent hover:bg-slate-100",
        !available && "cursor-not-allowed opacity-40"
      )}
    >
      <span
        className={cn(
          "mt-0.5 shrink-0",
          active ? "text-indigo-600" : "text-slate-400"
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span
            className={cn(
              "text-xs font-medium",
              active ? "text-indigo-700" : "text-slate-700"
            )}
          >
            {label}
          </span>
          {badge && (
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                badge === "success" ? "bg-emerald-500" : "bg-amber-500"
              )}
            />
          )}
        </div>
        <p className="truncate text-2xs text-slate-500">{description}</p>
      </div>
    </button>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-2xs text-slate-500">{label}</span>
      <span className="text-2xs font-medium text-slate-700">{value}</span>
    </div>
  );
}

function SubmitRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-2xs text-slate-500">{label}</span>
      <code className="max-w-[120px] truncate font-mono text-2xs text-slate-700">
        {value}
      </code>
    </div>
  );
}
