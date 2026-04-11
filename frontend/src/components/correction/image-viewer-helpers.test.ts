// @ts-nocheck
import assert from "node:assert/strict";
import {
  computeFitZoom,
  cropBoxToQuadPoints,
  quadPointsToCropBox,
  scaleQuadPoints,
  updateQuadPoint,
} from "./image-viewer-helpers.ts";

assert.equal(computeFitZoom(0, 600, 2400, 3200), 1);
assert.equal(computeFitZoom(800, 600, 400, 300), 0.9);

const largePortrait = computeFitZoom(800, 600, 2400, 3200);
assert.ok(largePortrait > 0);
assert.ok(largePortrait < 1);
assert.equal(largePortrait, 0.16875);

const wideImage = computeFitZoom(1200, 700, 3000, 1000);
assert.ok(Math.abs(wideImage - 0.36) < 1e-9);

assert.deepEqual(cropBoxToQuadPoints([0, 0, 1200, 3200]), [
  [0, 0],
  [1200, 0],
  [1200, 3200],
  [0, 3200],
]);

assert.deepEqual(
  quadPointsToCropBox([
    [5, 10],
    [1210, 0],
    [1200, 3205],
    [0, 3195],
  ]),
  [0, 0, 1210, 3205]
);

assert.deepEqual(
  scaleQuadPoints(
    [
      [0, 0],
      [1200, 0],
      [1200, 3200],
      [0, 3200],
    ],
    0.5,
    0.25
  ),
  [
    [0, 0],
    [600, 0],
    [600, 800],
    [0, 800],
  ]
);

assert.deepEqual(
  updateQuadPoint(
    [
      [0, 0],
      [1200, 0],
      [1200, 3200],
      [0, 3200],
    ],
    2,
    [1180, 3190]
  ),
  [
    [0, 0],
    [1200, 0],
    [1180, 3190],
    [0, 3200],
  ]
);
