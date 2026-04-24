// @ts-nocheck
import assert from "node:assert/strict";
import type { CorrectionWorkspaceDetail } from "../../types/api";
import {
  getDefaultWorkspaceSource,
  getWorkspaceEmptyMessage,
  getWorkspaceInteractionState,
  resolveWorkspaceSourceUri,
} from "./workspace-source.ts";

function makeWorkspace(
  overrides: Partial<CorrectionWorkspaceDetail> = {}
): CorrectionWorkspaceDetail {
  return {
    job_id: "job-001",
    page_number: 1,
    sub_page_index: null,
    material_type: "book",
    pipeline_mode: "layout",
    review_reasons: [],
    original_otiff_uri: "s3://bucket/raw/page-1.tiff",
    parent_source_uri: null,
    current_output_uri: "s3://bucket/jobs/job-001/output/1.tiff",
    current_output_role: "normalized_output",
    current_layout_uri: null,
    best_output_uri: "s3://bucket/jobs/job-001/output/1.tiff",
    branch_outputs: {
      iep1a_geometry: null,
      iep1b_geometry: null,
      iep1c_normalized: "s3://bucket/jobs/job-001/output/1.tiff",
      iep1d_rectified: null,
    },
    suggested_page_structure: "single",
    child_pages: [],
    current_selection_mode: "rect",
    current_quad_points: null,
    current_crop_box: null,
    current_deskew_angle: null,
    current_split_x: null,
    page_image_width: 2400,
    page_image_height: 3200,
    ...overrides,
  };
}

{
  const workspace = makeWorkspace({
    sub_page_index: 0,
    current_output_uri: "s3://bucket/norm/page-1.tiff",
    current_output_role: "split_child",
    best_output_uri: "s3://bucket/norm/page-1.tiff",
    branch_outputs: {
      iep1a_geometry: null,
      iep1b_geometry: null,
      iep1c_normalized: null,
      iep1d_rectified: null,
    },
    current_selection_mode: "quad",
    current_quad_points: [
      [0, 0],
      [1200, 0],
      [1200, 3200],
      [0, 3200],
    ],
  });

  assert.equal(getDefaultWorkspaceSource(workspace, "original"), "original");
  assert.equal(getDefaultWorkspaceSource(workspace, "current"), "current");
  assert.equal(
    resolveWorkspaceSourceUri(workspace, "current"),
    "s3://bucket/norm/page-1.tiff"
  );
}

{
  const leftChild = makeWorkspace({
    sub_page_index: 0,
    current_output_uri: "s3://bucket/norm/page-1.tiff",
    current_output_role: "split_child",
    best_output_uri: "s3://bucket/norm/page-1.tiff",
    current_selection_mode: "quad",
    current_quad_points: [
      [0, 0],
      [1200, 0],
      [1200, 3200],
      [0, 3200],
    ],
  });
  const rightChild = makeWorkspace({
    sub_page_index: 1,
    current_output_uri: "s3://bucket/norm/page-1.tiff",
    current_output_role: "split_child",
    best_output_uri: "s3://bucket/norm/page-1.tiff",
    current_selection_mode: "quad",
    current_quad_points: [
      [1200, 0],
      [2400, 0],
      [2400, 3200],
      [1200, 3200],
    ],
  });

  assert.equal(
    resolveWorkspaceSourceUri(leftChild, "current"),
    resolveWorkspaceSourceUri(rightChild, "current")
  );
  assert.deepEqual(leftChild.current_quad_points, [
    [0, 0],
    [1200, 0],
    [1200, 3200],
    [0, 3200],
  ]);
  assert.deepEqual(rightChild.current_quad_points, [
    [1200, 0],
    [2400, 0],
    [2400, 3200],
    [1200, 3200],
  ]);
}

{
  const workspace = makeWorkspace({
    sub_page_index: 1,
    parent_source_uri: "s3://bucket/raw/parent-page-1.tiff",
    original_otiff_uri: "s3://bucket/raw/parent-page-1.tiff",
    current_output_uri: "s3://bucket/jobs/job-001/output/1_1.tiff",
    current_output_role: "split_child",
  });

  assert.equal(getDefaultWorkspaceSource(workspace, "current"), "parent");
  assert.equal(
    resolveWorkspaceSourceUri(workspace, "parent"),
    "s3://bucket/raw/parent-page-1.tiff"
  );
}

{
  const workspace = makeWorkspace({
    original_otiff_uri: "s3://bucket/raw/page-1.tiff",
    current_output_uri: "s3://bucket/jobs/job-001/output/1.tiff",
    branch_outputs: {
      iep1a_geometry: null,
      iep1b_geometry: null,
      iep1c_normalized: "s3://bucket/jobs/job-001/output/1.tiff",
      iep1d_rectified: "s3://bucket/jobs/job-001/rectified/1.tiff",
    },
  });

  assert.equal(
    resolveWorkspaceSourceUri(workspace, "original"),
    "s3://bucket/raw/page-1.tiff"
  );
  assert.equal(
    resolveWorkspaceSourceUri(workspace, "rectified"),
    "s3://bucket/jobs/job-001/rectified/1.tiff"
  );
}

{
  const workspace = makeWorkspace({
    sub_page_index: 1,
    current_output_uri: null,
    current_output_role: null,
    best_output_uri: null,
    branch_outputs: {
      iep1a_geometry: null,
      iep1b_geometry: null,
      iep1c_normalized: null,
      iep1d_rectified: null,
    },
  });

  assert.equal(getDefaultWorkspaceSource(workspace, "original"), "original");
  assert.equal(resolveWorkspaceSourceUri(workspace, "current"), null);
  assert.match(
    getWorkspaceEmptyMessage(workspace, "current"),
    /Current child review source unavailable/
  );
}

{
  const workspace = makeWorkspace({
    child_pages: [
      { page_number: 1, sub_page_index: 0, status: "pending_human_correction" },
      { page_number: 1, sub_page_index: 1, status: "pending_human_correction" },
    ],
  });

  const state = getWorkspaceInteractionState(
    workspace,
    "spread",
    "current",
    workspace.current_output_uri
  );

  assert.equal(state.isParentLineageAnchor, true);
  assert.equal(state.canChoosePageStructure, false);
  assert.equal(state.isSpreadSelection, false);
  assert.equal(state.canEditGeometry, false);
  assert.equal(state.canSubmitCorrection, false);
}

{
  const childWorkspace = makeWorkspace({
    sub_page_index: 0,
    child_pages: [
      { page_number: 1, sub_page_index: 0, status: "pending_human_correction" },
      { page_number: 1, sub_page_index: 1, status: "pending_human_correction" },
    ],
    current_output_uri: "s3://bucket/parent-source.tiff",
  });

  const currentState = getWorkspaceInteractionState(
    childWorkspace,
    "single",
    "current",
    childWorkspace.current_output_uri
  );
  const originalState = getWorkspaceInteractionState(
    childWorkspace,
    "single",
    "original",
    childWorkspace.current_output_uri
  );

  assert.equal(currentState.canEditOnDisplayedSource, true);
  assert.equal(currentState.canSubmitCorrection, true);
  assert.equal(originalState.canEditOnDisplayedSource, true);
  assert.equal(originalState.canSubmitCorrection, true);
}
