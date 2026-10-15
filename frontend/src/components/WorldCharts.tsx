import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { MetricPoint, MetricsRange, WorldSummary } from "../api/types";
import { formatMb, formatRate, type XY } from "../charts";
import { LineChart } from "./Chart";

const RANGE_MS: Record<MetricsRange, number> = { "1h": 3_600_000, "24h": 86_400_000, "7d": 604_800_000 };

const series = (points: MetricPoint[], key: keyof MetricPoint): XY[] =>
  points.map((p) => ({ t: new Date(p.ts).getTime(), v: (p[key] as number | null) ?? null }));

/** CPU, memory, players and network for one world. */
export function WorldCharts({ world }: { world: WorldSummary }) {
  const [range, setRange] = useState<MetricsRange>("1h");
  const { data } = useQuery({
    queryKey: ["metrics", world.id, range],
    queryFn: () => api.metrics(world.id, range),
    refetchInterval: 60_000,
  });
  const t1 = Date.now();
  const t0 = t1 - RANGE_MS[range];
  const points = data?.points ?? [];
  const gap = (data?.step_s ?? 60) * 2500;
  const memLimit = world.metrics?.mem_limit_mb;

  return (
    <div className="card">
      <div className="top" style={{ margin: "0 0 6px" }}>
        <h3 style={{ margin: 0 }}>Resources</h3>
        <span className="spacer" />
        <div className="seg" role="group" aria-label="Time range">
          {(["1h", "24h", "7d"] as MetricsRange[]).map((r) => (
            <button key={r} className={r === range ? "on" : ""} aria-pressed={r === range} onClick={() => setRange(r)}>{r}</button>
          ))}
        </div>
      </div>
      <div className="charts">
        <LineChart title="CPU" t0={t0} t1={t1} gapMs={gap}
          series={[{ label: "CPU", color: "var(--gold)", points: series(points, "cpu_pct"), format: (v) => (v == null ? "—" : `${Math.round(v)}%`) }]} />
        <LineChart title={`Memory${memLimit && memLimit < 1e6 ? ` (limit ${formatMb(memLimit)})` : ""}`} t0={t0} t1={t1} gapMs={gap}
          series={[{ label: "Memory", color: "var(--info)", points: series(points, "mem_mb"), format: formatMb }]} />
        <LineChart title="Players" t0={t0} t1={t1} gapMs={gap} yMax={Math.max(world.max_players ?? 16, 1)}
          series={[{ label: "Players", color: "var(--ok)", points: series(points, "players"), format: (v) => (v == null ? "—" : String(Math.round(v))) }]} />
        <LineChart title="Network" t0={t0} t1={t1} gapMs={gap}
          series={[
            { label: "in", color: "var(--info)", points: series(points, "net_rx_bps"), format: formatRate },
            { label: "out", color: "var(--warn)", points: series(points, "net_tx_bps"), format: formatRate },
          ]} />
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>CPU: 100% = one core. Sampled every minute; kept 7 days.</p>
    </div>
  );
}
