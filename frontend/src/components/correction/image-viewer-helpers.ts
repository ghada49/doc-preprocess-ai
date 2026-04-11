export function computeFitZoom(
  containerWidth: number,
  containerHeight: number,
  imageWidth: number,
  imageHeight: number
): number {
  if (
    containerWidth <= 0 ||
    containerHeight <= 0 ||
    imageWidth <= 0 ||
    imageHeight <= 0
  ) {
    return 1;
  }

  const scaleX = containerWidth / imageWidth;
  const scaleY = containerHeight / imageHeight;
  return Math.min(scaleX, scaleY, 1) * 0.9;
}

export type QuadPoint = [number, number];

export function cropBoxToQuadPoints(
  cropBox: [number, number, number, number] | null
): QuadPoint[] | null {
  if (!cropBox) return null;
  const [x1, y1, x2, y2] = cropBox;
  return [
    [x1, y1],
    [x2, y1],
    [x2, y2],
    [x1, y2],
  ];
}

export function quadPointsToCropBox(
  quadPoints: QuadPoint[] | null
): [number, number, number, number] | null {
  if (!quadPoints || quadPoints.length !== 4) return null;
  const xs = quadPoints.map(([x]) => x);
  const ys = quadPoints.map(([, y]) => y);
  return [
    Math.floor(Math.min(...xs)),
    Math.floor(Math.min(...ys)),
    Math.ceil(Math.max(...xs)),
    Math.ceil(Math.max(...ys)),
  ];
}

export function scaleQuadPoints(
  quadPoints: QuadPoint[] | null,
  scaleX: number,
  scaleY: number
): QuadPoint[] | null {
  if (!quadPoints) return null;
  if (!Number.isFinite(scaleX) || !Number.isFinite(scaleY) || scaleX <= 0 || scaleY <= 0) {
    return quadPoints.map(([x, y]) => [x, y]);
  }
  return quadPoints.map(([x, y]) => [x * scaleX, y * scaleY]);
}

export function updateQuadPoint(
  quadPoints: QuadPoint[],
  index: number,
  nextPoint: QuadPoint
): QuadPoint[] {
  return quadPoints.map((point, pointIndex) =>
    pointIndex === index ? [nextPoint[0], nextPoint[1]] : [point[0], point[1]]
  ) as QuadPoint[];
}
