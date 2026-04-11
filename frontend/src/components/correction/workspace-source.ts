import type { CorrectionWorkspaceDetail } from "../../types/api";

export type SourceView = "original" | "current" | "normalized" | "rectified";

export function resolveWorkspaceSourceUri(
  workspace: CorrectionWorkspaceDetail | undefined,
  source: SourceView
): string | null {
  if (!workspace) return null;

  switch (source) {
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

  // Child workspaces must open on their shared current review source, which is
  // the parent artifact recorded in child correction state until submit time.
  if (workspace.sub_page_index != null) {
    return "current";
  }

  if (resolveWorkspaceSourceUri(workspace, previousSource)) {
    return previousSource;
  }
  if (workspace.current_output_uri) return "current";
  if (workspace.branch_outputs.iep1c_normalized) return "normalized";
  if (workspace.branch_outputs.iep1d_rectified) return "rectified";
  if (workspace.original_otiff_uri) return "original";
  return "current";
}

export function getWorkspaceEmptyMessage(
  workspace: CorrectionWorkspaceDetail | undefined,
  source: SourceView
): string {
  if (!workspace) return "No image selected.";

  switch (source) {
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
