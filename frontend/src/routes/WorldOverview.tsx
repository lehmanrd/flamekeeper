import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { WorldEvent, WorldSummary } from "../api/types";
import { useJobFocus } from "../components/JobsDrawer";
import { CloneDialog } from "../components/CloneDialog";
import { ConfigBanners } from "../components/ConfigBanners";
import { WorldCharts } from "../components/WorldCharts";
import { Modal } from "../components/Modal";
import { StatusPill } from "../components/StatusPill";
import { WorldActions } from "../components/WorldActions";
import { formatTime, since } from "../format";
import { useWorlds } from "../hooks/live";
import { NotFound } from "./NotFound";

export function useWorldBySlug(): { world: WorldSummary | undefined; loading: boolean } {
  const { slug } = useParams();
  const { data: worlds, isLoading } = useWorlds();
  return { world: worlds?.find((w) => w.slug === slug), loading: isLoading };
}

export const EVENT_TONE: Record<string, string> = {
  online: "ok",
  started: "info",
  stopped: "off",
  crashed: "bad",
  warning: "warn",
  created: "info",
  adopted: "info",
  player_joined: "ok",
  player_left: "off",
  backup_created: "ok",
  restored: "warn",
  config_saved: "info",
  config_applied: "ok",
  update_available: "warn",
  updated: "ok",
};

export function eventDetail(e: WorldEvent): string {
  const d = e.data as Record<string, string | number | undefined>;
  if (e.kind === "updated") {
    if (d.target === "image") return `image ${d.image ?? ""}`;
    return d.from_build === d.to_build ? `game already at ${d.to_build}` : `game ${d.from_build ?? "?"} → ${d.to_build ?? "?"}`;
  }
  if (e.kind === "update_available") return `build ${d.build}`;
  if (d.player) return `${d.player}${d.reason ? ` (${d.reason})` : ""}`;
  if (d.message) return String(d.message);
  if (d.mode) return `${d.mode} restart`;
  if (d.port) return `port ${d.port}`;
  if (d.filename) return String(d.filename);
  return "";
}

export function WorldOverview() {
  const { world, loading } = useWorldBySlug();
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  return <WorldOverviewInner world={world} />;
}

