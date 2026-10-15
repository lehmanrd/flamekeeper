import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { WorldSummary } from "../api/types";
import { StatusPill } from "../components/StatusPill";
import { WorldActions } from "../components/WorldActions";
import { formatMb } from "../charts";
import { Sparkline } from "../components/Chart";
import { formatBytes, isRunningState, since } from "../format";
import { useWorlds } from "../hooks/live";

export function Overview() {
  const { data: worlds, isLoading, error } = useWorlds();
  const { data: system } = useQuery({ queryKey: ["system"], queryFn: api.system, staleTime: 60_000 });
  const { data: host } = useQuery({ queryKey: ["host"], queryFn: api.host, refetchInterval: 15_000 });
  const { data: cap } = useQuery({ queryKey: ["capacity", 0, worlds?.length], queryFn: () => api.capacity(0), staleTime: 60_000 });

  const players = (worlds ?? []).reduce((n, w) => n + (w.players ?? 0), 0);
  const slots = (worlds ?? []).reduce((n, w) => n + (w.max_players ?? 0), 0);
  const diskUsedPct =
    host?.disk_total && host.disk_free != null ? 100 - (host.disk_free * 100) / host.disk_total : null;
  const memPct = host?.mem_total && host.mem_used != null ? (host.mem_used * 100) / host.mem_total : null;

  return (
    <>
      <div className="top">
        <h1>Worlds</h1>
        <span className="spacer" />
        <Link className="btn primary" to="/new">＋ New world</Link>
      </div>

      <div className="grid g4" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <div className="v">{host?.cpu_pct != null ? `${Math.round(host.cpu_pct)}%` : "—"}</div>
          <div className="l">Host CPU · {host?.cpus ?? "?"} cores</div>
          {host?.cpu_pct != null && <div className={`bar${host.cpu_pct > 90 ? " warn" : ""}`}><i style={{ width: `${host.cpu_pct}%` }} /></div>}
        </div>
        <div className="card stat">
          <div className="v">{formatBytes(host?.mem_used)} / {formatBytes(host?.mem_total)}</div>
          <div className="l">Host RAM (16 GB recommended per world)</div>
          {memPct != null && <div className={`bar${memPct > 90 ? " warn" : ""}`}><i style={{ width: `${memPct}%` }} /></div>}
        </div>
        <div className="card stat">
          <div className="v">{formatBytes(host?.disk_free)} free</div>
          <div className="l mono">{system?.data_dir ?? "Data disk"}</div>
          {diskUsedPct != null && (
            <div className={`bar${diskUsedPct > 90 ? " warn" : ""}`}>
              <i style={{ width: `${diskUsedPct}%` }} />
            </div>
          )}
        </div>
        <div className="card stat">
          <div className="v">
            {players}
            {slots ? ` / ${slots}` : ""}
          </div>
          <div className="l">
            Players online · {worlds?.length ?? 0} world{worlds?.length === 1 ? "" : "s"}
          </div>
        </div>
      </div>

      {cap?.warnings.map((w) => <div key={w} className="banner warn">⚠ {w}</div>)}
      {error && <div className="banner bad">Could not load worlds: {(error as Error).message}</div>}
      {isLoading && <p className="muted">Loading…</p>}
      {worlds && worlds.length === 0 && (
        <div className="card empty">
          <h2>No worlds yet</h2>
          <p>Create a world to download and run an Enshrouded dedicated server.</p>
          <Link className="btn primary" to="/new">＋ Create your first world</Link>
        </div>
      )}
      <div className="grid g3">
        {worlds?.map((w) => <WorldCard key={w.id} world={w} />)}
      </div>
    </>
  );
}

function WorldCard({ world }: { world: WorldSummary }) {
  const running = isRunningState(world.state);
  const { data: metrics } = useQuery({
    queryKey: ["metrics", world.id, "1h"],
    queryFn: () => api.metrics(world.id, "1h"),
    refetchInterval: 60_000,
    enabled: running,
  });
  const cpu = (metrics?.points ?? []).map((p) => ({ t: new Date(p.ts).getTime(), v: p.cpu_pct }));
  const m = world.metrics;
  return (
    <div className="card">
      <div className="top" style={{ margin: "0 0 8px" }}>
        <h2>
          <Link to={`/worlds/${world.slug}`} style={{ color: "inherit", textDecoration: "none" }}>
            {world.display_name}
          </Link>
        </h2>
        <span className="spacer" />
        {(world.game_update_available || world.image_update_available) && (
          <Link className="pill warn" to={`/worlds/${world.slug}/updates`} style={{ textDecoration: "none" }}>Update available</Link>
        )}
        <StatusPill world={world} />
      </div>
      <div className="muted mono">
        :{world.query_port}/udp{world.build_id ? ` · build ${world.build_id}` : ""}
        {world.game_update_available && world.latest_build ? ` → ${world.latest_build}` : ""}
      </div>
      {running ? <Sparkline points={cpu} /> : <div style={{ height: 10 }} />}
      {world.update_progress != null && (
        <div className="bar" style={{ marginBottom: 10 }}>
          <i style={{ width: `${world.update_progress}%` }} />
        </div>
      )}
      <dl className="kv">
        <dt>Players</dt>
        <dd>
          {world.players != null
            ? `${world.players} / ${world.max_players ?? "?"}${
                world.player_names.length ? ` (${world.player_names.join(", ")})` : ""
              }`
            : "—"}
        </dd>
        <dt>CPU / RAM</dt>
        <dd>{running && m ? `${m.cpu_pct != null ? Math.round(m.cpu_pct) : "—"}% · ${formatMb(m.mem_mb)}` : "—"}</dd>
        <dt>Uptime</dt>
        <dd>{world.state === "online" ? since(world.online_since) : "—"}</dd>
        <dt>Image</dt>
        <dd className="mono">{world.image.replace("mornedhels/enshrouded-server", "mornedhels")}</dd>
      </dl>
      {world.last_failure && (
        <div className="banner bad" style={{ margin: "12px 0 0" }}>
          {world.last_failure.title}
        </div>
      )}
      <div className="top" style={{ margin: "12px 0 0" }}>
        <Link className="btn sm" to={`/worlds/${world.slug}`}>Open</Link>
        <WorldActions world={world} size="sm" />
      </div>
    </div>
  );
}
