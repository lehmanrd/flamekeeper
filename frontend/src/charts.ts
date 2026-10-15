/** Pure helpers for the SVG charts (no chart library). */

export interface XY {
  t: number; // ms since epoch
  v: number | null;
}

/** A round upper bound for an axis: 1, 2, 2.5, 5 × 10^n. */
export function niceMax(max: number): number {
  if (!Number.isFinite(max) || max <= 0) return 1;
  const exp = Math.floor(Math.log10(max));
  const base = 10 ** exp;
  for (const m of [1, 2, 2.5, 5, 10]) if (max <= m * base) return m * base;
  return 10 * base;
}

/**
 * SVG path for a series scaled into width × height. Gaps (null values, or time gaps
 * longer than `gapMs`) break the line so a stopped server doesn't look idle.
 */
export function linePath(points: XY[], t0: number, t1: number, yMax: number, width: number, height: number, gapMs = Infinity): string {
  const span = Math.max(1, t1 - t0);
  let d = "";
  let prev: XY | null = null;
  for (const p of points) {
    if (p.v === null) {
      prev = null;
      continue;
    }
    const x = ((p.t - t0) / span) * width;
    const y = height - (Math.min(p.v, yMax) / yMax) * height;
    const move = prev === null || p.t - prev.t > gapMs;
    d += `${move ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    prev = p;
  }
  return d;
}

export function formatRate(bps: number | null | undefined): string {
  if (bps == null) return "—";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let v = bps;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function formatMb(mb: number | null | undefined): string {
  if (mb == null) return "—";
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}
