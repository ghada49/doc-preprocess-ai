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
      return "Original parent source unavailable for this child page.";
    case "current":
      return workspace.sub_page_index != null
        ? "Current child review source unavailable. This child page does not have a displayable image yet."
        : "Current artifact unavailable. This page does not have a displayable image yet.";
    case "normalized":
      return "Normalized artifact unavailable for this page.";
    case "rectified":
      return "Rectified artifact unavailable for this page.";
    case "original":
      return "Original upload unavailable for this page.";
    default:
      return "No image selected.";
  }
}

export function getWorkspacePreviewErrorMessage(
  workspace: CorrectionWorkspaceDetail | undefined,
  source: SourceView
): string {
  if (!workspace) return "Failed to load the selected artifact preview.";

  switch (source) {
    case "parent":
      return "Failed to load the original parent source preview.";
    case "current":
      return workspace.sub_page_index != null
        ? "Failed to load the current child review source preview."
        : "Failed to load the current artifact preview.";
    case "normalized":
      return "Failed to load the normalized artifact preview.";
    case "rectified":
      return "Failed to load the rectified artifact preview.";
    case "original":
      return "Failed to load the original upload preview.";
    default:
      return "Failed to load the selected artifact preview.";
  }
}
