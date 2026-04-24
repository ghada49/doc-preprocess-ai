"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { ZoomIn, ZoomOut, Maximize2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import {
  computeFitZoom,
  type QuadPoint,
  updateQuadPoint,
} from "./image-viewer-helpers";

interface CropBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

// Convert screen coordinates to natural image coordinates, accounting for zoom and deskew rotation.
// getBoundingClientRect() on a rotated element returns its axis-aligned bounding box,
// so we must un-rotate from the image center to get accurate image-space coords.
function screenToImage(
  clientX: number,
  clientY: number,
  imgRef: React.RefObject<HTMLImageElement | null>,
  zoom: number,
  deskewAngle: number,
  naturalSize: { w: number; h: number }
): { imgX: number; imgY: number } {
  if (!imgRef.current) return { imgX: 0, imgY: 0 };
  const r = imgRef.current.getBoundingClientRect();
  const cx = (r.left + r.right) / 2;
  const cy = (r.top + r.bottom) / 2;
  const relX = clientX - cx;
  const relY = clientY - cy;
  const rad = -(deskewAngle * Math.PI) / 180;
  return {
    imgX: Math.max(0, Math.min((relX * Math.cos(rad) - relY * Math.sin(rad)) / zoom + naturalSize.w / 2, naturalSize.w)),
    imgY: Math.max(0, Math.min((relX * Math.sin(rad) + relY * Math.cos(rad)) / zoom + naturalSize.h / 2, naturalSize.h)),
  };
}

// Convert screen-space drag delta to image-space delta (un-rotate + un-scale).
function screenDeltaToImage(
  dxScreen: number,
  dyScreen: number,
  zoom: number,
  deskewAngle: number
): { dx: number; dy: number } {
  const rad = -(deskewAngle * Math.PI) / 180;
  return {
    dx: (dxScreen * Math.cos(rad) - dyScreen * Math.sin(rad)) / zoom,
    dy: (dxScreen * Math.sin(rad) + dyScreen * Math.cos(rad)) / zoom,
  };
}

interface ImageViewerProps {
  imageUrl: string | null;
  cropBox?: CropBox | null;
  quadPoints?: QuadPoint[] | null;
  splitX?: number | null;
  deskewAngle?: number;
  onCropBoxChange?: (box: CropBox) => void;
  onQuadPointsChange?: (points: QuadPoint[]) => void;
  /** Called when the user drags the rotation handle on the crop overlay. */
  onCropAngleChange?: (angle: number) => void;
  onSplitXChange?: (x: number) => void;
  /** Called once when the image loads; reports natural (preview PNG) dimensions. */
  onNaturalSizeChange?: (w: number, h: number) => void;
  showCropOverlay?: boolean;
  showQuadOverlay?: boolean;
  showSplitOverlay?: boolean;
  isLoading?: boolean;
  isError?: boolean;
  emptyMessage?: string;
  errorMessage?: string;
}

