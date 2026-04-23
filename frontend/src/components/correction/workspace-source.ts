import type { CorrectionWorkspaceDetail } from "../../types/api";

export type SourceView = "parent" | "original" | "current" | "normalized" | "rectified";

export interface WorkspaceInteractionState {
  isChildPage: boolean;
  hasChildPages: boolean;
  isParentLineageAnchor: boolean;
  canChoosePageStructure: boolean;
  isSpreadSelection: boolean;
  canEditGeometry: boolean;
  canEditOnDisplayedSource: boolean;
  canSubmitCorrection: boolean;
}

export function resolveWorkspaceSourceUri(
  workspace: CorrectionWorkspaceDetail | undefined,
  source: SourceView
): string | null {
  if (!workspace) return null;

  switch (source) {
    case "parent":
      return workspace.parent_source_uri;
    case "original":
      return workspace.original_otiff_uri;
    case "current":
      return workspace.current_output_uri;
    case "normalized":
      return workspace.branch_outputs.iep1c_normalized;
    case "rectified":
      return workspace.branch_outputs.iep1d_rectified;
    default:
      return workspace.current_output_uri ?? workspace.best_output_uri;
  }
}

export function getDefaultWorkspaceSource(
  workspace: CorrectionWorkspaceDetail | undefined,
  previousSource: SourceView
): SourceView {
  if (!workspace) return "current";

  if (workspace.sub_page_index != null && workspace.parent_source_uri) {
    if (previousSource === "current" || !resolveWorkspaceSourceUri(workspace, previousSource)) {
      return "parent";
    }
  }
  if (resolveWorkspaceSourceUri(workspace, previousSource)) {
    return previousSource;
  }
  if (workspace.sub_page_index != null && workspace.parent_source_uri) return "parent";
  if (workspace.current_output_uri) return "current";
  if (workspace.branch_outputs.iep1c_normalized) return "normalized";
  if (workspace.branch_outputs.iep1d_rectified) return "rectified";
  if (workspace.original_otiff_uri) return "original";
  return "current";
}

export function getWorkspaceFallbackSource(
  workspace: CorrectionWorkspaceDetail | undefined,
  currentSource: SourceView
): SourceView | null {
  if (!workspace) return null;

  const candidates: SourceView[] =
    workspace.sub_page_index != null
      ? ["parent", "current", "normalized", "rectified", "original"]
      : ["current", "normalized", "rectified", "original", "parent"];

  for (const candidate of candidates) {
    if (candidate === currentSource) continue;
    if (resolveWorkspaceSourceUri(workspace, candidate)) {
      return candidate;
    }
  }

  return null;
}

export function getWorkspaceInteractionState(
  workspace: CorrectionWorkspaceDetail | undefined,
  pageStructure: "single" | "spread",
  activeSource: SourceView,
  activeUri: string | null
): WorkspaceInteractionState {
  const isChildPage = workspace?.sub_page_index != null;
  const hasChildPages = (workspace?.child_pages.length ?? 0) > 0;
  const isParentLineageAnchor = !isChildPage && hasChildPages;
  const canChoosePageStructure = !isChildPage && !hasChildPages;
  const isSpreadSelection = canChoosePageStructure && pageStructure === "spread";
  const canEditGeometry = isChildPage || (!hasChildPages && pageStructure === "single");
  const canEditOnDisplayedSource = canEditGeometry && !!activeUri;
  const canSubmitCorrection = !!activeUri && !isParentLineageAnchor;

  return {
    isChildPage,
    hasChildPages,
    isParentLineageAnchor,
    canChoosePageStructure,
    isSpreadSelection,
    canEditGeometry,
    canEditOnDisplayedSource,
    canSubmitCorrection,
  };
}

export function getWorkspaceEmptyMessage(
  workspace: CorrectionWorkspaceDetail | undefined,
  source: SourceView
): string {
  if (!workspace) return "No image selected.";

  switch (source) {
    case "parent":
      return "The original page is unavailable for this split page.";
    case "current":
      return workspace.sub_page_index != null
        ? "This split page does not have a displayable image yet."
        : "This page does not have a displayable image yet.";
    case "normalized":
      return "The cleaned page image is unavailable.";
    case "rectified":
      return "The enhanced page image is unavailable.";
    case "original":
      return "The original scan is unavailable.";
    default:
      return "No image selected.";
  }
}

export function getWorkspacePreviewErrorMessage(
  workspace: CorrectionWorkspaceDetail | undefined,
  source: SourceView
): string {
  if (!workspace) return "We could not load the selected image.";

  switch (source) {
    case "parent":
      return "We could not load the original page preview.";
    case "current":
      return workspace.sub_page_index != null
        ? "We could not load this split page preview."
        : "We could not load this page preview.";
    case "normalized":
      return "We could not load the cleaned page preview.";
    case "rectified":
      return "We could not load the enhanced page preview.";
    case "original":
      return "We could not load the original scan preview.";
    default:
      return "We could not load the selected image.";
  }
}
