"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FileSearch,
  Layers,
} from "lucide-react";
import { useArtifactJson, useArtifactPreview } from "@/lib/artifacts";
import { ArtifactLinkButton } from "@/components/shared/artifact-link-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { formatDuration, formatScore, snakeToTitle } from "@/lib/utils";
import type {
  LayoutAdjudicationResult,
  LayoutDecisionSource,
  LayoutDetectResponse,
  LayoutRegion,
  LayoutRegionType,
} from "@/types/api";

interface LayoutOverlayProps {
  imageUri: string | null;
  layoutUri: string | null;
  pageLabel?: string;
  originalImageSize?: ImageSize | null;
}

interface ImageSize {
  width: number;
  height: number;
}

interface DisplayedImageFrame extends ImageSize {
  left: number;
  top: number;
}

const REGION_STYLES: Record<
  LayoutRegionType,
  { label: string; color: string }
> = {
  title: { label: "Title", color: "#2563eb" },
  text_block: { label: "Text", color: "#16a34a" },
  table: { label: "Table", color: "#dc2626" },
  image: { label: "Image", color: "#9333ea" },
  caption: { label: "Caption", color: "#ea580c" },
};

export function LayoutOverlay({
  imageUri,
  layoutUri,
  pageLabel,
  originalImageSize,
}: LayoutOverlayProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const [naturalSize, setNaturalSize] = useState<ImageSize | null>(null);
  const [displayedImage, setDisplayedImage] = useState<DisplayedImageFrame | null>(
    null
  );
  const [imageError, setImageError] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [selectedRegionId, setSelectedRegionId] = useState<string | null>(null);

  const imageQuery = useArtifactPreview(imageUri);
  const layoutQuery = useArtifactJson<LayoutAdjudicationResult>(layoutUri);

  const layout = layoutQuery.data;
  const regions = layout?.final_layout_result ?? [];
  const source = layout?.layout_decision_source ?? "none";
  const layoutInput = layout?.layout_input ?? null;
  const resolvedOriginalSize = resolveOriginalImageSize(
    layout,
    naturalSize,
    originalImageSize
  );

  const isLoading = imageQuery.isLoading || layoutQuery.isLoading;
  const hasError =
    imageError ||
    imageQuery.isError ||
    layoutQuery.isError ||
    !imageUri ||
    !layoutUri;

  const syncDisplayedImage = useCallback(() => {
    const container = containerRef.current;
    const image = imageRef.current;

    if (!container || !image) {
      setDisplayedImage(null);
      return;
    }

    const containerRect = container.getBoundingClientRect();
    const imageRect = image.getBoundingClientRect();

    if (imageRect.width <= 0 || imageRect.height <= 0) {
      setDisplayedImage(null);
      return;
    }

    const nextFrame: DisplayedImageFrame = {
      left: Math.max(0, imageRect.left - containerRect.left),
      top: Math.max(0, imageRect.top - containerRect.top),
      width: imageRect.width,
      height: imageRect.height,
    };

    setDisplayedImage((current) =>
      areFramesEqual(current, nextFrame) ? current : nextFrame
    );
  }, []);

  useEffect(() => {
    syncDisplayedImage();

    const container = containerRef.current;
    const image = imageRef.current;

    if (typeof window === "undefined") return undefined;

    window.addEventListener("resize", syncDisplayedImage);

    if (typeof ResizeObserver === "undefined" || !container || !image) {
      return () => window.removeEventListener("resize", syncDisplayedImage);
    }

    const resizeObserver = new ResizeObserver(() => {
      syncDisplayedImage();
    });

    resizeObserver.observe(container);
    resizeObserver.observe(image);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener("resize", syncDisplayedImage);
    };
  }, [imageQuery.data?.blobUrl, layoutUri, syncDisplayedImage]);

  return (
    <Card className="overflow-hidden border-slate-200 shadow-sm">
      <CardHeader className="border-b border-slate-200 bg-slate-50/80">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-white text-slate-500 shadow-sm">
                <Layers className="h-4 w-4" />
              </span>
              <div>
                <CardTitle>
                  Layout Viewer{pageLabel ? ` - ${pageLabel}` : ""}
                </CardTitle>
                <CardDescription>
                  Rendered from the persisted layout adjudication artifact.
                </CardDescription>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={sourceBadgeVariant(source)} dot>
              Decision: {sourceLabel(source)}
            </Badge>
            {layoutInput && (
              <Badge variant="muted">
                Analyzed: {artifactRoleLabel(layoutInput.artifact_role)}
              </Badge>
            )}
            <Badge variant="muted">{regions.length} regions</Badge>
            <ArtifactLinkButton
              uri={imageUri}
              label="Open image"
              size="xs"
              variant="outline"
            />
            <ArtifactLinkButton
              uri={layoutUri}
              label="Layout JSON"
              size="xs"
              variant="ghost"
            />
          </div>
        </div>
      </CardHeader>

      <CardContent className="p-4">
        {isLoading ? (
          <div className="flex min-h-[320px] items-center justify-center rounded-xl border border-slate-200 bg-slate-50">
            <Spinner size="lg" />
          </div>
        ) : hasError || !layout ? (
          <ErrorState />
        ) : (
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.8fr)_minmax(320px,0.9fr)]">
            <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="text-sm font-semibold text-slate-900">
                    Page image
                  </p>
                  <p className="text-xs text-slate-500">
                    Canonical pixel boxes overlaid on the stored output image.
                  </p>
                </div>
                <Badge variant="muted">
                  {resolvedOriginalSize
                    ? `${resolvedOriginalSize.width} x ${resolvedOriginalSize.height}px`
                    : "Loading dimensions"}
                </Badge>
              </div>

              <div className="overflow-auto rounded-xl border border-slate-200 bg-white p-2">
                <div
                  ref={containerRef}
                  className="relative mx-auto w-full max-w-full"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    ref={imageRef}
                    src={imageQuery.data?.blobUrl}
                    alt={pageLabel ? `Layout for ${pageLabel}` : "Layout overlay"}
                    className="block h-auto w-full rounded-lg"
                    onError={() => {
                      setImageError(true);
                      setDisplayedImage(null);
                    }}
                    onLoad={(event) => {
                      const image = event.currentTarget;
                      setImageError(false);
                      setNaturalSize({
                        width: image.naturalWidth,
                        height: image.naturalHeight,
                      });
                      syncDisplayedImage();
                    }}
                  />

                  {displayedImage && resolvedOriginalSize && (
                    <div
                      className="pointer-events-none absolute overflow-hidden rounded-lg"
                      style={{
                        left: displayedImage.left,
                        top: displayedImage.top,
                        width: displayedImage.width,
                        height: displayedImage.height,
                      }}
                    >
                      {regions.map((region) => {
                        const box = getBoxStyle(
                          region,
                          resolvedOriginalSize,
                          displayedImage
                        );
                        if (!box) return null;

                        const config = REGION_STYLES[region.type];
                        const isSelected = selectedRegionId === region.id;
                        return (
                          <div
                            key={region.id}
                            className="pointer-events-auto absolute overflow-hidden rounded-sm border-2 cursor-pointer transition-shadow"
                            style={{
                              left: box.left,
                              top: box.top,
                              width: box.width,
                              height: box.height,
                              borderColor: config.color,
                              backgroundColor: hexToRgba(config.color, isSelected ? 0.28 : 0.12),
                              boxShadow: isSelected
                                ? `0 0 0 2px ${config.color}, 0 0 0 4px ${hexToRgba(config.color, 0.35)}`
                                : `0 0 0 1px ${hexToRgba(config.color, 0.25)}`,
                            }}
                            onClick={() =>
                              setSelectedRegionId((prev) =>
                                prev === region.id ? null : region.id
                              )
                            }
                          >
                            <div
                              className="absolute left-0 top-0 max-w-full truncate rounded-br-md px-1.5 py-0.5 text-[10px] font-semibold leading-4 text-white shadow-sm"
                              style={{ backgroundColor: config.color }}
                              title={region.text ? `${config.label} (${region.id}) — ${region.text}` : `${config.label} (${region.id}) - ${formatScore(region.confidence, 2)}`}
                            >
                              {config.label} {formatScore(region.confidence, 2)}
                            </div>
                            {region.text && (
                              <div
                                className="absolute bottom-0 left-0 right-0 truncate px-1 py-0.5 text-[9px] leading-3 text-white"
                                style={{ backgroundColor: hexToRgba(config.color, 0.75) }}
                                title={region.text}
                              >
                                {region.text}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>

              <div className="mt-3 flex items-start gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-500">
                <FileSearch className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
                <span>
                  Boxes are rendered from <code>final_layout_result</code> using
                  the persisted canonical coordinates scaled into the displayed
                  image frame.
                </span>
              </div>
            </div>

            <div className="space-y-4">
              <RegionTextPanel
                regions={regions}
                selectedRegionId={selectedRegionId}
                onSelectRegion={setSelectedRegionId}
              />

              <Panel title="Decision Panel">
                <Metric label="Decision source" value={sourceLabel(source)} />
                {layoutInput && (
                  <Metric
                    label="Analyzed artifact"
                    value={artifactRoleLabel(layoutInput.artifact_role)}
                  />
                )}
                {layoutInput && (
                  <Metric
                    label="Input source"
                    value={inputSourceLabel(layoutInput.input_source)}
                  />
                )}
                {layoutInput && (
                  <Metric
                    label="Artifact freshness"
                    value={
                      layoutInput.analyzed_artifact_uri ===
                      layoutInput.source_page_artifact_uri
                        ? "Direct current artifact"
                        : "Derivative of current artifact"
                    }
                  />
                )}
                {layout.ocr_source != null && (
                  <Metric
                    label="OCR source"
                    value={layout.ocr_source === "google" ? "Google Document AI" : "PaddleOCR"}
                  />
                )}
                <Metric label="Number of regions" value={String(regions.length)} />
                <Metric
                  label="Processing time"
                  value={formatDuration(layout.processing_time_ms)}
                />
                <Metric
                  label="Status"
                  value={layout.status === "done" ? "Displayable" : "Failed"}
                />
                {layout.consensus_confidence != null && (
                  <Metric
                    label="Consensus confidence"
                    value={formatScore(layout.consensus_confidence, 3)}
                  />
                )}
                {layout.google_response_time_ms != null && (
                  <Metric
                    label="Google latency"
                    value={formatDuration(layout.google_response_time_ms)}
                  />
                )}
                {layout.error && (
                  <div className="rounded-lg border border-orange-200 bg-orange-50 px-3 py-2 text-xs text-orange-700">
                    {layout.error}
                  </div>
                )}
              </Panel>

              <Panel title="Legend">
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
                  {(
                    Object.entries(REGION_STYLES) as Array<
                      [LayoutRegionType, (typeof REGION_STYLES)[LayoutRegionType]]
                    >
                  ).map(([type, config]) => (
                    <div
                      key={type}
                      className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-3 py-2"
                    >
                      <div className="flex items-center gap-2">
                        <span
                          className="h-3 w-3 rounded-full"
                          style={{ backgroundColor: config.color }}
                        />
                        <span className="text-xs font-medium text-slate-700">
                          {config.label}
                        </span>
                      </div>
                      <code className="text-[11px] text-slate-400">{type}</code>
                    </div>
                  ))}
                </div>
              </Panel>

              <Panel title="Advanced Panel">
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  className="w-full justify-between"
                  onClick={() => setShowAdvanced((value) => !value)}
                >
                  <span>Show IEP2A, IEP2B, and Google metadata</span>
                  {showAdvanced ? (
                    <ChevronDown className="h-4 w-4" />
                  ) : (
                    <ChevronRight className="h-4 w-4" />
                  )}
                </Button>

                {showAdvanced && (
                  <div className="space-y-3">
                    <AdvancedSection
                      title="IEP2A result"
                      data={layout.iep2a_result}
                      summary={summarizeModel(layout.iep2a_result)}
                    />
                    <AdvancedSection
                      title="IEP2B result"
                      data={layout.iep2b_result}
                      summary={summarizeModel(layout.iep2b_result)}
                    />
                    <AdvancedSection
                      title="Google metadata"
                      data={layout.google_document_ai_result}
                    />
                  </div>
                )}
              </Panel>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RegionTextPanel({
  regions,
  selectedRegionId,
  onSelectRegion,
}: {
  regions: LayoutRegion[];
  selectedRegionId: string | null;
  onSelectRegion: (id: string | null) => void;
}) {
  const textRegions = regions.filter((r) => r.text);
  const scrollRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  useEffect(() => {
    if (!selectedRegionId) return;
    const el = scrollRefs.current.get(selectedRegionId);
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedRegionId]);

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
      <p className="mb-3 text-sm font-semibold text-slate-900">Extracted Text</p>
      {textRegions.length === 0 ? (
        <p className="text-xs text-slate-400">No text regions in this layout.</p>
      ) : (
        <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
          {textRegions.map((region) => {
            const config = REGION_STYLES[region.type];
            const isSelected = selectedRegionId === region.id;
            return (
              <div
                key={region.id}
                ref={(el) => {
                  if (el) scrollRefs.current.set(region.id, el);
                  else scrollRefs.current.delete(region.id);
                }}
                className="cursor-pointer rounded-lg border-2 bg-white px-3 py-2 transition-colors"
                style={{
                  borderColor: isSelected ? config.color : "transparent",
                  outline: isSelected ? `1px solid ${hexToRgba(config.color, 0.35)}` : "none",
                  backgroundColor: isSelected ? hexToRgba(config.color, 0.06) : "white",
                  boxShadow: isSelected ? `0 0 0 3px ${hexToRgba(config.color, 0.15)}` : "none",
                }}
                onClick={() => onSelectRegion(isSelected ? null : region.id)}
              >
                <div className="mb-1 flex items-center gap-1.5">
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ backgroundColor: config.color }}
                  />
                  <span className="text-[11px] font-semibold text-slate-500">
                    {config.label}
                  </span>
                  <span className="text-[11px] text-slate-400">
                    {formatScore(region.confidence, 2)}
                  </span>
                </div>
                <p className="select-text whitespace-pre-wrap break-words text-xs text-slate-800">
                  {region.text}
                </p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ErrorState() {
  return (
    <div className="flex min-h-[320px] flex-col items-center justify-center gap-3 rounded-xl border border-red-200 bg-red-50 px-6 text-center">
      <AlertTriangle className="h-6 w-6 text-red-500" />
      <div>
        <p className="text-sm font-semibold text-red-700">
          Layout visualization unavailable
        </p>
        <p className="mt-1 text-xs text-red-600">
          The image preview or persisted layout JSON could not be loaded.
        </p>
      </div>
    </div>
  );
}

function Panel({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
      <p className="mb-3 text-sm font-semibold text-slate-900">{title}</p>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4 rounded-lg border border-slate-200 bg-white px-3 py-2">
      <span className="text-xs text-slate-500">{label}</span>
      <span className="text-xs font-medium text-slate-800">{value}</span>
    </div>
  );
}

function AdvancedSection({
  title,
  data,
  summary,
}: {
  title: string;
  data: unknown;
  summary?: string | null;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-3 py-2">
        <p className="text-xs font-semibold text-slate-800">{title}</p>
        {summary && <p className="mt-1 text-[11px] text-slate-500">{summary}</p>}
      </div>
      <div className="p-3">
        {data ? (
          <pre className="overflow-x-auto rounded-lg bg-slate-950 px-3 py-3 text-[11px] leading-5 text-slate-100">
            {JSON.stringify(data, null, 2)}
          </pre>
        ) : (
          <p className="text-xs text-slate-400">No data available.</p>
        )}
      </div>
    </div>
  );
}

function summarizeModel(result: LayoutDetectResponse | null): string | null {
  if (!result) return null;
  const meanConfidence = formatScore(result.layout_conf_summary.mean_conf, 3);
  return `${snakeToTitle(result.detector_type)} - ${result.regions.length} regions - ${formatDuration(result.processing_time_ms)} - mean conf ${meanConfidence}`;
}

function sourceLabel(source: LayoutDecisionSource): string {
  switch (source) {
    case "local_agreement":
      return "Local agreement";
    case "google_document_ai":
      return "Google adjudicated";
    case "local_fallback_unverified":
      return "Best local result";
    default:
      return "Unavailable";
  }
}

function sourceBadgeVariant(source: LayoutDecisionSource) {
  switch (source) {
    case "local_agreement":
      return "success" as const;
    case "google_document_ai":
      return "info" as const;
    case "local_fallback_unverified":
      return "warning" as const;
    default:
      return "muted" as const;
  }
}

function artifactRoleLabel(role: NonNullable<LayoutAdjudicationResult["layout_input"]>["artifact_role"]): string {
  switch (role) {
    case "human_corrected":
      return "Human corrected";
    case "split_child":
      return "Split child";
    case "normalized_output":
      return "Normalized output";
    case "original_upload":
      return "Original upload";
    default:
      return snakeToTitle(role);
  }
}

function inputSourceLabel(source: NonNullable<LayoutAdjudicationResult["layout_input"]>["input_source"]): string {
  switch (source) {
    case "downsampled":
      return "Downsampled derivative";
    case "page_output":
      return "Current page artifact";
    default:
      return snakeToTitle(source);
  }
}

function getBoxStyle(
  region: LayoutRegion,
  originalSize: ImageSize,
  displayedImage: ImageSize
) {
  if (
    originalSize.width <= 0 ||
    originalSize.height <= 0 ||
    displayedImage.width <= 0 ||
    displayedImage.height <= 0
  ) {
    return null;
  }

  const scaleX = displayedImage.width / originalSize.width;
  const scaleY = displayedImage.height / originalSize.height;

  const left = clampPx(region.bbox.x_min * scaleX, displayedImage.width);
  const right = clampPx(region.bbox.x_max * scaleX, displayedImage.width);
  const top = clampPx(region.bbox.y_min * scaleY, displayedImage.height);
  const bottom = clampPx(region.bbox.y_max * scaleY, displayedImage.height);
  const width = Math.max(0, right - left);
  const height = Math.max(0, bottom - top);

  if (width <= 0 || height <= 0) return null;

  return { left, top, width, height };
}

function resolveOriginalImageSize(
  layout: LayoutAdjudicationResult | undefined,
  fallbackSize: ImageSize | null,
  propSize?: ImageSize | null
): ImageSize | null {
  const metadataCandidates: Array<Record<string, unknown> | null> = [
    asRecord(layout?.layout_input),
    asRecord(layout),
    asRecord(layout?.google_document_ai_result),
    asRecord(layout?.iep2a_result as unknown),
    asRecord(layout?.iep2b_result as unknown),
  ];

  if (isValidImageSize(propSize)) return propSize;

  for (const candidate of metadataCandidates) {
    const metadataSize = readImageSize(candidate, [
      ["canonical_output_width", "canonical_output_height"],
      ["original_width", "original_height"],
      ["image_width", "image_height"],
      ["page_width", "page_height"],
    ]);
    if (metadataSize) return metadataSize;
  }

  return isValidImageSize(fallbackSize) ? fallbackSize : null;
}

function readImageSize(
  source: Record<string, unknown> | null,
  keys: Array<[widthKey: string, heightKey: string]>
): ImageSize | null {
  if (!source) return null;

  for (const [widthKey, heightKey] of keys) {
    const width = asPositiveNumber(source[widthKey]);
    const height = asPositiveNumber(source[heightKey]);
    if (width != null && height != null) {
      return { width, height };
    }
  }

  return null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object") return null;
  return value as Record<string, unknown>;
}

function asPositiveNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : null;
}

function isValidImageSize(value: ImageSize | null | undefined): value is ImageSize {
  return Boolean(
    value &&
      Number.isFinite(value.width) &&
      value.width > 0 &&
      Number.isFinite(value.height) &&
      value.height > 0
  );
}

function areFramesEqual(
  current: DisplayedImageFrame | null,
  next: DisplayedImageFrame
): boolean {
  if (!current) return false;
  const threshold = 0.5;

  return (
    Math.abs(current.left - next.left) < threshold &&
    Math.abs(current.top - next.top) < threshold &&
    Math.abs(current.width - next.width) < threshold &&
    Math.abs(current.height - next.height) < threshold
  );
}

function clampPx(value: number, max: number): number {
  if (Number.isNaN(value) || !Number.isFinite(value)) return 0;
  return Math.min(max, Math.max(0, value));
}

function hexToRgba(hex: string, alpha: number): string {
  const normalized = hex.replace("#", "");
  const value = normalized.length === 3
    ? normalized
        .split("")
        .map((part) => `${part}${part}`)
        .join("")
    : normalized;

  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);

  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