function WorldOverviewInner({ world }: { world: WorldSummary }) {
  const { data: detail } = useQuery({ queryKey: ["world", world.id], queryFn: () => api.world(world.id) });
  const { data: events = [] } = useQuery({
    queryKey: ["events", world.id],
    queryFn: () => api.events(world.id, 15),
    refetchInterval: 30_000,
  });
  const { data: players } = useQuery({
    queryKey: ["players", world.id, world.players],
    queryFn: () => api.players(world.id),
  });

  return (
    <>
      <div className="top">
        <h1>{world.display_name}</h1>
        <StatusPill world={world} />
        <span className="muted mono">
          {world.container_name} · :{world.query_port}/udp
        </span>
        <span className="spacer" />
        <WorldActions world={world} />
      </div>

      {world.last_failure && (
        <div className="banner bad" role="alert">
          <div>
            <b>{world.last_failure.title}</b>
            <div>{world.last_failure.hint}</div>
          </div>
          <span className="spacer" />
          <Link className="btn sm" to={`/worlds/${world.slug}/logs`}>View logs</Link>
        </div>
      )}
      <ConfigBanners world={world} />
      {(world.game_update_available || world.image_update_available) && (
        <div className="banner warn">
          <span>
            ⟳{" "}
            {world.game_update_available
              ? `Game update available: build ${world.build_id} → ${world.latest_build}.`
              : "A new container image is available."}
          </span>
          <span className="spacer" />
          <Link className="btn sm" to={`/worlds/${world.slug}/updates`}>Updates</Link>
        </div>
      )}
      {world.state === "missing" && (
        <div className="banner warn">
          The game container is missing. Start the world to recreate it; the game files and saves are kept.
        </div>
      )}
      {world.state === "updating" && world.update_progress != null && (
        <div className="banner info">
          Downloading game files: {Math.floor(world.update_progress)}%
          <span className="spacer" />
          <div className="bar" style={{ width: 200 }}>
            <i style={{ width: `${world.update_progress}%` }} />
          </div>
        </div>
      )}

      <div className="grid g4" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <div className="v">
            {world.players ?? "—"} / {world.max_players ?? "—"}
          </div>
          <div className="l">Players</div>
        </div>
        <div className="card stat">
          <div className="v">{world.state === "online" ? since(world.online_since) : "—"}</div>
          <div className="l">Uptime</div>
        </div>
        <div className="card stat">
          <div className="v mono" style={{ fontSize: 18 }}>{world.build_id ?? "—"}</div>
          <div className="l">Game build</div>
        </div>
        <div className="card stat">
          <div className="v">{world.query_port}</div>
          <div className="l">Query port (UDP)</div>
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <WorldCharts world={world} />
      </div>

      <div className="grid g2">
        <div className="card">
          <h3>Recent events <Link className="small" to={`/worlds/${world.slug}/players`} style={{ marginLeft: 6 }}>all</Link></h3>
          {events.length === 0 ? (
            <p className="muted">No events yet.</p>
          ) : (
            <table>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>{formatTime(e.ts)}</td>
                    <td><span className={`pill ${EVENT_TONE[e.kind] ?? "info"}`}>{e.kind.replaceAll("_", " ")}</span></td>
                    <td>{eventDetail(e)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="card">
          <h3>Online now</h3>
          {!players || !players.count ? (
            <p className="muted">{world.state === "online" ? "Nobody is online." : "Server is not online."}</p>
          ) : players.online.length ? (
            <table>
              <thead><tr><th>Player</th><th>Session</th></tr></thead>
              <tbody>
                {players.online.map((p) => (
                  <tr key={p.name + p.joined_at}><td>{p.name}</td><td>{since(p.joined_at)}</td></tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p>
              {players.count} player{players.count === 1 ? "" : "s"} online
              {players.a2s_names.length ? `: ${players.a2s_names.join(", ")}` : ""}
            </p>
          )}
        </div>
      </div>

      <div className="grid g2" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>At a glance</h3>
          <dl className="kv">
            <dt>Image</dt><dd className="mono">{world.image}</dd>
            <dt>Container</dt><dd className="mono">{world.container_name} ({world.container_status ?? "none"})</dd>
            <dt>Data</dt><dd className="mono">{detail?.data_dir ?? "…"}</dd>
            <dt>Run as</dt><dd className="mono">{detail ? `${detail.infra.puid}:${detail.infra.pgid}` : "…"}</dd>
            <dt>Steam branch</dt><dd>{detail?.infra.game_branch ?? "…"}</dd>
            <dt>Server version</dt><dd className="mono">{world.server_version ?? "—"}</dd>
            <dt>Created</dt><dd>{detail ? formatTime(detail.created_at) : "…"}</dd>
          </dl>
        </div>
        <DangerZone world={world} />
      </div>
    </>
  );
}

function DangerZone({ world }: { world: WorldSummary }) {
  const navigate = useNavigate();
  const focusJob = useJobFocus();
  const [open, setOpen] = useState<null | "delete" | "recreate" | "clone">(null);
  const [confirm, setConfirm] = useState("");
  const [deleteData, setDeleteData] = useState(false);

  const act = useMutation({
    mutationFn: (fn: () => Promise<{ job_id: string }>) => fn(),
    onSuccess: (res) => {
      focusJob(res.job_id);
      if (open === "delete") navigate("/");
      setOpen(null);
    },
  });

  return (
    <div className="card">
      <h3>Maintenance</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Recreate rebuilds the container from its settings. Game files and saves are kept.
      </p>
      <div className="top" style={{ margin: 0 }}>
        <button className="btn" disabled={world.busy} onClick={() => setOpen("recreate")}>Recreate container</button>
        <button className="btn" onClick={() => setOpen("clone")}>Clone world…</button>
        <button className="btn danger" disabled={world.busy} onClick={() => { setConfirm(""); setDeleteData(false); setOpen("delete"); }}>
          Delete world…
        </button>
      </div>

      <CloneDialog world={world} source="current" sourceLabel="its current state" open={open === "clone"} onClose={() => setOpen(null)} />
      <Modal
        open={open === "recreate"}
        onClose={() => setOpen(null)}
        title="Recreate container?"
        actions={
          <>
            <button className="btn" onClick={() => setOpen(null)}>Cancel</button>
            <button className="btn primary" disabled={act.isPending} onClick={() => act.mutate(() => api.recreate(world.id))}>
              Recreate
            </button>
          </>
        }
      >
        <p>The server stops, its container is removed and created again, then it starts. Players are disconnected.</p>
        {act.error && <p className="error-text">{(act.error as Error).message}</p>}
      </Modal>

      <Modal
        open={open === "delete"}
        onClose={() => setOpen(null)}
        title={`Delete ${world.display_name}?`}
        actions={
          <>
            <button className="btn" onClick={() => setOpen(null)}>Cancel</button>
            <button
              className="btn primary"
              style={{ background: "var(--bad)", borderColor: "var(--bad)" }}
              disabled={confirm !== world.slug || act.isPending}
              onClick={() => act.mutate(() => api.deleteWorld(world.id, deleteData))}
            >
              Delete world
            </button>
          </>
        }
      >
        <p>The server is stopped and its container removed.</p>
        <div className="radio-row">
          <label>
            <input type="radio" checked={!deleteData} onChange={() => setDeleteData(false)} />
            <span>Keep the data folder on disk (saves, config, backups)</span>
          </label>
          <label>
            <input type="radio" checked={deleteData} onChange={() => setDeleteData(true)} />
            <span style={{ color: "var(--bad)" }}>Delete all data, including saves and backups. This cannot be undone.</span>
          </label>
        </div>
        <p>
          Type <b className="mono">{world.slug}</b> to confirm:
        </p>
        <input value={confirm} onChange={(e) => setConfirm(e.target.value)} className="mono" style={{ width: "100%" }}
          aria-label="World slug" autoComplete="off" />
        {act.error && <p className="error-text">{(act.error as Error).message}</p>}
      </Modal>
    </div>
  );
}