export function ImageViewer({
  imageUrl,
  cropBox,
  quadPoints,
  splitX,
  deskewAngle = 0,
  onCropBoxChange,
  onQuadPointsChange,
  onCropAngleChange,
  onSplitXChange,
  onNaturalSizeChange,
  showCropOverlay = true,
  showQuadOverlay = false,
  showSplitOverlay = false,
  isLoading = false,
  isError = false,
  emptyMessage = "No image selected.",
  errorMessage = "Failed to load image preview.",
}: ImageViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const [panStart, setPanStart] = useState({ x: 0, y: 0 });
  const [imgLoaded, setImgLoaded] = useState(false);
  const [imgError, setImgError] = useState(false);
  const [naturalSize, setNaturalSize] = useState({ w: 0, h: 0 });

  // Active drag state for crop handles
  const [activeHandle, setActiveHandle] = useState<string | null>(null);
  const [dragStartPos, setDragStartPos] = useState({ x: 0, y: 0 });
  const [dragStartCrop, setDragStartCrop] = useState<CropBox | null>(null);

  // Draw-new-crop-box drag state
  const [isDrawing, setIsDrawing] = useState(false);
  const [drawStart, setDrawStart] = useState({ x: 0, y: 0 });

  // Draw-new-quad drag state
  const [isDrawingQuad, setIsDrawingQuad] = useState(false);
  const [quadDrawStart, setQuadDrawStart] = useState({ x: 0, y: 0 });

  // Active drag for split line
  const [isDraggingSplit, setIsDraggingSplit] = useState(false);
  const [activeQuadHandle, setActiveQuadHandle] = useState<number | null>(null);

  // Rotation drag state (for the rotation handle on the crop box)
  const [isRotating, setIsRotating] = useState(false);
  const [rotateCenterScreen, setRotateCenterScreen] = useState({ x: 0, y: 0 });
  const [rotateStartAngle, setRotateStartAngle] = useState(0);
  const [cropAngleAtStart, setCropAngleAtStart] = useState(0);

  useEffect(() => {
    setImgLoaded(false);
    setImgError(false);
    setNaturalSize({ w: 0, h: 0 });
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [imageUrl]);

  useEffect(() => {
    if (!imgLoaded || !naturalSize.w || !naturalSize.h || !containerRef.current) return;
    const { width, height } = containerRef.current.getBoundingClientRect();
    setZoom(computeFitZoom(width, height, naturalSize.w, naturalSize.h));
    setPan({ x: 0, y: 0 });
  }, [imgLoaded, naturalSize.w, naturalSize.h, imageUrl]);

  // When the split overlay activates and no splitX is set, default to image center
  useEffect(() => {
    if (imgLoaded && showSplitOverlay && splitX == null && naturalSize.w > 0) {
      onSplitXChange?.(naturalSize.w / 2);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imgLoaded, showSplitOverlay]);

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.15 : 0.15;
    setZoom((prev) => Math.max(0.25, Math.min(8, prev + delta)));
  }, []);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (activeHandle || isDraggingSplit || isRotating) return;
      if (e.button !== 0) return;

      // Draw a new quad by dragging on the canvas (takes priority over panning)
      if (showQuadOverlay && onQuadPointsChange && imgLoaded && imgRef.current) {
        const { imgX, imgY } = screenToImage(e.clientX, e.clientY, imgRef, zoom, deskewAngle, naturalSize);
        setIsDrawingQuad(true);
        setQuadDrawStart({ x: imgX, y: imgY });
        return;
      }

      // Draw a new crop box by dragging on the canvas
      if (showCropOverlay && onCropBoxChange && imgLoaded && imgRef.current) {
        const { imgX, imgY } = screenToImage(e.clientX, e.clientY, imgRef, zoom, deskewAngle, naturalSize);
        setIsDrawing(true);
        setDrawStart({ x: imgX, y: imgY });
        return;
      }

      setIsPanning(true);
      setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
    },
    [
      pan,
      activeHandle,
      isDraggingSplit,
      isRotating,
      showQuadOverlay,
      onQuadPointsChange,
      showCropOverlay,
      onCropBoxChange,
      imgLoaded,
      zoom,
      naturalSize,
      deskewAngle,
    ]
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (activeQuadHandle != null && onQuadPointsChange && quadPoints && imgRef.current) {
        const { imgX, imgY } = screenToImage(
          e.clientX,
          e.clientY,
          imgRef,
          zoom,
          deskewAngle,
          naturalSize
        );
        onQuadPointsChange(updateQuadPoint(quadPoints, activeQuadHandle, [imgX, imgY]));
        return;
      }

      if (isDrawingQuad && onQuadPointsChange && imgRef.current) {
        const { imgX, imgY } = screenToImage(e.clientX, e.clientY, imgRef, zoom, deskewAngle, naturalSize);
        const x1 = Math.min(quadDrawStart.x, imgX);
        const y1 = Math.min(quadDrawStart.y, imgY);
        const x2 = Math.max(quadDrawStart.x, imgX);
        const y2 = Math.max(quadDrawStart.y, imgY);
        if (x2 - x1 > 2 && y2 - y1 > 2) {
          onQuadPointsChange([[x1, y1], [x2, y1], [x2, y2], [x1, y2]]);
        }
        return;
      }

      if (isDrawing && onCropBoxChange && imgRef.current) {
        const { imgX, imgY } = screenToImage(e.clientX, e.clientY, imgRef, zoom, deskewAngle, naturalSize);
        const x1 = Math.min(drawStart.x, imgX);
        const y1 = Math.min(drawStart.y, imgY);
        const x2 = Math.max(drawStart.x, imgX);
        const y2 = Math.max(drawStart.y, imgY);
        if (x2 - x1 > 2 && y2 - y1 > 2) {
          onCropBoxChange({ x1, y1, x2, y2 });
        }
        return;
      }

      if (isPanning) {
        setPan({ x: e.clientX - panStart.x, y: e.clientY - panStart.y });
        return;
      }

      if (activeHandle && dragStartCrop && onCropBoxChange) {
        const { dx, dy } = screenDeltaToImage(
          e.clientX - dragStartPos.x,
          e.clientY - dragStartPos.y,
          zoom,
          deskewAngle
        );
        let { x1, y1, x2, y2 } = dragStartCrop;

        if (activeHandle.includes("left")) x1 += dx;
        if (activeHandle.includes("right")) x2 += dx;
        if (activeHandle.includes("top")) y1 += dy;
        if (activeHandle.includes("bottom")) y2 += dy;
        if (activeHandle === "move") { x1 += dx; x2 += dx; y1 += dy; y2 += dy; }

        x1 = Math.max(0, Math.min(x1, naturalSize.w));
        x2 = Math.max(0, Math.min(x2, naturalSize.w));
        y1 = Math.max(0, Math.min(y1, naturalSize.h));
        y2 = Math.max(0, Math.min(y2, naturalSize.h));

        if (x2 - x1 > 10 && y2 - y1 > 10) {
          onCropBoxChange({ x1, y1, x2, y2 });
        }
        return;
      }

      if (isDraggingSplit && onSplitXChange && imgRef.current) {
        const { imgX } = screenToImage(e.clientX, e.clientY, imgRef, zoom, deskewAngle, naturalSize);
        onSplitXChange(imgX);
        return;
      }

      if (isRotating && onCropAngleChange) {
        const currentAngle =
          Math.atan2(e.clientY - rotateCenterScreen.y, e.clientX - rotateCenterScreen.x) *
          180 / Math.PI;
        const delta = currentAngle - rotateStartAngle;
        // Normalize to -180..180 to avoid wrap-around jumps
        const normalizedDelta = ((delta + 540) % 360) - 180;
        const newAngle = Math.max(-45, Math.min(45, cropAngleAtStart + normalizedDelta));
        onCropAngleChange(newAngle);
      }
    },
    [
      isDrawing, drawStart,
      isDrawingQuad, quadDrawStart,
      isPanning, panStart,
      activeQuadHandle, quadPoints,
      activeHandle, dragStartCrop, dragStartPos,
      zoom, naturalSize, deskewAngle,
      isDraggingSplit,
      isRotating, rotateCenterScreen, rotateStartAngle, cropAngleAtStart,
      onCropBoxChange, onQuadPointsChange, onSplitXChange, onCropAngleChange,
    ]
  );

  const handleMouseUp = useCallback(() => {
    setIsDrawing(false);
    setIsDrawingQuad(false);
    setIsPanning(false);
    setActiveQuadHandle(null);
    setActiveHandle(null);
    setIsDraggingSplit(false);
    setIsRotating(false);
  }, []);

  const resetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  const fitView = () => {
    if (!containerRef.current || !naturalSize.w) return;
    const { width, height } = containerRef.current.getBoundingClientRect();
    setZoom(computeFitZoom(width, height, naturalSize.w, naturalSize.h));
    setPan({ x: 0, y: 0 });
  };

  return (
    <div className="relative flex min-h-[420px] flex-1 flex-col overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-br from-white via-slate-50 to-slate-100 shadow-sm shadow-slate-200/70">
      {/* Zoom controls */}
      <div className="absolute top-3 right-3 z-20 flex flex-col gap-1">
        <Button
          variant="secondary"
          size="icon"
          className="h-7 w-7 border border-slate-200 bg-white/95 text-slate-600 shadow-sm backdrop-blur"
          onClick={() => setZoom((z) => Math.min(8, z + 0.25))}
        >
          <ZoomIn className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="secondary"
          size="icon"
          className="h-7 w-7 border border-slate-200 bg-white/95 text-slate-600 shadow-sm backdrop-blur"
          onClick={() => setZoom((z) => Math.max(0.25, z - 0.25))}
        >
          <ZoomOut className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="secondary"
          size="icon"
          className="h-7 w-7 border border-slate-200 bg-white/95 text-slate-600 shadow-sm backdrop-blur"
          onClick={fitView}
        >
          <Maximize2 className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="secondary"
          size="icon"
          className="h-7 w-7 border border-slate-200 bg-white/95 text-slate-600 shadow-sm backdrop-blur"
          onClick={resetView}
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Zoom indicator */}
      <div className="absolute bottom-3 left-3 z-20 rounded-lg border border-slate-200 bg-white/95 px-2 py-1 text-xs text-slate-500 tabular-nums shadow-sm backdrop-blur">
        {Math.round(zoom * 100)}%
      </div>

      {/* Canvas area */}
      <div
        ref={containerRef}
        className={cn(
          "flex-1 overflow-hidden relative select-none",
          isDrawing || isDrawingQuad
            ? "cursor-crosshair"
            : (showQuadOverlay && onQuadPointsChange) || (showCropOverlay && onCropBoxChange)
            ? "cursor-crosshair"
            : isPanning
            ? "cursor-grabbing"
            : "cursor-grab"
        )}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
      >
        {isLoading ? (
          <div className="flex items-center justify-center h-full">
            <Spinner size="lg" />
          </div>
        ) : isError || imgError ? (
          <div className="flex h-full items-center justify-center px-4">
            <div className="max-w-xs text-center">
              <div className="text-red-600 text-xs font-medium">{errorMessage}</div>
            </div>
          </div>
        ) : !imageUrl ? (
          <div className="flex items-center justify-center h-full px-4">
            <div className="max-w-xs text-center">
              <div className="text-slate-500 text-xs font-medium">{emptyMessage}</div>
            </div>
          </div>
        ) : (
          <div
            className="absolute inset-0 flex items-center justify-center"
            style={{ transform: `translate(${pan.x}px, ${pan.y}px)` }}
          >
            <div
              className="relative"
              style={{ transform: `scale(${zoom}) rotate(${deskewAngle}deg)`, transformOrigin: "center center" }}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                ref={imgRef}
                src={imageUrl}
                alt="Page artifact"
                className={cn("block max-w-none", !imgLoaded && "invisible")}
                draggable={false}
                onLoad={(e) => {
                  const img = e.currentTarget;
                  setNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
                  setImgLoaded(true);
                  setImgError(false);
                  onNaturalSizeChange?.(img.naturalWidth, img.naturalHeight);
                }}
                onError={() => {
                  setImgLoaded(false);
                  setImgError(true);
                }}
              />

              {!imgLoaded && !imgError && (
                <div className="absolute inset-0 flex items-center justify-center">
                  <Spinner size="lg" />
                </div>
              )}

              {imgLoaded && showQuadOverlay && quadPoints && (
                <QuadOverlay
                  quadPoints={quadPoints}
                  imgW={naturalSize.w}
                  imgH={naturalSize.h}
                  onHandleMouseDown={(index, e) => {
                    e.stopPropagation();
                    setActiveQuadHandle(index);
                  }}
                />
              )}

              {/* Crop box overlay — renders whenever showCropOverlay is on */}
              {imgLoaded && showCropOverlay && cropBox && (
                <CropOverlay
                  cropBox={cropBox}
                  imgW={naturalSize.w}
                  imgH={naturalSize.h}
                  showRotateHandle={!!onCropAngleChange}
                  onHandleMouseDown={(handle, e) => {
                    e.stopPropagation();
                    if (handle === "rotate" && imgRef.current) {
                      // Compute screen-space center of crop box for rotation math
                      const cx = (cropBox.x1 + cropBox.x2) / 2;
                      const cy = (cropBox.y1 + cropBox.y2) / 2;
                      const r = imgRef.current.getBoundingClientRect();
                      const imgCX = (r.left + r.right) / 2;
                      const imgCY = (r.top + r.bottom) / 2;
                      const rad = (deskewAngle * Math.PI) / 180;
                      const dxImg = (cx - naturalSize.w / 2) * zoom;
                      const dyImg = (cy - naturalSize.h / 2) * zoom;
                      const screenCX = imgCX + dxImg * Math.cos(rad) - dyImg * Math.sin(rad);
                      const screenCY = imgCY + dxImg * Math.sin(rad) + dyImg * Math.cos(rad);
                      setRotateCenterScreen({ x: screenCX, y: screenCY });
                      setRotateStartAngle(
                        Math.atan2(e.clientY - screenCY, e.clientX - screenCX) * 180 / Math.PI
                      );
                      setCropAngleAtStart(deskewAngle);
                      setIsRotating(true);
                    } else {
                      setActiveHandle(handle);
                      setDragStartPos({ x: e.clientX, y: e.clientY });
                      setDragStartCrop({ ...cropBox });
                    }
                  }}
                />
              )}

              {/* Split line overlay */}
              {imgLoaded && showSplitOverlay && splitX != null && (
                <div
                  className="absolute top-0 bottom-0 z-10 w-0.5 cursor-col-resize bg-cyan-500/80"
                  style={{ left: splitX }}
                  onMouseDown={(e) => {
                    e.stopPropagation();
                    setIsDraggingSplit(true);
                  }}
                >
                  {/* Drag handle */}
                  <div className="absolute top-1/2 h-8 w-3 -translate-x-1/2 -translate-y-1/2 rounded border border-cyan-400/60 bg-white/80 shadow-sm cursor-col-resize" />
                  {/* Child sub-page labels */}
                  <span className="pointer-events-none absolute top-2 right-full mr-1.5 rounded bg-cyan-500 px-1 py-0.5 text-2xs font-semibold text-white shadow-sm">
                    sub 0
                  </span>
                  <span className="pointer-events-none absolute top-2 left-full ml-1.5 rounded bg-cyan-500 px-1 py-0.5 text-2xs font-semibold text-white shadow-sm">
                    sub 1
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function QuadOverlay({
  quadPoints,
  imgW,
  imgH,
  onHandleMouseDown,
}: {
  quadPoints: QuadPoint[];
  imgW: number;
  imgH: number;
  onHandleMouseDown: (index: number, e: React.MouseEvent) => void;
}) {
  const pointsAttr = quadPoints.map(([x, y]) => `${x},${y}`).join(" ");
  const cornerLabels = ["TL", "TR", "BR", "BL"] as const;

  return (
    <>
      <svg
        className="pointer-events-none absolute inset-0"
        width={imgW}
        height={imgH}
        viewBox={`0 0 ${imgW} ${imgH}`}
      >
        <path
          d={`M0 0 H${imgW} V${imgH} H0 Z M${pointsAttr} Z`}
          fill="rgba(15, 23, 42, 0.42)"
          fillRule="evenodd"
        />
        <polygon
          points={pointsAttr}
          fill="rgba(99, 102, 241, 0.12)"
          stroke="rgba(99, 102, 241, 0.95)"
          strokeWidth="3"
          strokeLinejoin="round"
        />
        <polyline
          points={pointsAttr}
          fill="none"
          stroke="rgba(255, 255, 255, 0.95)"
          strokeWidth="1"
          strokeDasharray="8 6"
          strokeLinejoin="round"
        />
      </svg>
      {quadPoints.map(([x, y], index) => (
        <div
          key={`${index}-${x}-${y}`}
          className="absolute z-10 flex h-10 w-10 -translate-x-1/2 -translate-y-1/2 cursor-grab items-center justify-center rounded-full active:cursor-grabbing"
          style={{ left: x, top: y }}
          onMouseDown={(event) => onHandleMouseDown(index, event)}
        >
          <div className="pointer-events-none absolute h-4 w-4 rounded-full border-2 border-white bg-indigo-500 shadow-[0_0_0_3px_rgba(99,102,241,0.18)]" />
          <div className="pointer-events-none absolute top-full mt-1 rounded bg-slate-900/80 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-white shadow-sm">
            {cornerLabels[index]}
          </div>
        </div>
      ))}
    </>
  );
}

function CropOverlay({
  cropBox,
  imgW,
  imgH,
  showRotateHandle,
  onHandleMouseDown,
}: {
  cropBox: { x1: number; y1: number; x2: number; y2: number };
  imgW: number;
  imgH: number;
  showRotateHandle: boolean;
  onHandleMouseDown: (handle: string, e: React.MouseEvent) => void;
}) {
  const { x1, y1, x2, y2 } = cropBox;
  const w = x2 - x1;
  const h = y2 - y1;
  const cx = x1 + w / 2;
  const rotHandleOffset = 32;
  const rotHandleY = y1 - rotHandleOffset;

  const handles = [
    { id: "top-left", style: { left: x1 - 5, top: y1 - 5 } },
    { id: "top-right", style: { left: x2 - 5, top: y1 - 5 } },
    { id: "bottom-left", style: { left: x1 - 5, top: y2 - 5 } },
    { id: "bottom-right", style: { left: x2 - 5, top: y2 - 5 } },
    { id: "top", style: { left: cx - 5, top: y1 - 5 } },
    { id: "bottom", style: { left: cx - 5, top: y2 - 5 } },
    { id: "left", style: { left: x1 - 5, top: y1 + h / 2 - 5 } },
    { id: "right", style: { left: x2 - 5, top: y1 + h / 2 - 5 } },
  ];

  return (
    <>
      {/* Soft mask around crop area */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background: `
            linear-gradient(to right, rgba(148,163,184,0.42) ${x1}px, transparent ${x1}px),
            linear-gradient(to left, rgba(148,163,184,0.42) ${imgW - x2}px, transparent ${imgW - x2}px),
            linear-gradient(to bottom, rgba(148,163,184,0.42) ${y1}px, transparent ${y1}px),
            linear-gradient(to top, rgba(148,163,184,0.42) ${imgH - y2}px, transparent ${imgH - y2}px)
          `,
        }}
      />

      {/* Crop rectangle border */}
      <div
        className="absolute border-2 border-indigo-400 pointer-events-none"
        style={{ left: x1, top: y1, width: w, height: h }}
      >
        {/* Rule-of-thirds grid */}
        <div className="absolute inset-0 pointer-events-none opacity-30">
          <div className="absolute top-1/3 left-0 right-0 border-t border-indigo-400/50" />
          <div className="absolute top-2/3 left-0 right-0 border-t border-indigo-400/50" />
          <div className="absolute left-1/3 top-0 bottom-0 border-l border-indigo-400/50" />
          <div className="absolute left-2/3 top-0 bottom-0 border-l border-indigo-400/50" />
        </div>
      </div>

      {/* Move handle (center) */}
      <div
        className="absolute cursor-move"
        style={{ left: x1, top: y1, width: w, height: h }}
        onMouseDown={(e) => onHandleMouseDown("move", e)}
      />

      {/* Resize handles */}
      {handles.map((handle) => (
        <div
          key={handle.id}
          className="absolute z-10 h-2.5 w-2.5 cursor-pointer rounded-sm border-2 border-white bg-indigo-500 shadow-sm"
          style={handle.style}
          onMouseDown={(e) => onHandleMouseDown(handle.id, e)}
        />
      ))}

      {/* Rotation handle: stem + circle above top-center */}
      {showRotateHandle && (
        <>
          <div
            className="absolute pointer-events-none"
            style={{
              left: cx - 0.5,
              top: rotHandleY + 14,
              width: 1,
              height: rotHandleOffset - 14,
              backgroundColor: "rgba(99,102,241,0.5)",
            }}
          />
          <div
            className="absolute z-20 flex h-6 w-6 cursor-grab items-center justify-center rounded-full border-2 border-indigo-400 bg-white shadow-sm select-none hover:bg-indigo-50 active:cursor-grabbing"
            style={{ left: cx - 12, top: rotHandleY - 12 }}
            onMouseDown={(e) => onHandleMouseDown("rotate", e)}
            title="Drag to rotate (set deskew angle)"
          >
            <span className="text-indigo-500 text-xs leading-none">↻</span>
          </div>
        </>
      )}
    </>
  );
}
