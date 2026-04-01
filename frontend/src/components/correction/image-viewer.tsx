"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { ZoomIn, ZoomOut, Maximize2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

interface CropBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

interface ImageViewerProps {
  imageUrl: string | null;
  cropBox?: CropBox | null;
  splitX?: number | null;
  deskewAngle?: number;
  onCropBoxChange?: (box: CropBox) => void;
  onSplitXChange?: (x: number) => void;
  showCropOverlay?: boolean;
  showSplitOverlay?: boolean;
  isLoading?: boolean;
}

export function ImageViewer({
  imageUrl,
  cropBox,
  splitX,
  deskewAngle = 0,
  onCropBoxChange,
  onSplitXChange,
  showCropOverlay = true,
  showSplitOverlay = false,
  isLoading = false,
}: ImageViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const [panStart, setPanStart] = useState({ x: 0, y: 0 });
  const [imgLoaded, setImgLoaded] = useState(false);
  const [naturalSize, setNaturalSize] = useState({ w: 0, h: 0 });

  // Active drag state for crop handles
  const [activeHandle, setActiveHandle] = useState<string | null>(null);
  const [dragStartPos, setDragStartPos] = useState({ x: 0, y: 0 });
  const [dragStartCrop, setDragStartCrop] = useState<CropBox | null>(null);

  // Draw-new-crop-box drag state
  const [isDrawing, setIsDrawing] = useState(false);
  const [drawStart, setDrawStart] = useState({ x: 0, y: 0 });

  // Active drag for split line
  const [isDraggingSplit, setIsDraggingSplit] = useState(false);

  useEffect(() => {
    setImgLoaded(false);
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [imageUrl]);

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.15 : 0.15;
    setZoom((prev) => Math.max(0.25, Math.min(8, prev + delta)));
  }, []);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (activeHandle || isDraggingSplit) return;
      if (e.button !== 0) return;

      // Draw a new crop box by dragging on the canvas
      if (showCropOverlay && onCropBoxChange && imgLoaded && imgRef.current) {
        const imgRect = imgRef.current.getBoundingClientRect();
        const imgX = Math.max(0, Math.min((e.clientX - imgRect.left) / zoom, naturalSize.w));
        const imgY = Math.max(0, Math.min((e.clientY - imgRect.top) / zoom, naturalSize.h));
        setIsDrawing(true);
        setDrawStart({ x: imgX, y: imgY });
        return;
      }

      setIsPanning(true);
      setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
    },
    [pan, activeHandle, isDraggingSplit, showCropOverlay, onCropBoxChange, imgLoaded, zoom, naturalSize]
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (isDrawing && onCropBoxChange && imgRef.current) {
        const imgRect = imgRef.current.getBoundingClientRect();
        const imgX = Math.max(0, Math.min((e.clientX - imgRect.left) / zoom, naturalSize.w));
        const imgY = Math.max(0, Math.min((e.clientY - imgRect.top) / zoom, naturalSize.h));
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
        if (!containerRef.current) return;

        // Convert screen delta to image coords
        const dx = (e.clientX - dragStartPos.x) / zoom;
        const dy = (e.clientY - dragStartPos.y) / zoom;
        let { x1, y1, x2, y2 } = dragStartCrop;

        if (activeHandle.includes("left")) x1 += dx;
        if (activeHandle.includes("right")) x2 += dx;
        if (activeHandle.includes("top")) y1 += dy;
        if (activeHandle.includes("bottom")) y2 += dy;
        if (activeHandle === "move") { x1 += dx; x2 += dx; y1 += dy; y2 += dy; }

        // Clamp to natural image bounds
        x1 = Math.max(0, Math.min(x1, naturalSize.w));
        x2 = Math.max(0, Math.min(x2, naturalSize.w));
        y1 = Math.max(0, Math.min(y1, naturalSize.h));
        y2 = Math.max(0, Math.min(y2, naturalSize.h));

        if (x2 - x1 > 10 && y2 - y1 > 10) {
          onCropBoxChange({ x1, y1, x2, y2 });
        }
        return;
      }

      if (isDraggingSplit && onSplitXChange) {
        if (!containerRef.current || !imgRef.current) return;
        const imgRect = imgRef.current.getBoundingClientRect();
        const relX = (e.clientX - imgRect.left) / zoom;
        const newSplitX = Math.max(0, Math.min(relX, naturalSize.w));
        onSplitXChange(newSplitX);
      }
    },
    [
      isDrawing,
      drawStart,
      isPanning,
      panStart,
      activeHandle,
      dragStartCrop,
      dragStartPos,
      zoom,
      naturalSize,
      isDraggingSplit,
      onCropBoxChange,
      onSplitXChange,
    ]
  );

  const handleMouseUp = useCallback(() => {
    setIsDrawing(false);
    setIsPanning(false);
    setActiveHandle(null);
    setIsDraggingSplit(false);
  }, []);

  const resetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  const fitView = () => {
    if (!containerRef.current || !naturalSize.w) return;
    const { width, height } = containerRef.current.getBoundingClientRect();
    const scaleX = width / naturalSize.w;
    const scaleY = height / naturalSize.h;
    setZoom(Math.min(scaleX, scaleY, 1) * 0.9);
    setPan({ x: 0, y: 0 });
  };

  return (
    <div className="relative flex h-full flex-col overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-br from-white via-slate-50 to-slate-100 shadow-sm shadow-slate-200/70">
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
          isDrawing
            ? "cursor-crosshair"
            : showCropOverlay && onCropBoxChange
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
        {isLoading || !imageUrl ? (
          <div className="flex items-center justify-center h-full">
            {isLoading ? (
              <Spinner size="lg" />
            ) : (
              <div className="text-center">
                <div className="text-slate-400 text-xs">No image selected</div>
              </div>
            )}
          </div>
        ) : (
          <div
            className="absolute inset-0 flex items-center justify-center"
            style={{ transform: `translate(${pan.x}px, ${pan.y}px)` }}
          >
            <div className="relative" style={{ transform: `scale(${zoom}) rotate(${deskewAngle}deg)`, transformOrigin: "center center" }}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                ref={imgRef}
                src={imageUrl}
                alt="Page artifact"
                className="block max-w-none"
                draggable={false}
                onLoad={(e) => {
                  const img = e.currentTarget;
                  setNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
                  setImgLoaded(true);
                }}
                onError={() => setImgLoaded(false)}
              />

              {/* Crop box overlay — renders whenever showCropOverlay is on (cropBox may be null during initial draw) */}
              {imgLoaded && showCropOverlay && cropBox && (
                <CropOverlay
                  cropBox={cropBox}
                  imgW={naturalSize.w}
                  imgH={naturalSize.h}
                  onHandleMouseDown={(handle, e) => {
                    e.stopPropagation();
                    setActiveHandle(handle);
                    setDragStartPos({ x: e.clientX, y: e.clientY });
                    setDragStartCrop({ ...cropBox });
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

function CropOverlay({
  cropBox,
  imgW,
  imgH,
  onHandleMouseDown,
}: {
  cropBox: { x1: number; y1: number; x2: number; y2: number };
  imgW: number;
  imgH: number;
  onHandleMouseDown: (handle: string, e: React.MouseEvent) => void;
}) {
  const { x1, y1, x2, y2 } = cropBox;
  const w = x2 - x1;
  const h = y2 - y1;

  const handles = [
    { id: "top-left", style: { left: x1 - 5, top: y1 - 5 } },
    { id: "top-right", style: { left: x2 - 5, top: y1 - 5 } },
    { id: "bottom-left", style: { left: x1 - 5, top: y2 - 5 } },
    { id: "bottom-right", style: { left: x2 - 5, top: y2 - 5 } },
    { id: "top", style: { left: x1 + w / 2 - 5, top: y1 - 5 } },
    { id: "bottom", style: { left: x1 + w / 2 - 5, top: y2 - 5 } },
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
    </>
  );
}
