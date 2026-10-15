import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, apiUrl } from "../api/client";
import type { Job, LogLine, WorldSummary } from "../api/types";

/**
 * One SSE connection for world status and job progress; keeps the
 * ["worlds"], ["world", id] and ["jobs"] caches fresh without polling.
 */
export function useLiveUpdates(): { connected: boolean } {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const es = new EventSource(apiUrl("/stream/worlds"));
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("snapshot", (e) => {
      qc.setQueryData(["worlds"], JSON.parse((e as MessageEvent).data) as WorldSummary[]);
    });
    es.addEventListener("world", (e) => {
      const msg = JSON.parse((e as MessageEvent).data) as WorldSummary & { deleted?: boolean };
      qc.setQueryData<WorldSummary[]>(["worlds"], (prev = []) => {
        if (msg.deleted) return prev.filter((w) => w.id !== msg.id);
        const i = prev.findIndex((w) => w.id === msg.id);
        if (i === -1) return [...prev, msg];
        const next = prev.slice();
        next[i] = msg;
        return next;
      });
      if (msg.deleted) {
        qc.removeQueries({ queryKey: ["world", msg.id] });
      } else {
        qc.setQueryData(["world", msg.id], (prev: object | undefined) =>
          prev ? { ...prev, ...msg } : prev,
        );
      }
    });
    es.addEventListener("job", (e) => {
      const job = JSON.parse((e as MessageEvent).data) as Job;
      qc.setQueryData<Job[]>(["jobs"], (prev = []) => {
        const rest = prev.filter((j) => j.id !== job.id);
        return [job, ...rest].slice(0, 50);
      });
      if (job.status !== "running" && job.status !== "queued" && job.world_id) {
        qc.invalidateQueries({ queryKey: ["events", job.world_id] });
      }
    });
    return () => es.close();
  }, [qc]);

  return { connected };
}

export function useWorlds() {
  return useQuery({ queryKey: ["worlds"], queryFn: api.worlds, staleTime: Infinity });
}

export function useJobs() {
  return useQuery({ queryKey: ["jobs"], queryFn: () => api.jobs({ limit: 50 }), staleTime: Infinity });
}

/** Live job log via SSE. */
export function useJobStream(jobId: string | null): { job: Job | null; log: string[] } {
  const [job, setJob] = useState<Job | null>(null);
  const [log, setLog] = useState<string[]>([]);

  useEffect(() => {
    setJob(null);
    setLog([]);
    if (!jobId) return;
    const es = new EventSource(apiUrl(`/stream/jobs/${jobId}`));
    es.addEventListener("snapshot", (e) => {
      const j = JSON.parse((e as MessageEvent).data) as Job;
      setJob(j);
      setLog((j.log ?? "").split("\n").filter(Boolean));
    });
    es.addEventListener("job", (e) => setJob(JSON.parse((e as MessageEvent).data)));
    es.addEventListener("log", (e) => {
      const line = JSON.parse((e as MessageEvent).data) as string;
      setLog((prev) => [...prev, line]);
    });
    // The server closes the stream when the job finishes; don't reconnect.
    es.onerror = () => es.close();
    return () => es.close();
  }, [jobId]);

  return { job, log };
}

const MAX_LINES = 5000;

/** Live container log: last `tail` lines, then follows. */
export function useLogStream(worldId: string, paused: boolean) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [status, setStatus] = useState<"connecting" | "live" | "ended" | "error">("connecting");
  const buffer = useRef<LogLine[]>([]);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    if (!paused && buffer.current.length) {
      const pending = buffer.current;
      buffer.current = [];
      setLines((prev) => [...prev, ...pending].slice(-MAX_LINES));
    }
  }, [paused]);

  useEffect(() => {
    setLines([]);
    setStatus("connecting");
    const es = new EventSource(apiUrl(`/stream/worlds/${worldId}/logs?tail=500`));
    es.onopen = () => setStatus("live");
    es.addEventListener("lines", (e) => {
      const batch = JSON.parse((e as MessageEvent).data) as LogLine[];
      if (pausedRef.current) {
        buffer.current.push(...batch);
        return;
      }
      setLines((prev) => [...prev, ...batch].slice(-MAX_LINES));
    });
    es.addEventListener("end", () => {
      setStatus("ended");
      es.close();
    });
    es.onerror = () => setStatus((s) => (s === "ended" ? s : "error"));
    return () => es.close();
  }, [worldId]);

  return { lines, status, buffered: buffer.current.length };
}
