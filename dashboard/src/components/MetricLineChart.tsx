import { useRef, useState } from "react";
import { cn } from "@/lib/utils";

export interface MetricPoint {
  x: number;
  y: number;
}

export interface MetricLineChartProps {
  /** Names the single series plotted — doubles as the chart's only legend
   * (per the dataviz skill: a single series needs no legend box). */
  title: string;
  points: MetricPoint[];
  /** Format a Y value for the axis ticks, the end-label, and the tooltip. */
  formatY: (y: number) => string;
  /** Format an X value (epoch number) for the tooltip. */
  formatX?: (x: number) => string;
  className?: string;
}

const WIDTH = 400;
const HEIGHT = 160;
const PAD_LEFT = 44;
const PAD_RIGHT = 12;
const PAD_TOP = 12;
const PAD_BOTTOM = 24;
const PLOT_W = WIDTH - PAD_LEFT - PAD_RIGHT;
const PLOT_H = HEIGHT - PAD_TOP - PAD_BOTTOM;

/** A restrained single-series line chart (dark instrument-panel tokens):
 * 2px accent line, an 8px end-marker with a surface ring, hairline recessive
 * gridlines, and a crosshair+tooltip that snaps to the nearest real point.
 * Renders an honest "no data yet" placeholder instead of a broken/empty plot
 * when there are no points. */
export function MetricLineChart({
  title,
  points,
  formatY,
  formatX,
  className,
}: MetricLineChartProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  if (points.length === 0) {
    return (
      <div
        className={cn(
          "rounded-md border border-hairline bg-panel p-4",
          className,
        )}
      >
        <h3 className="text-sm font-semibold text-primary">{title}</h3>
        <div className="flex h-40 items-center justify-center text-sm text-tertiary">
          No data reported yet
        </div>
      </div>
    );
  }

  const ys = points.map((p) => p.y);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  // Guard the degenerate case (one point, or every value identical) so the
  // scale never divides by zero — the line still renders as flat, not broken.
  const yRange = yMax - yMin || 1;
  const xMin = points[0].x;
  const xMax = points[points.length - 1].x;
  const xRange = xMax - xMin || 1;

  const scaleX = (x: number) => PAD_LEFT + ((x - xMin) / xRange) * PLOT_W;
  const scaleY = (y: number) => PAD_TOP + PLOT_H - ((y - yMin) / yRange) * PLOT_H;

  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${scaleX(p.x).toFixed(2)},${scaleY(p.y).toFixed(2)}`)
    .join(" ");

  const last = points[points.length - 1];
  const yTicks = [yMax, (yMax + yMin) / 2, yMin];

  function handleMove(e: React.PointerEvent<SVGSVGElement>) {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const relX = (e.clientX - rect.left) / rect.width;
    const idx = Math.round(relX * (points.length - 1));
    setHoverIndex(Math.min(points.length - 1, Math.max(0, idx)));
  }

  const hovered = hoverIndex !== null ? points[hoverIndex] : null;

  function handleKeyDown(e: React.KeyboardEvent<SVGSVGElement>) {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setHoverIndex((i) => Math.max(0, (i ?? points.length - 1) - 1));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setHoverIndex((i) => Math.min(points.length - 1, (i ?? 0) + 1));
    } else if (e.key === "Escape") {
      setHoverIndex(null);
    }
  }

  return (
    <div className={cn("rounded-md border border-hairline bg-panel p-4", className)}>
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-primary">{title}</h3>
        <span className="font-data text-sm text-primary">{formatY(last.y)}</span>
      </div>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-40 w-full touch-none rounded outline-none focus-visible:ring-2 focus-visible:ring-accent"
        role="img"
        tabIndex={0}
        aria-label={`${title}: ${points.length} points, latest ${formatY(last.y)}. Use arrow keys to inspect values.`}
        onPointerMove={handleMove}
        onPointerLeave={() => setHoverIndex(null)}
        onFocus={() => setHoverIndex((i) => i ?? points.length - 1)}
        onBlur={() => setHoverIndex(null)}
        onKeyDown={handleKeyDown}
      >
        {/* Recessive hairline gridlines + monospace axis ticks */}
        {yTicks.map((t, i) => (
          <g key={i}>
            <line
              x1={PAD_LEFT}
              x2={WIDTH - PAD_RIGHT}
              y1={scaleY(t)}
              y2={scaleY(t)}
              stroke="var(--border-hairline)"
              strokeWidth={1}
            />
            <text
              x={PAD_LEFT - 6}
              y={scaleY(t)}
              textAnchor="end"
              dominantBaseline="middle"
              className="font-data"
              fontSize={9}
              fill="var(--text-tertiary)"
            >
              {formatY(t)}
            </text>
          </g>
        ))}

        {/* The line itself: 2px, round join/cap, one restrained accent hue */}
        <path
          d={path}
          fill="none"
          stroke="var(--accent)"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {/* End marker: >=8px, filled with the series color, 2px surface ring */}
        <circle cx={scaleX(last.x)} cy={scaleY(last.y)} r={6} fill="var(--bg-panel)" />
        <circle cx={scaleX(last.x)} cy={scaleY(last.y)} r={4} fill="var(--accent)" />

        {/* Crosshair + hovered-point marker */}
        {hovered && (
          <>
            <line
              x1={scaleX(hovered.x)}
              x2={scaleX(hovered.x)}
              y1={PAD_TOP}
              y2={PAD_TOP + PLOT_H}
              stroke="var(--text-tertiary)"
              strokeWidth={1}
              strokeDasharray="2,2"
            />
            <circle cx={scaleX(hovered.x)} cy={scaleY(hovered.y)} r={5} fill="var(--bg-panel)" />
            <circle cx={scaleX(hovered.x)} cy={scaleY(hovered.y)} r={3.5} fill="var(--accent)" />
          </>
        )}
      </svg>
      <div aria-live="polite" className="mt-1 h-5 font-data text-xs text-secondary">
        {hovered
          ? `${formatX ? formatX(hovered.x) : `x=${hovered.x}`} — ${formatY(hovered.y)}`
          : `Hover or focus the chart for exact values (${points.length} point${points.length === 1 ? "" : "s"} total).`}
      </div>
    </div>
  );
}
