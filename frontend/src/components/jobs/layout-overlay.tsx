"use client";

import { useState } from "react";
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
}

interface NaturalSize {
  width: number;
  height: number;
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
}: LayoutOverlayProps) {
  const [naturalSize, setNaturalSize] = useState<NaturalSize | null>(null);
  const [imageError, setImageError] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const imageQuery = useArtifactPreview(imageUri);
  const layoutQuery = useArtifactJson<LayoutAdjudicationResult>(layoutUri);

  const layout = layoutQuery.data;
  const regions = layout?.final_layout_result ?? [];
  const source = layout?.layout_decision_source ?? "none";

  const isLoading = imageQuery.isLoading || layoutQuery.isLoading;
  const hasError =
    imageError ||
    imageQuery.isError ||
    layoutQuery.isError ||
    !imageUri ||
    !layoutUri;

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
              {sourceLabel(source)}
            </Badge>
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
                  {naturalSize
                    ? `${naturalSize.width} x ${naturalSize.height}px`
                    : "Loading dimensions"}
                </Badge>
              </div>

              <div className="overflow-auto rounded-xl border border-slate-200 bg-white p-2">
                <div className="relative mx-auto w-full max-w-full">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={imageQuery.data?.blobUrl}
                    alt={pageLabel ? `Layout for ${pageLabel}` : "Layout overlay"}
                    className="block h-auto w-full rounded-lg"
                    onError={() => setImageError(true)}
                    onLoad={(event) => {
                      setImageError(false);
                      setNaturalSize({
                        width: event.currentTarget.naturalWidth,
                        height: event.currentTarget.naturalHeight,
                      });
                    }}
                  />

                  {naturalSize && (
                    <div className="pointer-events-none absolute inset-0">
                      {regions.map((region) => {
                        const box = getBoxStyle(region, naturalSize);
                        if (!box) return null;

                        const config = REGION_STYLES[region.type];
                        return (
                          <div
                            key={region.id}
                            className="absolute overflow-hidden rounded-sm border-2"
                            style={{
                              left: `${box.leftPct}%`,
                              top: `${box.topPct}%`,
                              width: `${box.widthPct}%`,
                              height: `${box.heightPct}%`,
                              borderColor: config.color,
                              backgroundColor: hexToRgba(config.color, 0.12),
                              boxShadow: `0 0 0 1px ${hexToRgba(config.color, 0.25)}`,
                            }}
                          >
                            <div
                              className="absolute left-0 top-0 max-w-full truncate rounded-br-md px-1.5 py-0.5 text-[10px] font-semibold leading-4 text-white shadow-sm"
                              style={{ backgroundColor: config.color }}
                              title={`${config.label} (${region.id}) - ${formatScore(region.confidence, 2)}`}
                            >
                              {config.label} {formatScore(region.confidence, 2)}
                            </div>
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
                  the persisted canonical coordinates.
                </span>
              </div>
            </div>

            <div className="space-y-4">
              <Panel title="Decision Panel">
                <Metric label="Layout source" value={sourceLabel(source)} />
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
      return "Fallback (unverified)";
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

function getBoxStyle(region: LayoutRegion, naturalSize: NaturalSize) {
  const leftPct = clampPct((region.bbox.x_min / naturalSize.width) * 100);
  const rightPct = clampPct((region.bbox.x_max / naturalSize.width) * 100);
  const topPct = clampPct((region.bbox.y_min / naturalSize.height) * 100);
  const bottomPct = clampPct((region.bbox.y_max / naturalSize.height) * 100);
  const widthPct = Math.max(0, rightPct - leftPct);
  const heightPct = Math.max(0, bottomPct - topPct);

  if (widthPct <= 0 || heightPct <= 0) return null;

  return {
    leftPct,
    topPct,
    widthPct,
    heightPct,
  };
}

function clampPct(value: number): number {
  if (Number.isNaN(value) || !Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, value));
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
