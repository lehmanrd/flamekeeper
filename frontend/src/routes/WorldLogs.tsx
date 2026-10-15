import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { apiUrl } from "../api/client";
import type { WorldSummary } from "../api/types";
import { filterLines, type LevelFilter } from "../format";
import { useLogStream } from "../hooks/live";
import styles from "./WorldLogs.module.css";
import { NotFound } from "./NotFound";
import { useWorldBySlug } from "./WorldOverview";

export function WorldLogs() {
  const { world, loading } = useWorldBySlug();
  if (loading) return <p className="muted">Loading…</p>;
  if (!world) return <NotFound what="World" />;
  return <LogsView key={world.id} world={world} />;
}

function LogsView({ world }: { world: WorldSummary }) {
  const [paused, setPaused] = useState(false);
  const [query, setQuery] = useState("");
  const [level, setLevel] = useState<LevelFilter>("all");
  const { lines, status } = useLogStream(world.id, paused);
  const shown = useMemo(() => filterLines(lines, query, level), [lines, query, level]);
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useLayoutEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [shown.length]);
  useEffect(() => {
    stick.current = true;
  }, [query, level]);

  const statusPill = paused ? (
    <span className="pill warn">Paused</span>
  ) : status === "live" ? (
    <span className="pill ok pulse">Live</span>
  ) : status === "ended" ? (
    <span className="pill off">Container stopped</span>
  ) : status === "error" ? (
    <span className="pill bad">Disconnected</span>
  ) : (
    <span className="pill info">Connecting</span>
  );

  return (
    <>
      <div className="top">
        <h1>Logs</h1>
        <span className="muted">{world.display_name}</span>
        {statusPill}
        <span className="spacer" />
        <input type="search" placeholder="Search…" value={query} onChange={(e) => setQuery(e.target.value)}
          style={{ width: 200 }} aria-label="Search logs" />
        <select value={level} onChange={(e) => setLevel(e.target.value as LevelFilter)} aria-label="Filter">
          <option value="all">All levels</option>
          <option value="warn">Warnings and errors</option>
          <option value="error">Errors</option>
          <option value="players">Player events</option>
        </select>
        <button className="btn" onClick={() => setPaused(!paused)}>{paused ? "Resume" : "Pause"}</button>
        <a className="btn" href={apiUrl(`/worlds/${world.id}/logs/download?tail=5000`)}>Download</a>
      </div>

      {world.last_failure && (
        <div className="banner bad">
          <div>
            <b>Known problem: {world.last_failure.title}</b>
            <div>{world.last_failure.hint}</div>
          </div>
        </div>
      )}

      <div
        ref={box}
        className={styles.log}
        role="log"
        aria-live="off"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {shown.length === 0 ? (
          <div className="muted">
            {status === "connecting" ? "Connecting…" : lines.length ? "No lines match." : "No log output yet."}
          </div>
        ) : (
          shown.map((l, i) => (
            <div key={i} className={styles[l.level] + (l.player ? ` ${styles.player}` : "")}>
              <span className={styles.ts}>{l.ts ? l.ts.slice(11, 19) : ""}</span>{" "}
              <span className={styles.src}>{l.source.replace("enshrouded-", "")}</span> {l.text}
            </div>
          ))
        )}
      </div>
      <div className="note">
        Showing {shown.length} of {lines.length} lines (up to 5,000 kept). Passwords are masked.
      </div>
    </>
  );
}
