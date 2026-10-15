import { useMemo, useState } from "react";
import { linePath, niceMax, type XY } from "../charts";
import { formatTime } from "../format";

export interface Series {
  label: string;
  color: string;
  points: XY[];
  format: (v: number | null) => string;
}

const W = 600;
const H = 140;

/** A small time-series chart: one or two lines, a y-axis max, hover read-out. */
export function LineChart({
  title,
  series,
  t0,
  t1,
  yMax,
  gapMs,
}: {
  title: string;
  series: Series[];
  t0: number;
  t1: number;
  yMax?: number;
  gapMs?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const max = useMemo(() => {
    if (yMax) return yMax;
    const vals = series.flatMap((s) => s.points.map((p) => p.v ?? 0));
    return niceMax(Math.max(0, ...vals));
  }, [series, yMax]);
  const empty = series.every((s) => s.points.every((p) => p.v === null));
  const at = hover === null ? null : t0 + (hover / W) * (t1 - t0);
  const nearest = (s: Series) => {
    if (at === null || !s.points.length) return null;
    return s.points.reduce((a, b) => (Math.abs(b.t - at) < Math.abs(a.t - at) ? b : a));
  };
  const latest = (s: Series) => [...s.points].reverse().find((p) => p.v !== null) ?? null;

  return (
    <figure className="chart">
      <figcaption>
        <span>{title}</span>
        <span className="legend-items">
          {series.map((s) => {
            const p = at !== null ? nearest(s) : latest(s);
            return (
              <span key={s.label}>
                <i style={{ background: s.color }} /> {series.length > 1 ? `${s.label} ` : ""}
                <b>{s.format(p?.v ?? null)}</b>
              </span>
            );
          })}
          {at !== null && <span className="muted">{formatTime(new Date(at).toISOString())}</span>}
        </span>
      </figcaption>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${title} chart`}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          setHover(((e.clientX - r.left) / r.width) * W);
        }}
        onMouseLeave={() => setHover(null)}
      >
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={0} x2={W} y1={H * f} y2={H * f} className="grid-line" />
        ))}
        {series.map((s) => (
          <path key={s.label} d={linePath(s.points, t0, t1, max, W, H, gapMs)} fill="none" stroke={s.color} strokeWidth={1.6} vectorEffect="non-scaling-stroke" />
        ))}
        {hover !== null && <line x1={hover} x2={hover} y1={0} y2={H} className="cursor" />}
      </svg>
      <div className="axis">
        <span>{formatTime(new Date(t0).toISOString())}</span>
        <span>max {series[0].format(max)}</span>
        <span>now</span>
      </div>
      {empty && <div className="chart-empty muted">No data yet: samples are taken every minute while the server runs.</div>}
    </figure>
  );
}

/** Tiny line for world cards. */
export function Sparkline({ points, color = "var(--gold)", max }: { points: XY[]; color?: string; max?: number }) {
  if (!points.length) return <svg className="spark" viewBox="0 0 100 30" aria-hidden="true" />;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const m = max ?? niceMax(Math.max(1, ...points.map((p) => p.v ?? 0)));
  return (
    <svg className="spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
      <path d={linePath(points, t0, t1, m, 100, 28, 5 * 60_000)} fill="none" stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
