import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { WorldEvent, WorldSummary } from "../api/types";
import { formatDuration, formatTime, since } from "../format";
import { NotFound } from "./NotFound";
import { EVENT_TONE, eventDetail, useWorldBySlug } from "./WorldOverview";

const FILTERS: [string, string[]][] = [
  ["All", []],
  ["Players", ["player_joined", "player_left"]],
  ["Server", ["started", "stopped", "online", "crashed", "created", "adopted"]],
  ["Config", ["config_saved", "config_applied"]],
  ["Backups", ["backup_created", "restored"]],
  ["Updates", ["update_available", "updated"]],
  ["Warnings", ["warning", "crashed"]],
];

export function WorldPlayers() {
  const { world, loading } = useWorldBySlug();
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  return <Players key={world.id} world={world} />;
}

function Players({ world }: { world: WorldSummary }) {
  const { data: history } = useQuery({
    queryKey: ["players-history", world.id, world.players],
    queryFn: () => api.playersHistory(world.id),
    refetchInterval: 60_000,
  });
  const online = history?.players.filter((p) => p.online) ?? [];

  return (
    <>
      <div className="top">
        <h1>Players &amp; activity</h1>
        <span className="muted">{world.display_name}</span>
      </div>
      <div className="grid g2">
        <div className="card">
          <h3>Players</h3>
          {!history ? (
            <p className="muted">Loading…</p>
          ) : history.players.length === 0 ? (
            <p className="muted">Nobody has joined yet.</p>
          ) : (
            <table>
              <thead>
                <tr><th>Player</th><th>Sessions</th><th>Playtime</th><th>Last seen</th></tr>
              </thead>
              <tbody>
                {history.players.map((p) => (
                  <tr key={p.player}>
                    <td>{p.online && <span className="pill ok" style={{ marginRight: 6 }}>online</span>}{p.player}</td>
                    <td>{p.sessions}</td>
                    <td>{formatDuration(p.playtime_s * 1000)}</td>
                    <td className="muted">{p.online ? "now" : formatTime(p.last_seen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3 style={{ marginTop: 18 }}>Recent sessions</h3>
          {history && history.sessions.length > 0 ? (
            <table>
              <tbody>
                {history.sessions.slice(0, 25).map((s, i) => (
                  <tr key={i}>
                    <td>{s.player}</td>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>{formatTime(s.joined_at)}</td>
                    <td>{s.left_at ? formatDuration(s.duration_s * 1000) : <span className="pill ok">{since(s.joined_at)} so far</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">No sessions yet.</p>
          )}
          <p className="muted" style={{ fontSize: 12 }}>
            {online.length ? `${online.length} online now. ` : ""}Sessions come from the server's join and leave log lines.
          </p>
        </div>
        <Timeline world={world} />
      </div>
    </>
  );
}

function Timeline({ world }: { world: WorldSummary }) {
  const [filter, setFilter] = useState(0);
  const kinds = FILTERS[filter][1];
  const q = useInfiniteQuery({
    queryKey: ["timeline", world.id, filter],
    queryFn: ({ pageParam }) => api.events(world.id, 50, kinds, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last: WorldEvent[]) => (last.length === 50 ? last[last.length - 1].id : undefined),
    refetchInterval: 30_000,
  });
  const events = q.data?.pages.flat() ?? [];
  return (
    <div className="card">
      <div className="top" style={{ margin: "0 0 10px" }}>
        <h3 style={{ margin: 0 }}>Timeline</h3>
        <span className="spacer" />
        <select value={filter} onChange={(e) => setFilter(Number(e.target.value))} aria-label="Filter events">
          {FILTERS.map(([label], i) => <option key={label} value={i}>{label}</option>)}
        </select>
      </div>
      {events.length === 0 ? (
        <p className="muted">{q.isLoading ? "Loading…" : "No events."}</p>
      ) : (
        <ol className="timeline">
          {events.map((e) => (
            <li key={e.id}>
              <span className="muted when">{formatTime(e.ts)}</span>
              <span className={`pill ${EVENT_TONE[e.kind] ?? "info"}`}>{e.kind.replaceAll("_", " ")}</span>
              <span className="what">{eventDetail(e)}</span>
            </li>
          ))}
        </ol>
      )}
      {q.hasNextPage && (
        <button className="btn sm" disabled={q.isFetchingNextPage} onClick={() => q.fetchNextPage()} style={{ marginTop: 10 }}>
          Older events
        </button>
      )}
    </div>
  );
}
