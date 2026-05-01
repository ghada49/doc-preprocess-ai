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
import { pageStateLabel, reviewReasonLabel, truncateId } from "@/lib/utils";
import { cn } from "@/lib/utils";
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
  getWorkspaceFallbackSource,
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
  // previewNaturalWidth: width of the preview PNG currently displayed.
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
    { maxWidth: 1600 },
    {
      scopeKey: [jobId, pageNumber, subPageIndex ?? "root", activeSource],
      staleTimeMs: 5 * 60 * 1000,
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
      toast.success("Review saved.");
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
        toast.error("This page no longer needs review.");
      } else if (status === 422) {
        toast.error("We could not save this review. Check the page outline and try again.");
      } else {
        toast.error("We could not save this review. Please try again.");
      }
    },
  });

  const rejectMut = useMutation({
    mutationFn: () =>
      rejectPage(jobId, pageNumber, {
        subPageIndex,
        notes: reviewerNotes,
      }),
    onSuccess: () => {
      toast.success("Page marked as an issue.");
      queryClient.invalidateQueries({ queryKey: ["correction-queue"] });
      router.push(backPath);
    },
    onError: () => {
      toast.error("We could not mark this page as an issue.");
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
            ? "This page no longer needs review."
            : status === 404
              ? "This page was not found in the review list."
              : "There was a problem loading this page for review."
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
  const fallbackSource = getWorkspaceFallbackSource(workspace, activeSource);

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
              {isAdmin ? "Queue" : "Needs review"}
            </Button>
            <Separator orientation="vertical" className="h-4" />
            <div className="flex items-center gap-2">
              {isAdmin && (
                <>
                  <span className="text-xs text-slate-500">Job</span>
                  <code className="font-mono text-xs text-indigo-600">
                    {truncateId(jobId, 8)}...
                  </code>
                  <span className="text-xs text-slate-300">|</span>
                </>
              )}
              <span className="text-xs text-slate-700">
                Page {pageNumber}
                {workspace.sub_page_index != null &&
                  ` / ${workspace.sub_page_index === 0 ? "Left page" : "Right page"}`}
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
                label={isAdmin ? "Original Parent" : "Original page"}
                description={isAdmin ? "Parent scan" : "Full scan"}
                active={activeSource === "parent"}
                available={!!workspace.parent_source_uri}
                onClick={() => setActiveSource("parent")}
                icon={<Eye className="h-3.5 w-3.5" />}
              />
            )}
            <SourceButton
              label={isAdmin ? "Current" : "Page preview"}
              description={currentArtifactLabel(workspace.current_output_role)}
              active={activeSource === "current"}
              available={!!workspace.current_output_uri}
              onClick={() => setActiveSource("current")}
              icon={<Eye className="h-3.5 w-3.5" />}
            />
            {(!workspace.parent_source_uri ||
              workspace.original_otiff_uri !== workspace.parent_source_uri) && (
              <SourceButton
                label={isAdmin ? "Original OTIFF" : "Original scan"}
                description={isAdmin ? "Raw scan" : "Uploaded file"}
                active={activeSource === "original"}
                available={!!workspace.original_otiff_uri}
                onClick={() => setActiveSource("original")}
                icon={<Eye className="h-3.5 w-3.5" />}
              />
            )}
            <SourceButton
              label={isAdmin ? "Normalized" : "Cleaned page"}
              description={isAdmin ? "Normalized" : "Cleaned version"}
              active={activeSource === "normalized"}
              available={!!workspace.branch_outputs.iep1c_normalized}
              onClick={() => setActiveSource("normalized")}
              icon={<GitBranch className="h-3.5 w-3.5" />}
            />
            <SourceButton
              label={isAdmin ? "Rectified" : "Enhanced page"}
              description={isAdmin ? "Rectified" : "Enhanced version"}
              active={activeSource === "rectified"}
              available={!!workspace.branch_outputs.iep1d_rectified}
              onClick={() => setActiveSource("rectified")}
              icon={<GitBranch className="h-3.5 w-3.5" />}
            />
          </div>

          {isAdmin && (
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
          )}
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
                No image is available for this view. Choose another view before saving.
              </div>
            )}
            {canEditGeometry && activeUri && activeSource !== "current" && (
              <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-500">
                Changes will be saved using the selected <strong>{sourceViewLabel(activeSource)}</strong>.
              </div>
            )}
            {viewerIsError && fallbackSource && (
              <div className="flex items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                <span>
                  We could not show <strong>{sourceViewLabel(activeSource).toLowerCase()}</strong>.
                  {" "}Try <strong>{sourceViewLabel(fallbackSource).toLowerCase()}</strong> instead.
                </span>
                <Button
                  size="xs"
                  variant="secondary"
                  onClick={() => setActiveSource(fallbackSource)}
                  className="shrink-0"
                >
                  Show {sourceViewLabel(fallbackSource)}
                </Button>
              </div>
            )}
            {isAdmin && workspace.current_output_uri && workspace.current_layout_uri && (
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
              {isAdmin ? "Correction Controls" : "Review tools"}
            </p>
          </div>

          <div className="flex-1 space-y-5 p-3">
            {canChoosePageStructure && (
              <>
                <div className="space-y-2">
                  <Label className="text-xs text-slate-600">{isAdmin ? "Page Structure" : "Page type"}</Label>
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
                        Review this scan as one page.
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
                        Split into left and right pages.
                      </div>
                    </button>
                  </div>
                  {workspace.branch_outputs.iep1a_geometry?.split_required ? (
                    <p className="flex items-center gap-1 text-2xs text-amber-600">
                      <AlertTriangle className="h-3 w-3" />
                      This scan may contain two pages.
                    </p>
                  ) : (
                    <p className="text-2xs text-slate-400">
                      Confirm whether this scan has one page or two.
                    </p>
                  )}
                  {isSpreadSelection && (
                    <div className="flex items-start gap-1.5 rounded border border-cyan-200 bg-cyan-50 p-2 text-2xs text-cyan-700">
                      <GitBranch className="mt-0.5 h-3 w-3 shrink-0" />
                      <span>
                        Saving this choice creates left and right pages for separate review.
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
                    <Label className="text-xs text-slate-600">
                      {isAdmin ? "Child Pages" : "Split pages"}
                    </Label>
                    {isAdmin && (
                      <span className="text-2xs text-slate-400">
                        Parent stays as lineage anchor
                      </span>
                    )}
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
                          {isAdmin
                            ? `Page ${child.sub_page_index}`
                            : child.sub_page_index === 0
                              ? "Left page"
                              : "Right page"}
                        </div>
                        <div className="mt-1 text-2xs text-slate-500">
                          {pageStateLabel(child.status)}
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
                    {isParentLineageAnchor
                      ? isAdmin ? "Parent already split" : "Split pages created"
                      : "Two-page scan confirmed"}
                  </p>
                  <p className="mt-1 text-2xs leading-relaxed text-slate-500">
                    {isParentLineageAnchor
                      ? isAdmin
                        ? "This parent is lineage-only now. Continue correction in Page 0 and Page 1."
                        : "Continue review in the left and right pages above."
                      : "Left and right pages can be reviewed separately after they are created."}
                  </p>
                </div>

                <Separator />
              </>
            )}

            {canEditGeometry && (
              <>
                <div className="space-y-2">
                  <Label className="text-xs text-slate-600">
                    {isAdmin ? "Page Quad" : "Page outline"}{" "}
                    {isAdmin && (
                      <span className="font-normal text-slate-400">
                        {isChildPage ? "[x, y] in parent image" : "[x, y] in image"}
                      </span>
                    )}
                  </Label>
                  {isAdmin && (
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
                  )}
                  {!quadPoints && (
                    <p className="flex items-center gap-1 text-2xs text-slate-400">
                      <Info className="h-3 w-3" />
                      No outline selected. Drag on the image to set one.
                    </p>
                  )}
                  <p className="flex items-start gap-1 text-2xs text-slate-400">
                    <Info className="mt-0.5 h-3 w-3 shrink-0" />
                    {isChildPage
                      ? "Drag each corner handle on the original page, or drag to draw a new outline."
                      : "Drag each corner handle to adjust the page outline, or drag anywhere to draw a new one."}
                  </p>
                </div>

                <Separator />
              </>
            )}

            <div className="space-y-2">
              <Label className="text-xs text-slate-600">{isAdmin ? "Reviewer Notes" : "Notes"}</Label>
              <Textarea
                value={reviewerNotes}
                onChange={(event) => setReviewerNotes(event.target.value)}
                placeholder="Optional notes..."
                className="min-h-[80px] text-xs"
              />
            </div>

            {isAdmin ? (
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
            ) : (
              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-2xs leading-relaxed text-slate-500">
                Your review will be saved and this page will continue processing.
              </div>
            )}
          </div>

          <div className="shrink-0 space-y-2 border-t border-slate-200 p-3">
            {isParentLineageAnchor ? (
              <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-2xs text-slate-500">
                {isAdmin
                  ? "Parent split is already committed. Open a child page above to continue editing."
                  : "Split pages are ready. Open the left or right page above to continue."}
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
                  {isSpreadSelection
                    ? isAdmin ? "Create Child Pages" : "Create split pages"
                    : isAdmin ? "Submit Correction" : "Save review"}
                </Button>
                {!activeUri && (
                  <p className="text-2xs text-amber-600">
                    Choose a view with an image before saving.
                  </p>
                )}
                <Button
                  variant="danger"
                  className="w-full gap-2"
                  onClick={() => setShowRejectModal(true)}
                  disabled={submitMut.isPending || rejectMut.isPending}
                >
                  <XCircle className="h-4 w-4" />
                  {isAdmin ? "Reject Page" : "Mark as issue"}
                </Button>
              </>
            )}
          </div>
        </div>
      </div>

      <ConfirmModal
        open={showRejectModal}
        onOpenChange={setShowRejectModal}
        title={isAdmin ? "Reject Page?" : "Mark this page as an issue?"}
        description={
          isAdmin
            ? "This will route the page to the review state. This action cannot be undone from this screen."
            : "This marks the page for follow-up review. You can add notes before confirming."
        }
        confirmLabel={isAdmin ? "Reject Page" : "Mark as issue"}
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
      return "Reviewed image";
    case "split_child":
      return "Split page";
    case "normalized_output":
      return "Cleaned page";
    case "original_upload":
      return "Original scan";
    default:
      return "Unavailable";
  }
}

function sourceViewLabel(source: SourceView): string {
  switch (source) {
    case "parent":
      return "Original page";
    case "original":
      return "Original scan";
    case "normalized":
      return "Cleaned page";
    case "rectified":
      return "Enhanced page";
    case "current":
    default:
      return "Page preview";
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
