// @ts-nocheck
import assert from "node:assert/strict";
import {
  artifactPreviewQueryKey,
  artifactReadQueryKey,
} from "./artifact-query-key.ts";

assert.deepEqual(
  artifactReadQueryKey("s3://bucket/page-1.tiff", 120),
  ["artifact-read", "s3://bucket/page-1.tiff", 120]
);

assert.deepEqual(
  artifactPreviewQueryKey(
    "s3://bucket/page-1.tiff",
    { maxWidth: 2400 },
    ["job-1", 1, 0, "current"]
  ),
  [
    "artifact-preview",
    "s3://bucket/page-1.tiff",
    JSON.stringify({ maxWidth: 2400 }),
    JSON.stringify(["job-1", 1, 0, "current"]),
  ]
);

assert.notDeepEqual(
  artifactPreviewQueryKey(
    "s3://bucket/shared-parent.tiff",
    { maxWidth: 2400 },
    ["job-1", 1, 0, "current"]
  ),
  artifactPreviewQueryKey(
    "s3://bucket/shared-parent.tiff",
    { maxWidth: 2400 },
    ["job-1", 1, 1, "current"]
  )
);

assert.notDeepEqual(
  artifactPreviewQueryKey(
    "s3://bucket/shared-parent.tiff",
    { maxWidth: 2400 },
    ["job-1", 1, 1, "current"]
  ),
  artifactPreviewQueryKey(
    "s3://bucket/shared-parent.tiff",
    { maxWidth: 2400 },
    ["job-1", 1, 1, "original"]
  )
);
