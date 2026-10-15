import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { Job } from "../api/types";
import { JOB_LABEL, formatTime } from "../format";
import { useJobStream, useJobs, useWorlds } from "../hooks/live";
import { Modal } from "./Modal";
import styles from "./JobsDrawer.module.css";

const RECENT_MS = 10 * 60_000;
const FocusCtx = createContext<(jobId: string) => void>(() => {});

/** Open the drawer and the detail view for a job (after starting an action). */
export function useJobFocus() {
  return useContext(FocusCtx);
}

export function JobsProvider({ children }: { children: ReactNode }) {
  const [focused, setFocused] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const focus = (id: string) => {
    setExpanded(true);
    setFocused(id);
  };
  return (
    <FocusCtx.Provider value={focus}>
      {children}
      <JobsDrawer expanded={expanded} setExpanded={setExpanded} onOpen={setFocused} />
      <JobDialog jobId={focused} onClose={() => setFocused(null)} />
    </FocusCtx.Provider>
  );
}

function JobsDrawer({
  expanded,
  setExpanded,
  onOpen,
}: {
  expanded: boolean;
  setExpanded: (v: boolean) => void;
  onOpen: (id: string) => void;
}) {
  const { data: jobs = [] } = useJobs();
  const { data: worlds = [] } = useWorlds();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const names = useMemo(() => new Map(worlds.map((w) => [w.id, w.display_name])), [worlds]);
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued");
  const recent = jobs
    .filter((j) => !active.includes(j))
    .filter((j) => j.finished_at && now - new Date(j.finished_at).getTime() < RECENT_MS)
    .slice(0, 5);
  const shown = [...active, ...recent];
  const prevActive = useRef(active.length);
  useEffect(() => {
    if (active.length > prevActive.current) setExpanded(true);
    prevActive.current = active.length;
  }, [active.length, setExpanded]);

  if (!shown.length) return null;

  return (
    <aside className={styles.drawer} aria-label="Jobs">
      <button className={styles.head} onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
        <h3>Jobs</h3>
        <span className="muted">{active.length ? `${active.length} running` : "recent"}</span>
        <span className="muted">{expanded ? "▾" : "▴"}</span>
      </button>
      {expanded && (
        <ul className={styles.list}>
          {shown.map((j) => (
            <JobRow key={j.id} job={j} world={j.world_id ? names.get(j.world_id) : undefined} onOpen={onOpen} />
          ))}
        </ul>
      )}
    </aside>
  );
}

function JobRow({ job, world, onOpen }: { job: Job; world?: string; onOpen: (id: string) => void }) {
  const running = job.status === "running" || job.status === "queued";
  const tone = job.status === "succeeded" ? "ok" : job.status === "failed" ? "bad" : running ? "info" : "off";
  return (
    <li>
      <button className={styles.row} onClick={() => onOpen(job.id)}>
        <span className={styles.title}>
          {world ?? "—"} · {JOB_LABEL[job.type] ?? job.type}
        </span>
        <span className={`pill ${tone}${running ? " pulse" : ""}`}>{job.status}</span>
      </button>
      {job.message && <div className={styles.msg}>{job.message}</div>}
      {running && (
        <div className="bar">
          <i style={{ width: `${job.progress ?? 5}%` }} />
        </div>
      )}
    </li>
  );
}

function JobDialog({ jobId, onClose }: { jobId: string | null; onClose: () => void }) {
  const { job, log } = useJobStream(jobId);
  const logRef = useRef<HTMLPreElement>(null);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log.length]);
  const running = job && (job.status === "running" || job.status === "queued");

  return (
    <Modal
      open={!!jobId}
      onClose={onClose}
      wide
      title={job ? `${JOB_LABEL[job.type] ?? job.type}: ${job.status}` : "Job"}
      actions={
        <>
          {running && (
            <button className="btn danger" onClick={() => jobId && api.cancelJob(jobId)}>
              Cancel job
            </button>
          )}
          <button className="btn" onClick={onClose}>Close</button>
        </>
      }
    >
      {job && (
        <>
          <p className="muted">
            {job.message || "…"} · started {formatTime(job.started_at ?? job.created_at)}
          </p>
          {running && (
            <div className="bar" style={{ marginBottom: 10 }}>
              <i style={{ width: `${job.progress ?? 5}%` }} />
            </div>
          )}
        </>
      )}
      <pre ref={logRef} className={styles.log}>
        {log.length ? log.join("\n") : "No output yet."}
      </pre>
    </Modal>
  );
}
